"""Dispatch Frigate detections to the aviary-id service and fold the answer back in.

Aviary owns the orchestration: the GPU service is a stateless ``/identify`` endpoint that
answers one question, and everything about *when* to ask, *whether to believe the answer*,
and *what to do when it doesn't come back* lives here.

Ordering matters and is the subtle part. Frigate normally supplies the species in
``sub_label`` and Aviary announces on the event's ``end`` message. With identification
enabled, Frigate's classifier is off, so ``end`` arrives with no species at all: the row is
stored as ``pending``, nothing is announced, and the announcement happens later, when the
identification comes back. The "announce exactly once per detection" invariant is
preserved — it just moves.

This module deliberately does not import ``ingest``'s caller. ``ingest`` has no reference
to this module either; ``main`` wires the two together with ``ingest.set_identify_hook``,
which keeps the import graph acyclic and lets tests substitute a fake service.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Optional

import httpx

from . import db, ingest
from .settings import Settings

log = logging.getLogger("aviary.identify")

# How far either side of a detection to look for a BirdNET-Go audio detection, and how
# much more likely a species heard in that window is treated as being. Ten minutes is
# wide enough to catch a bird that called on its way to the feeder without sweeping in
# whatever was singing half an hour ago; 3.0 nudges a close call without letting audio
# overrule a confident visual match.
_PRIOR_WINDOW = 600.0
_PRIOR_MULTIPLIER = 3.0

# Bounded so a Frigate storm (or a dead GPU host) can't grow the queue without limit.
# Overflow is logged and marked failed rather than silently dropped.
_QUEUE_MAX = 200

_settings: Optional[Settings] = None
_client: Optional[httpx.AsyncClient] = None
_queue: Optional[asyncio.Queue] = None
_workers: list[asyncio.Task] = []
_loop: Optional[asyncio.AbstractEventLoop] = None

# Refs currently queued or being worked. Guarded by a lock because submit() is called
# from paho's MQTT thread while the workers clear entries on the event loop.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def configure(settings: Settings) -> None:
    global _settings
    _settings = settings


def enabled() -> bool:
    return _settings is not None and _settings.identify_active


def init_client() -> None:
    global _client
    if _client is None and _settings is not None:
        # Read timeout is the service's whole pipeline: clip download, ffmpeg, inference.
        # Connect stays short so an unreachable host fails fast instead of occupying a
        # worker for the full timeout.
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(_settings.identify_timeout, connect=5.0),
            follow_redirects=True,
        )


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _auth_headers() -> dict[str, str]:
    if _settings and _settings.identify_token:
        return {"Authorization": f"Bearer {_settings.identify_token}"}
    return {}


# ------------------------------------------------------------------------ queue

async def start(loop: asyncio.AbstractEventLoop) -> None:
    """Create the work queue and its workers. No-op when identification is off."""
    global _queue, _loop
    if not enabled():
        return
    _loop = loop
    _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    for i in range(max(1, _settings.identify_workers)):
        _workers.append(asyncio.create_task(_worker(i), name=f"aviary-identify-{i}"))
    log.info(
        "Identification enabled: %s (%d workers, min score %.2f, min margin %.2f).",
        _settings.identify_url, len(_workers),
        _settings.identify_min_score, _settings.identify_min_margin,
    )


async def stop() -> None:
    for task in _workers:
        task.cancel()
    for task in _workers:
        try:
            await task
        except asyncio.CancelledError:
            pass
    _workers.clear()


def submit(row: dict[str, Any]) -> bool:
    """Queue a detection for identification. Safe to call from the MQTT thread.

    Returns False if it was not queued — already in flight, or the queue is full.
    """
    if not enabled() or _queue is None or _loop is None:
        return False
    ref = row.get("source_ref")
    if not ref:
        return False

    with _inflight_lock:
        if ref in _inflight:
            return False
        _inflight.add(ref)

    def _put() -> None:
        try:
            _queue.put_nowait(dict(row))
        except asyncio.QueueFull:
            # Mark it rather than dropping it silently, so a backlog shows up in the
            # review queue instead of looking like the detection never happened.
            log.warning("Identification queue full; %s marked failed.", ref)
            _discard(ref)
            db.set_identification(row["source"], ref, status="failed")

    # call_soon_threadsafe rather than run_coroutine_threadsafe: put_nowait doesn't need
    # to await, and this way the MQTT thread never blocks on the event loop.
    _loop.call_soon_threadsafe(_put)
    return True


def _discard(ref: str) -> None:
    with _inflight_lock:
        _inflight.discard(ref)


async def requeue_pending() -> int:
    """Resubmit rows stranded in 'pending' by a restart. Returns how many.

    Without this they are stuck forever: the MQTT ``end`` message that would have
    triggered identification is long gone, and nothing else ever revisits the row.
    """
    if not enabled():
        return 0
    rows = await asyncio.to_thread(db.pending_identifications)
    queued = sum(1 for row in rows if submit(row))
    if queued:
        log.info("Requeued %d detection(s) left pending by a restart.", queued)
    return queued


# ----------------------------------------------------------------------- worker

async def _worker(index: int) -> None:
    while True:
        row = await _queue.get()
        ref = row.get("source_ref", "")
        try:
            await _process(row)
        except asyncio.CancelledError:
            _discard(ref)
            raise
        except Exception:
            # A worker that dies takes a permanent slice of throughput with it, and the
            # detection would sit in 'pending' forever. Log and keep serving.
            log.exception("Identification worker %d failed on %s", index, ref)
            await asyncio.to_thread(
                db.set_identification, row.get("source", "frigate"), ref, "failed"
            )
        finally:
            _discard(ref)
            _queue.task_done()


async def _exclusions(row: dict[str, Any]) -> list[str]:
    """Species the identifier must not suggest for this detection.

    Two sources, and they mean different things:

    * Per-detection rejections — the user pressed "wrong" on this bird. Unambiguous: the
      answer was incorrect *here*, so hand it back and the model walks down its ranking.
    * The global blacklist — only when ``identify_exclude_blacklisted`` is on. Aviary
      documents the blacklist as being for species a classifier is reliably wrong about,
      and under that reading excluding them outright is a real accuracy gain: a bird
      currently misread as a blacklisted species is discarded entirely, whereas excluding
      it lets the correct species win instead. If you blacklisted a species that genuinely
      does visit — because you simply don't want it recorded — turn this off, or every one
      of its visits will be recorded as something else.
    """
    names: list[str] = []
    if row.get("id"):
        names.extend(await asyncio.to_thread(db.rejections_for, row["id"]))
    if _settings.identify_exclude_blacklisted:
        for common, sci in await asyncio.to_thread(db.blacklist_names):
            names.extend(n for n in (common, sci) if n)
    return names


async def _process(row: dict[str, Any]) -> None:
    ref = row["source_ref"]
    priors = await _audio_priors(row)
    exclude = await _exclusions(row)
    result = await _call_service(ref, priors, exclude)

    if result is None or result.get("status") != "ok":
        status = (result or {}).get("status", "failed")
        log.info("Identification for %s returned %s.", ref, status)
        await asyncio.to_thread(db.set_identification, row["source"], ref, "failed")
        return

    score = float(result.get("score") or 0.0)
    margin = float(result.get("margin") or 0.0)
    model = result.get("model_version")
    embedding = result.get("embedding")
    name = result.get("common_name")

    if (not name
            or score < _settings.identify_min_score
            or margin < _settings.identify_min_margin):
        log.info(
            "Identification for %s below threshold: %s score=%.3f margin=%.3f "
            "(runner-up %s) — queued for review.",
            ref, name, score, margin, result.get("runner_up"),
        )
        await asyncio.to_thread(
            db.set_identification, row["source"], ref, "low_confidence",
            score, margin, model, embedding,
        )
        return

    identified = dict(row)
    identified["common_name"] = name
    identified["scientific_name"] = result.get("scientific_name")
    identified["species_code"] = result.get("species_code")
    # Set here for the notification payload, which is built from this dict. The database
    # column is overwritten separately below via set_confidence, because upsert_detection
    # deliberately keeps the *highest* confidence it has seen and would otherwise let
    # Frigate's object-detection score stand in for the species score.
    identified["confidence"] = score

    # Back through the normal pipeline so canonicalization, the blacklist and the
    # new-species notification all apply — none of which could run earlier, because until
    # now there was no species to apply them to.
    stored = await asyncio.to_thread(ingest.store_row, identified, True, True)
    if not stored:
        # Filtered on the way in; almost always a blacklisted species. Remove the
        # provisional row rather than leaving an orphan stuck in 'pending'.
        log.debug("Identified %s as %s, which is filtered; dropping the row.", ref, name)
        await asyncio.to_thread(db.drop_detection, row["source"], ref)
        return

    await asyncio.to_thread(
        db.set_identification, row["source"], ref, "ok", score, margin, model, embedding,
        True,  # set_confidence — see the note above
    )
    log.info(
        "Identified %s as %s (score=%.3f margin=%.3f, %d frames%s, %sms).",
        ref, name, score, margin, result.get("frames_used", 0),
        f", {result['excluded']} excluded" if result.get("excluded") else "",
        result.get("elapsed_ms", "?"),
    )


async def _audio_priors(row: dict[str, Any]) -> dict[str, float]:
    if not _settings.identify_use_audio_priors:
        return {}
    start = float(row.get("start_time") or 0.0)
    if not start:
        return {}
    heard = await asyncio.to_thread(
        db.species_heard_between, start - _PRIOR_WINDOW, start + _PRIOR_WINDOW
    )
    return {name: _PRIOR_MULTIPLIER for name in heard}


async def _call_service(event_id: str, priors: dict[str, float],
                        exclude: Optional[list[str]] = None) -> Optional[dict]:
    """POST to the service, retrying once. Returns the parsed body or None."""
    if _client is None:
        return None
    payload = {
        "event_id": event_id,
        "priors": priors,
        "exclude": exclude or [],
        # Our thresholds, sent so the service knows when an answer is not good enough yet
        # and it should sample more frames. It never gates on these — it still returns raw
        # numbers and the decision below is ours — so there is exactly one source of truth
        # and retuning here also retunes when the GPU works harder.
        "min_score": _settings.identify_min_score,
        "min_margin": _settings.identify_min_margin,
    }
    url = f"{_settings.identify_url}/identify"

    for attempt in (1, 2):
        try:
            resp = await _client.post(url, json=payload, headers=_auth_headers())
        except httpx.HTTPError as exc:
            log.warning("Identification request for %s failed (attempt %d): %s",
                        event_id, attempt, exc)
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                # Retrying won't fix a wrong token, and this is worth being loud about:
                # every detection will fail until it's corrected.
                log.error("Identification service rejected our token (401). "
                          "Check identify_token against AVIARY_ID_TOKEN.")
                return None
            log.warning("Identification service returned %s for %s (attempt %d): %s",
                        resp.status_code, event_id, attempt, resp.text[:200])
            # 4xx other than 401 is a bad request; a retry produces the same answer.
            if resp.status_code < 500:
                return None
        if attempt == 1:
            await asyncio.sleep(2.0)
    return None


# ------------------------------------------------------------------ diagnostics

async def health() -> dict:
    """Service status for the settings page. Never raises."""
    if not enabled():
        return {"configured": False}
    if _client is None:
        return {"configured": True, "ok": False, "error": "client not initialized"}
    try:
        resp = await _client.get(
            f"{_settings.identify_url}/healthz", timeout=httpx.Timeout(5.0)
        )
        if resp.status_code != 200:
            return {"configured": True, "ok": False, "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        data["configured"] = True
        return data
    except (httpx.HTTPError, ValueError) as exc:
        return {"configured": True, "ok": False, "error": str(exc)[:200]}


async def identify_one(row: dict[str, Any]) -> dict:
    """Identify a single detection on demand, bypassing the queue.

    Backs the UI's "re-identify" action, where the user is waiting on the answer. The
    service serializes GPU work itself, so jumping the queue costs latency for queued
    events but never correctness.
    """
    if not enabled():
        return {"status": "disabled"}
    ref = row["source_ref"]
    with _inflight_lock:
        if ref in _inflight:
            return {"status": "already_running"}
        _inflight.add(ref)
    try:
        await _process(row)
    finally:
        _discard(ref)
    updated = await asyncio.to_thread(db.detection_by_ref, row["source"], ref)
    return {"status": "ok", "detection": updated}


async def purge_old() -> int:
    """Drop unidentifiable rows past the retention window. 0 days keeps them forever."""
    if not enabled() or _settings.identify_retain_days <= 0:
        return 0
    cutoff = time.time() - _settings.identify_retain_days * 86400
    removed = await asyncio.to_thread(db.purge_unidentified, cutoff)
    if removed:
        log.info("Purged %d unidentified detection(s) older than %d days.",
                 removed, _settings.identify_retain_days)
    return removed
