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
import json
import logging
import threading
import time
from typing import Any, Optional

import httpx

from . import crops, db, ingest, probe
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

# How far below the configured thresholds frame consensus can rescue an answer, as a
# fraction of each threshold. A modest fused score backed by independent frames agreeing
# is stronger evidence than the score alone suggests — a 0.40 softmax over hundreds of
# species with every frame voting the same way is a confident answer, not a doubtful one.
_CONSENSUS_RESCUE = 0.5

# Minimum seconds between self-heal probe rebuilds, so a database with nothing to load
# (fresh install) costs two SELECTs a minute, not two per event.
_PROBE_HEAL_INTERVAL = 60.0
_probe_heal_at = 0.0

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
        rejected = await asyncio.to_thread(db.rejections_for, row["id"])
        if rejected:
            # Loud on purpose. A rejection silently vetoes that species on every future
            # run of this detection — the single most confusing failure mode this
            # pipeline has ("why does re-identify keep refusing the obvious answer?").
            # It must be visible in the log next to the result it shaped.
            log.info(
                "%s carries %d rejected answer(s): %s — these cannot win again for "
                "this detection until its rejections are reset.",
                row.get("source_ref"), len(rejected), ", ".join(rejected),
            )
        names.extend(rejected)
    if _settings.identify_exclude_blacklisted:
        for common, sci in await asyncio.to_thread(db.blacklist_names):
            names.extend(n for n in (common, sci) if n)
    return names


async def _ensure_probe(embed_key: str) -> None:
    """Rebuild the probe inline when it is empty or loaded for a different model.

    This is what makes "the probe builds on first use" actually true. The startup
    maintenance task can miss the service (it may take minutes to come up), and the
    service can be redeployed with a different model while the add-on keeps running —
    in both cases the probe would otherwise silently abstain on every event until the
    next add-on restart. A rebuild is a DB read plus numpy stacking (sub-second at this
    scale), so doing it inline means the CURRENT event already benefits. Throttled so a
    fresh install with nothing to load doesn't rebuild on every single event.
    """
    global _probe_heal_at
    if not embed_key or (probe.ready() and probe.model() == embed_key):
        return
    now = time.monotonic()
    if now - _probe_heal_at < _PROBE_HEAL_INTERVAL:
        return
    _probe_heal_at = now
    await asyncio.to_thread(probe.rebuild, embed_key)


def _zoom_for(row: dict[str, Any]) -> Optional[dict]:
    """The PTZ recordings window the service should classify instead of the event clip.

    None whenever zoom doesn't apply: no mapping for this camera, or no usable time
    window (the end message carries end_time, so a live dispatch always has one; a
    missing one means an odd row, and the event's own media is the honest fallback).
    """
    if not _settings or not _settings.identify_zoom_map:
        return None
    camera = (row.get("location") or "").strip().lower()
    ptz = _settings.identify_zoom_map.get(camera)
    if not ptz:
        return None
    start, end = row.get("start_time"), row.get("end_time")
    if not start or not end or end <= start:
        return None
    # Trim PTZ travel time off the front so a leftover view of the camera's previous
    # target is not classified — unless the event is shorter than the trim itself.
    offset = _settings.identify_zoom_start_offset
    if start + offset < end:
        start += offset
    return {"camera": ptz, "start": start, "end": end}


def _zone_rank(zone_csv: Optional[str], rank: dict[str, int], unranked: int) -> int:
    """Best (lowest) priority rank among a row's comma-joined zones."""
    zones = (z.strip().lower() for z in (zone_csv or "").split(","))
    return min((rank.get(z, unranked) for z in zones if z), default=unranked)


async def _zoom_allowed(row: dict[str, Any]) -> bool:
    """Whether the PTZ was plausibly pointed at THIS event's bird.

    The PTZ automation parks on the highest-priority occupied zone, so when another
    bird's event overlaps this one in a higher-priority zone, the zoomed footage would
    show the wrong bird — skip zoom and classify the event's own media instead.
    Conservative on purpose: overlap at any point in the window disqualifies, because
    PTZ timing within the window is unknowable from here.
    """
    priority = _settings.identify_zoom_zone_priority
    if not priority:
        return True
    rank = {name: i for i, name in enumerate(priority)}
    unranked = len(priority)
    own = _zone_rank(row.get("zone"), rank, unranked)
    others = await asyncio.to_thread(
        db.overlapping_detections, row["source_ref"],
        row.get("start_time") or 0.0, row.get("end_time") or 0.0,
    )
    for other in others:
        if _zone_rank(other.get("zone"), rank, unranked) < own:
            log.info(
                "Zoom skipped for %s: concurrent event %s in a higher-priority zone "
                "(%s outranks %s) — the PTZ was likely filming that bird.",
                row["source_ref"], other.get("source_ref"),
                other.get("zone"), row.get("zone") or "no zone",
            )
            return False
    return True


async def _process(row: dict[str, Any]) -> None:
    ref = row["source_ref"]
    priors = await _audio_priors(row)
    exclude = await _exclusions(row)
    zoom = _zoom_for(row)
    if zoom is not None and not await _zoom_allowed(row):
        zoom = None
    result = await _call_service(ref, priors, exclude, zoom)

    if result is None or result.get("status") != "ok":
        status = (result or {}).get("status", "failed")
        log.info("Identification for %s returned %s.", ref, status)
        await asyncio.to_thread(db.set_identification, row["source"], ref, "failed")
        return

    score = float(result.get("score") or 0.0)
    margin = float(result.get("margin") or 0.0)
    # Two identities, on purpose. model_version (stored as id_model) is result
    # provenance — it changes with the vocabulary, marking rows worth re-identifying
    # after a region change. embedding_key names what the EMBEDDING is comparable
    # under (the model alone) and survives vocabulary changes; older services don't
    # send it, so it is recovered from the combined string.
    model = result.get("model_version")
    embed_key = result.get("embedding_key") or db.embedding_key_from(model or "")
    embedding = result.get("embedding")
    name = result.get("common_name")
    # The shortlist the model considered. Kept whatever the outcome: it is most useful
    # precisely when the top answer was rejected, because it shows the model did try and
    # lets the right bird be picked by hand from what it was weighing up.
    shortlist = _encode_candidates(result.get("candidates"))

    # Normalize the service's candidate shape (common_name/scientific_name) to the slim
    # one probe.blend expects (name/sci). Passing the raw list looked like it worked but
    # silently left the blend's zero-shot side EMPTY — the probe's distribution replaced
    # the zero-shot answer outright instead of mixing with it, at any blend weight.
    slim_candidates = [
        {"name": c.get("common_name") or c.get("name"),
         "sci": c.get("scientific_name") or c.get("sci"),
         "code": c.get("species_code") or c.get("code"),
         "score": float(c.get("score") or 0.0)}
        for c in (result.get("candidates") or [])
        if isinstance(c, dict) and (c.get("common_name") or c.get("name"))
    ]

    # Fold in what we have learned from confirmed birds of our own. Applied before the
    # thresholds, not after: the probe exists to rescue exactly the results that would
    # otherwise be rejected, so gating first would throw away the cases it is for.
    await _ensure_probe(embed_key)
    probe_examples: Optional[int] = None
    probe_weight: Optional[float] = None
    blended = probe.blend(embedding or "", slim_candidates, embed_key,
                          exclude=set(exclude or []))
    if blended:
        if blended["name"] != name:
            log.info(
                "Probe reranked %s: %s (%.3f) -> %s (%.3f), matched against %d confirmed "
                "%s of your own.",
                ref, name, score, blended["name"], blended["score"],
                blended["probe_examples"], blended["name"],
            )
        name = blended["name"]
        score, margin = blended["score"], blended["margin"]
        probe_examples = blended["probe_examples"]
        probe_weight = blended.get("probe_weight")
        result["scientific_name"] = blended.get("sci") or result.get("scientific_name")
        result["species_code"] = blended.get("code") or result.get("species_code")
        shortlist = _encode_candidates(blended["candidates"]) or shortlist

    # Frame agreement, computed by the service. Only meaningful while the final answer
    # is still the service's own winner: if the probe reranked to a different species,
    # votes about the old winner say nothing about the new one — and the probe's example
    # evidence is playing the corroboration role instead. None means "no data" (too few
    # frames could vote), which is deliberately treated as neither support nor dissent.
    agreed: Optional[bool] = None
    consensus = result.get("consensus")
    if isinstance(consensus, dict) and name == result.get("common_name"):
        agreed = bool(consensus.get("agreed"))

    passes = (bool(name)
              and score >= _settings.identify_min_score
              and margin >= _settings.identify_min_margin)
    if passes and agreed is False:
        # The fused score cleared the bar but the frames actively voted for different
        # species — a good-looking average emerging from conflicting votes is exactly
        # the failure mode consensus exists to catch. A human gets the final say.
        log.info(
            "Identification for %s scored %.3f but frames disagreed (%d/%d for %s) "
            "— queued for review.",
            ref, score, consensus.get("supporting", 0), consensus.get("votes", 0), name,
        )
        passes = False
    rescued = (not passes and bool(name) and agreed is True
               and score >= _CONSENSUS_RESCUE * _settings.identify_min_score
               and margin >= _CONSENSUS_RESCUE * _settings.identify_min_margin)
    if rescued:
        log.info(
            "Identification for %s below threshold (score=%.3f margin=%.3f) but %d/%d "
            "frames agree on %s — accepting on consensus.",
            ref, score, margin, consensus.get("supporting", 0),
            consensus.get("votes", 0), name,
        )

    # Keep the crop that backed the answer either way. The review queue is where seeing
    # what the model actually looked at matters MOST — especially when the classified
    # footage (a zoomed PTZ recording) is not the event's own media.
    await asyncio.to_thread(crops.save, ref, result.get("best_crop"))

    if not passes and not rescued:
        log.info(
            "Identification for %s below threshold: %s score=%.3f margin=%.3f "
            "(runner-up %s) — queued for review.",
            ref, name, score, margin, result.get("runner_up"),
        )
        await asyncio.to_thread(
            db.set_identification, row["source"], ref, "low_confidence",
            score, margin, model, embedding, False, shortlist,
            embedding_model=embed_key, probe_weight=probe_weight,
            probe_examples=probe_examples,
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
        # provisional row rather than leaving an orphan stuck in 'pending' — and the
        # crop stored above, which now has no row to belong to.
        log.debug("Identified %s as %s, which is filtered; dropping the row.", ref, name)
        await asyncio.to_thread(db.drop_detection, row["source"], ref)
        await asyncio.to_thread(crops.remove, ref)
        return

    await asyncio.to_thread(
        db.set_identification, row["source"], ref, "ok", score, margin, model, embedding,
        True,  # set_confidence — see the note above
        shortlist,
        embedding_model=embed_key, probe_weight=probe_weight,
        probe_examples=probe_examples,
    )
    log.info(
        "Identified %s as %s (score=%.3f margin=%.3f, %d frames%s%s, %sms).",
        ref, name, score, margin, result.get("frames_used", 0),
        f", {result['excluded']} excluded" if result.get("excluded") else "",
        f", probe:{probe_examples}" if probe_examples else "",
        result.get("elapsed_ms", "?"),
    )


def _encode_candidates(candidates: Any) -> Optional[str]:
    """Compact the service's shortlist for storage. None when there is nothing useful.

    Only the top few are kept: the service now returns a much longer list so the probe
    can blend over real softmax mass, but for storage and display anything past fifth
    place is noise.
    """
    if not isinstance(candidates, list) or not candidates:
        return None
    candidates = candidates[:5]
    slim = [
        {
            # Accepts both the service's shape (common_name/scientific_name) and the
            # probe's already-slim shape (name/sci), so a reranked shortlist stores the
            # same way an untouched one does.
            "name": c.get("common_name") or c.get("name"),
            "sci": c.get("scientific_name") or c.get("sci"),
            "code": c.get("species_code") or c.get("code"),
            "score": round(float(c.get("score") or 0.0), 4),
        }
        for c in candidates
        if isinstance(c, dict) and (c.get("common_name") or c.get("name"))
    ]
    return json.dumps(slim) if slim else None


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
                        exclude: Optional[list[str]] = None,
                        zoom: Optional[dict] = None) -> Optional[dict]:
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
    if zoom:
        # The PTZ camera's recordings window to classify instead of the event clip —
        # see _zoom_for. The service falls back to the event clip on its own if the
        # recordings turn out not to exist.
        payload["zoom"] = zoom
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


async def species_list() -> dict:
    """The service's candidate vocabulary, for the manual-entry picker. Never raises."""
    if not enabled() or _client is None:
        return {"ok": False, "species": []}
    try:
        resp = await _client.get(f"{_settings.identify_url}/species",
                                 headers=_auth_headers(), timeout=httpx.Timeout(10.0))
        if resp.status_code != 200:
            return {"ok": False, "species": [], "error": f"HTTP {resp.status_code}"}
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "species": [], "error": str(exc)[:200]}
    # Only the names are needed for a datalist; the rest is noise on the wire.
    return {
        "ok": True,
        "species": [s.get("common_name") for s in data.get("species", [])
                    if s.get("common_name")],
    }


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


async def probe_model() -> Optional[str]:
    """The key stored embeddings are compared under, from the service's health.

    Prefers the service's ``embedding_key`` (the model name alone, stable across
    vocabulary changes); falls back to reducing an older service's ``model_version``
    so the probe keeps working against a container that predates the field.
    """
    data = await health()
    if not data.get("ok"):
        return None
    key = data.get("embedding_key")
    if key:
        return key
    version = data.get("model_version")
    return db.embedding_key_from(version) if version else None


async def backfill_embedding(row: dict[str, Any]) -> bool:
    """Store an embedding for a detection that never got one. True if one was stored.

    A manual label on a detection whose identification failed (or predates embeddings)
    teaches the probe nothing — there is no vector for the confirmed name to train. This
    re-runs the event through the service purely to harvest the embedding of its best
    frame; the label, status and confidence are deliberately left alone, because the
    human's answer outranks anything the service would say.
    """
    if not enabled():
        return False
    ref = row.get("source_ref")
    if not ref or row.get("source") != "frigate" or not row.get("id"):
        return False
    if await asyncio.to_thread(db.has_embedding, row["id"]):
        return False
    with _inflight_lock:
        if ref in _inflight:
            return False
        _inflight.add(ref)
    # Same zoom decision as a live identification: the embedding should come from the
    # same footage a live run would have looked at.
    zoom = _zoom_for(row)
    if zoom is not None and not await _zoom_allowed(row):
        zoom = None
    try:
        result = await _call_service(ref, {}, [], zoom)
    finally:
        _discard(ref)
    if not result or result.get("status") != "ok":
        log.debug("Embedding backfill for %s returned %s.",
                  ref, (result or {}).get("status", "no answer"))
        return False
    embedding = result.get("embedding")
    key = (result.get("embedding_key")
           or db.embedding_key_from(result.get("model_version") or ""))
    if not embedding or not key:
        return False
    await asyncio.to_thread(db.put_detection_embedding, row["id"], key, embedding)
    log.info("Backfilled an embedding for %s so its manual label can teach the probe.", ref)
    return True


async def purge_old() -> int:
    """Drop unidentifiable rows past the retention window. 0 days keeps them forever."""
    if not enabled() or _settings.identify_retain_days <= 0:
        return 0
    cutoff = time.time() - _settings.identify_retain_days * 86400
    refs = await asyncio.to_thread(db.purge_unidentified, cutoff)
    for ref in refs:
        await asyncio.to_thread(crops.remove, ref)
    if refs:
        log.info("Purged %d unidentified detection(s) older than %d days.",
                 len(refs), _settings.identify_retain_days)
    return len(refs)
