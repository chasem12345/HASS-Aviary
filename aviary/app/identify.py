"""Dispatch Frigate detections to the aviary-id service and fold the answer back in.

Aviary owns the orchestration: the GPU service is a stateless ``/identify`` endpoint that
answers one question, and everything about *when* to ask, *whether to believe the answer*,
and *what to do when it doesn't come back* lives here.

Ordering matters and is the subtle part. Without identification, Frigate supplies the
species in ``sub_label`` and Aviary announces on the event's ``end`` message. With it
enabled there are two routes, both of which MOVE the announcement rather than add one:

* Frigate could not name the bird (its classifier is off, or unsure): ``end`` arrives as
  generic 'bird', the row is stored ``pending``, and the announcement happens when the
  full identification comes back.
* Frigate DID name it (classifier on, service 0.12.0+): the row is stored ``confirming``
  with Frigate's name showing, the service takes a quick look at Frigate's own crop —
  no clip — and the announcement happens when that lands: Frigate's label as-is unless
  the birds confirmed by hand (the learning probe) confidently say otherwise, in which
  case the learned name wins and Frigate's becomes the runner-up. If the service cannot
  be reached, Frigate's label is announced anyway (status ``frigate``).

Frigate applies a sub_label the moment its classifier clears the threshold, so the same
object can arrive unnamed first and named later — even after ``end``. ``ingest`` routes
those late labels here (``resolve_late_label``) or records them for ``_process`` to read,
so every ordering yields one row and one announcement per (visit, species).

This module deliberately does not import ``ingest``'s caller. ``ingest`` has no reference
to this module either; ``main`` wires the two together with ``ingest.set_identify_hook``,
which keeps the import graph acyclic and lets tests substitute a fake service.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
from typing import Any, Optional

import httpx

from . import crops, db, http, ingest, probe
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

def _make_client() -> Optional[httpx.AsyncClient]:
    if _settings is None:
        return None
    # Read timeout is the service's whole pipeline: clip download, ffmpeg, inference.
    # Connect stays short so an unreachable host fails fast instead of occupying a
    # worker for the full timeout.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(_settings.identify_timeout, connect=5.0),
        follow_redirects=True,
    )


_http = http.register("identify", _make_client)
# A priority queue: confirmations (Frigate's crops only, no clip) go ahead of full
# identifications. They are the quick answers, and a burst of birds fills the queue with
# slow clip pipelines — exactly the moment a confirmation must not wait behind them.
# Items are (priority, sequence, row); the sequence keeps FIFO order within a priority
# and stops the queue from ever comparing two dicts.
_queue: Optional[asyncio.PriorityQueue] = None
_seq = itertools.count()
_PRIORITY_CONFIRM = 0
_PRIORITY_FULL = 1
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
    _queue = asyncio.PriorityQueue(maxsize=_QUEUE_MAX)
    for i in range(max(1, _settings.identify_workers)):
        _workers.append(asyncio.create_task(_worker(i), name=f"aviary-identify-{i}"))
    log.info(
        "Identification enabled: %s (%d workers, min score %.2f, min margin %.2f).",
        _settings.identify_url, len(_workers),
        _settings.identify_min_score, _settings.identify_min_margin,
    )
    # Keeps confirm_available() current for the MQTT thread, which cannot await.
    _workers.append(asyncio.create_task(_capability_loop(), name="aviary-identify-capabilities"))


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

    confirm = row.get("_mode") == "confirm"
    priority = _PRIORITY_CONFIRM if confirm else _PRIORITY_FULL

    def _put() -> None:
        try:
            _queue.put_nowait((priority, next(_seq), dict(row)))
        except asyncio.QueueFull:
            _discard(ref)
            if confirm:
                # Frigate already has a name for this bird; a full queue is no reason
                # to hold it back. Announce as-is (status 'frigate') — same as when the
                # service cannot be reached.
                log.warning("Identification queue full; announcing Frigate's %s for %s "
                            "without confirmation.", row.get("common_name"), ref)
                _loop.create_task(_accept_frigate(dict(row), "queue full"))
                return
            # Mark it rather than dropping it silently, so a backlog shows up in the
            # review queue instead of looking like the detection never happened.
            log.warning("Identification queue full; %s marked failed.", ref)
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
    for row in rows:
        # A row stored 'confirming' was waiting on the quick look at Frigate's crop, not
        # a full identification; its common_name is Frigate's (canonical) label.
        if row.get("id_status") == "confirming":
            row["_mode"] = "confirm"
    queued = sum(1 for row in rows if submit(row))
    if queued:
        log.info("Requeued %d detection(s) left pending by a restart.", queued)
    return queued


# ----------------------------------------------------------------------- worker

async def _worker(index: int) -> None:
    while True:
        _, _, row = await _queue.get()
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
            settled = False
            if row.get("_mode") == "confirm":
                # Frigate's name is still good; a bug here must not hold it back.
                try:
                    await _accept_frigate(row, "error")
                    settled = True
                except Exception:  # noqa: BLE001
                    log.exception("Falling back to Frigate's label failed for %s", ref)
            if not settled:
                await asyncio.to_thread(
                    db.set_identification, row.get("source", "frigate"), ref, "failed"
                )
        finally:
            _discard(ref)
            _queue.task_done()


async def _exclusions(row: dict[str, Any]) -> list[str]:
    return await asyncio.to_thread(_exclusion_names, row)


def _exclusion_names(row: dict[str, Any]) -> list[str]:
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
        rejected = db.rejections_for(row["id"])
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
        for common, sci in db.blacklist_names():
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
    if row.get("_mode") == "confirm":
        await _process_confirm(row)
        return
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
    # Tracked alongside name so the review log stays truthful after a probe rerank —
    # the service's runner-up says nothing about the blended ranking.
    runner_up = result.get("runner_up")
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
        if len(blended.get("candidates") or []) > 1:
            runner_up = blended["candidates"][1].get("name")
        probe_examples = blended["probe_examples"]
        probe_weight = blended.get("probe_weight")
        result["scientific_name"] = blended.get("sci") or result.get("scientific_name")
        result["species_code"] = blended.get("code") or result.get("species_code")
        shortlist = _encode_candidates(blended["candidates"]) or shortlist

    # Frame agreement, computed by the service. It plays ONE role here: rescuing a
    # modest answer that every frame independently backed (below). It never demotes an
    # answer that already cleared the user's thresholds — the thresholds are final.
    # Frames legitimately disagree when a second bird shares the view (a hummingbird at
    # the feeder next to a cardinal split the vote 1/2 and sent a passing 0.53 to review),
    # and that is not evidence against the winner. Only meaningful while the final answer
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
    if await asyncio.to_thread(crops.save, ref, result.get("best_crop")):
        # The row keeps a flag too: the feed filter needs "has its own picture" in SQL.
        await asyncio.to_thread(db.set_has_crop, ref, True)
    # And every OTHER bird the service found in the event, each on its own crop and
    # embedding. Done on both branches below: a second bird can be perfectly clear while
    # the tracked one is not.
    await _store_subjects(row, result, embed_key)

    if not passes and not rescued and row.get("source") == "frigate":
        # Frigate may have named this bird while we were looking (its classifier
        # applies a sub_label the moment it clears the threshold — possibly after the
        # end message that sent us here). An uncertain answer of ours does not outrank
        # a confident one of Frigate's: its label stands, with our winner as runner-up,
        # exactly as a confirmation would have ended. Never a name that was rejected
        # for this bird or blacklisted — that is what sent it to the full run.
        late = await _late_frigate_label(row, result)
        if late and late[0].lower() not in {e.lower() for e in (exclude or [])}:
            label, late_score = late
            verdict = _frigate_verdict(label, slim_candidates, blended)
            recorded = await _record_answer(
                row, result, verdict, embed_key=embed_key, probe_weight=probe_weight,
                probe_examples=probe_examples,
                confidence=late_score if late_score is not None else verdict["score"],
            )
            if recorded:
                log.info(
                    "Identification for %s was uncertain (%s score=%.3f margin=%.3f) but "
                    "Frigate named it %s%s — Frigate's label stands.",
                    ref, name, score, margin, label,
                    f" ({late_score:.2f})" if late_score is not None else "",
                )
            return

    if not passes and not rescued:
        log.info(
            "Identification for %s below threshold: %s score=%.3f margin=%.3f "
            "(runner-up %s) — queued for review.",
            ref, name, score, margin, runner_up,
        )
        await asyncio.to_thread(
            db.set_identification, row["source"], ref, "low_confidence",
            score, margin, model, embedding, False, shortlist,
            embedding_model=embed_key, probe_weight=probe_weight,
            probe_examples=probe_examples,
            # The frame behind this embedding was chosen for the SERVICE's winner (the
            # probe reranks names, not frames). Recorded so a later relabel to another
            # species knows the stored frame is the wrong one to learn from.
            embedding_target=result.get("embedding_target") or result.get("common_name"),
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
        embedding_target=result.get("embedding_target") or result.get("common_name"),
    )
    log.info(
        "Identified %s as %s (score=%.3f margin=%.3f, %d frames%s%s, %sms).",
        ref, name, score, margin, result.get("frames_used", 0),
        f", {result['excluded']} excluded" if result.get("excluded") else "",
        f", probe:{probe_examples}" if probe_examples else "",
        result.get("elapsed_ms", "?"),
    )


# ------------------------------------------------------------ Frigate confirmation

def _slim_candidates(candidates: Any) -> list[dict]:
    """The service's candidate shape (common_name/scientific_name) as the slim one
    probe.blend expects (name/sci/code/score). Accepts either shape."""
    return [
        {"name": c.get("common_name") or c.get("name"),
         "sci": c.get("scientific_name") or c.get("sci"),
         "code": c.get("species_code") or c.get("code"),
         "score": float(c.get("score") or 0.0)}
        for c in (candidates or [])
        if isinstance(c, dict) and (c.get("common_name") or c.get("name"))
    ]


def _decode_candidates(stored: Any) -> list[dict]:
    """A row's stored ``id_candidates`` JSON back to the slim list."""
    if not stored:
        return []
    try:
        data = json.loads(stored) if isinstance(stored, str) else stored
    except (ValueError, TypeError):
        return []
    return _slim_candidates(data) if isinstance(data, list) else []


def _lookup(candidates: list[dict], name: str) -> Optional[dict]:
    """The candidate for a species, by common OR scientific name, case-insensitive."""
    key = (name or "").strip().lower()
    if not key:
        return None
    for c in candidates:
        if (c.get("name") or "").lower() == key or (c.get("sci") or "").lower() == key:
            return c
    return None


def _frigate_verdict(frigate_name: str, service: list[dict],
                     blended: Optional[dict]) -> dict:
    """Decide between Frigate's label and what the learned birds say.

    The rule (the user's, 2026-09-19): Frigate's label stands unless the probe — the
    birds confirmed by hand — blends to a DIFFERENT species that clears the same
    thresholds a fresh identification must (``identify_min_score`` /
    ``identify_min_margin``). The service's own zero-shot disagreement never overrides on
    its own: with its default supervised backend being Frigate's model, and Frigate
    having seen the bird across many frames, an uncorroborated "I'd have said otherwise"
    is not evidence enough. It is recorded as the runner-up so the card can say so.

    Returns ``{name, sci, code, score, margin, runner_up, candidates, override}``. When
    Frigate stands, ``score`` is the (blended or service) probability of ITS species and
    ``margin`` its lead over the best other candidate — honest numbers about the label,
    not about whichever species the service preferred.
    """
    ranked = list(blended["candidates"]) if blended else list(service)
    learned = blended["name"] if blended else None
    override = (
        blended is not None
        and (learned or "").lower() != frigate_name.lower()
        and blended["score"] >= _settings.identify_min_score
        and blended["margin"] >= _settings.identify_min_margin
    )
    if override:
        return {
            "name": learned, "sci": blended.get("sci"), "code": blended.get("code"),
            "score": float(blended["score"]), "margin": float(blended["margin"]),
            "runner_up": frigate_name, "candidates": ranked, "override": True,
        }
    mine = _lookup(ranked, frigate_name) or _lookup(service, frigate_name)
    score = float(mine["score"]) if mine else 0.0
    others = [float(c["score"]) for c in ranked
              if (c.get("name") or "").lower() != frigate_name.lower()
              and (c.get("sci") or "").lower() != frigate_name.lower()]
    margin = max(0.0, score - max(others, default=0.0))
    # What aviary-id would have said instead, if anything.
    top = ranked[0] if ranked else None
    if top and (top.get("name") or "").lower() != frigate_name.lower():
        runner_up = top.get("name")
    else:
        runner_up = ranked[1].get("name") if len(ranked) > 1 else None
    return {
        "name": frigate_name,
        "sci": mine.get("sci") if mine else None,
        "code": mine.get("code") if mine else None,
        "score": score, "margin": margin, "runner_up": runner_up,
        "candidates": ranked, "override": False,
    }


async def _record_answer(row: dict[str, Any], result: dict, verdict: dict, *,
                         embed_key: Optional[str], probe_weight: Optional[float],
                         probe_examples: Optional[int], confidence: Optional[float]) -> bool:
    """Write a settled species onto the row and announce it — the one announcement.

    Goes back through ``ingest.store_row`` (authoritative) so canonicalization, the
    blacklist, the new-species notification and the keepsake hook all apply. False when
    the row was filtered on the way in (a blacklisted species), in which case it is
    removed along with its crop, as ``_process`` does.
    """
    ref = row["source_ref"]
    identified = {k: v for k, v in row.items() if k != "_mode"}
    identified["common_name"] = verdict["name"]
    identified["scientific_name"] = verdict.get("sci")
    identified["species_code"] = verdict.get("code")
    # For the notification payload; the column is written by set_identification below
    # (upsert keeps the highest confidence seen, which would be Frigate's object score).
    identified["confidence"] = confidence
    stored = await asyncio.to_thread(ingest.store_row, identified, True, True, False, True)
    if not stored:
        log.debug("Settled %s as %s, which is filtered; dropping the row.", ref, verdict["name"])
        await asyncio.to_thread(db.drop_detection, row["source"], ref)
        await asyncio.to_thread(crops.remove, ref)
        return False
    await asyncio.to_thread(
        db.set_identification, row["source"], ref, "ok",
        verdict["score"], verdict["margin"], result.get("model_version"),
        result.get("embedding"), True, _encode_candidates(verdict["candidates"]),
        embedding_model=embed_key, probe_weight=probe_weight, probe_examples=probe_examples,
        embedding_target=result.get("embedding_target") or result.get("common_name"),
        confidence=confidence,
    )
    return True


async def _late_frigate_label(row: dict[str, Any],
                              result: dict) -> Optional[tuple[str, Optional[float]]]:
    """Frigate's label for a row that was dispatched unnamed, if one has since landed.

    Two places it can be: on the row (``ingest.apply_frigate_label`` recorded a label
    that arrived while we were pending) or in the service's answer (0.12.0+ echoes the
    ``sub_label`` from the event JSON it fetched — the only channel for a label Frigate
    applied after the event's end message). The latter is persisted so the card can say
    what Frigate said.
    """
    current = await asyncio.to_thread(db.detection_by_ref, row["source"], row["source_ref"])
    label = (current or {}).get("frigate_label")
    score = (current or {}).get("frigate_score")
    if not label and result.get("frigate_label"):
        label, score = result["frigate_label"], result.get("frigate_label_score")
        await asyncio.to_thread(db.set_frigate_label, row["source"], row["source_ref"],
                                label, score)
    label = (label or "").strip()
    if not label or ingest.is_unclassified({"common_name": label}):
        return None
    return label, score


async def _process_confirm(row: dict[str, Any]) -> None:
    """Take a quick second look at a bird Frigate already named, then announce once.

    The service classifies Frigate's own crops of the tracked object (``mode: confirm``,
    no clip), the probe weighs the embedding against the birds confirmed by hand, and
    ``_frigate_verdict`` decides. Anything that stops a verdict — no answer, an error, no
    media — falls back to announcing Frigate's label as-is (``_accept_frigate``).
    """
    ref = row["source_ref"]
    frigate_name = (row.get("common_name") or row.get("frigate_label") or "").strip()
    frigate_score = row.get("frigate_score")
    if not frigate_name or ingest.is_unclassified({"common_name": frigate_name}):
        # Nothing to confirm (the name was reset under us): run the full identification.
        plain = {k: v for k, v in row.items() if k != "_mode"}
        await asyncio.to_thread(db.set_identification, row["source"], ref, "pending")
        await _process(plain)
        return

    priors = await _audio_priors(row)
    exclude = await _exclusions(row)
    result = await _call_service(ref, priors, exclude, None, target=frigate_name,
                                 mode="confirm", frigate_label=frigate_name,
                                 frigate_score=frigate_score)
    if result is None or result.get("status") != "ok":
        await _accept_frigate(row, (result or {}).get("status", "no answer"))
        return

    model = result.get("model_version")
    embed_key = result.get("embedding_key") or db.embedding_key_from(model or "")
    embedding = result.get("embedding")
    service = _slim_candidates(result.get("candidates"))
    await _ensure_probe(embed_key)
    blended = probe.blend(embedding or "", service, embed_key, exclude=set(exclude or []))
    verdict = _frigate_verdict(frigate_name, service, blended)
    probe_examples = blended["probe_examples"] if blended else None
    probe_weight = blended.get("probe_weight") if blended else None

    if await asyncio.to_thread(crops.save, ref, result.get("best_crop")):
        await asyncio.to_thread(db.set_has_crop, ref, True)
    await _store_subjects(row, result, embed_key)

    # Frigate's label stands with Frigate's own confidence in it; an override carries
    # the learned answer's score.
    confidence = (verdict["score"] if verdict["override"]
                  else (frigate_score if frigate_score is not None else verdict["score"]))
    if not await _record_answer(row, result, verdict, embed_key=embed_key,
                                probe_weight=probe_weight, probe_examples=probe_examples,
                                confidence=confidence):
        return
    frigate_note = f" ({frigate_score:.2f})" if frigate_score is not None else ""
    if verdict["override"]:
        log.info(
            "Overrode Frigate on %s: %s%s -> %s (score=%.3f margin=%.3f) on %d confirmed "
            "example(s) of your own, %sms.",
            ref, frigate_name, frigate_note, verdict["name"], verdict["score"],
            verdict["margin"], probe_examples or 0, result.get("elapsed_ms", "?"),
        )
    else:
        theirs = result.get("common_name")
        opinion = ("agrees" if (theirs or "").lower() == frigate_name.lower()
                   else f"would have said {theirs} ({float(result.get('score') or 0):.2f})")
        log.info(
            "Confirmed %s: Frigate said %s%s; aviary-id %s%s — Frigate's label stands "
            "(score=%.3f margin=%.3f, %d frame(s), %sms).",
            ref, frigate_name, frigate_note, opinion,
            f", probe:{probe_examples}" if probe_examples else "",
            verdict["score"], verdict["margin"], result.get("frames_used", 0),
            result.get("elapsed_ms", "?"),
        )


async def _accept_frigate(row: dict[str, Any], reason: str) -> None:
    await asyncio.to_thread(_accept_frigate_sync, row, reason)


def _accept_frigate_sync(row: dict[str, Any], reason: str) -> None:
    """Announce Frigate's label without a confirmation (status ``frigate``).

    The fallback for every way a confirmation can fail to happen: the service is down,
    answered no_media/error, the queue was full, a worker raised. Frigate had a name and
    a confident score; hiding the bird behind "no ID" would be worse than trusting it.
    No embedding is stored, so this can never become a learning example. ↻ on the card
    runs a full identification later.
    """
    ref = row["source_ref"]
    name = row.get("frigate_label") if ingest.is_unclassified(row) else row.get("common_name")
    name = (name or row.get("frigate_label") or "").strip()
    if not name or ingest.is_unclassified({"common_name": name}):
        db.set_identification(row["source"], ref, "failed")
        return
    score = row.get("frigate_score")
    identified = {k: v for k, v in row.items() if k != "_mode"}
    identified["common_name"] = name
    identified["confidence"] = score
    if not ingest.store_row(identified, True, True, False, True):
        # Filtered (blacklisted after the fact): remove the provisional row.
        db.drop_detection(row["source"], ref)
        crops.remove(ref)
        return
    db.set_identification(row["source"], ref, "frigate", None, None, None, None,
                          score is not None, None, confidence=score)
    log.warning("Could not confirm %s (%s); announcing Frigate's %s%s as-is.",
                ref, reason, name, f" ({score:.2f})" if score is not None else "")


def resolve_late_label(det: dict[str, Any], label: str, score: Optional[float]) -> None:
    """Frigate named a bird the identifier had already given up on. Synchronous: called
    from the MQTT thread via ``ingest.apply_frigate_label``.

    ``low_confidence``: the stored embedding is enough to ask the probe whether the
    learned birds disagree — no second GPU pass — and ``_frigate_verdict`` decides as a
    live confirmation would. ``failed``: nothing to weigh against; Frigate's label is
    announced as-is. Either way exactly one announcement, through store_row.
    """
    if not enabled():
        return
    ref = det["source_ref"]
    label = (label or "").strip()
    exclude = _exclusion_names(det)
    if label.lower() in {e.lower() for e in exclude}:
        log.info("Frigate's late label %r for %s was rejected for this bird or is "
                 "blacklisted; leaving it in the review queue.", label, ref)
        return
    row = dict(det)
    row["frigate_label"], row["frigate_score"] = label, score
    det_id = det.get("id")
    if det.get("id_status") != "low_confidence" or not det_id:
        _accept_frigate_sync(row, "identification had failed")
        return
    stored = db.embedding_for(det_id)
    service = _decode_candidates(det.get("id_candidates"))
    blended = None
    if stored:
        model, embedding = stored
        if probe.ready() and probe.model() == model:
            blended = probe.blend(embedding, service, model, exclude=set(exclude))
    verdict = _frigate_verdict(label, service, blended)
    identified = dict(row)
    identified["common_name"] = verdict["name"]
    identified["scientific_name"] = verdict.get("sci")
    identified["species_code"] = verdict.get("code")
    confidence = verdict["score"] if verdict["override"] else (
        score if score is not None else verdict["score"])
    identified["confidence"] = confidence
    if not ingest.store_row(identified, True, True, False, True):
        db.drop_detection(row["source"], ref)
        crops.remove(ref)
        return
    db.set_identification(
        row["source"], ref, "ok", verdict["score"], verdict["margin"], det.get("id_model"),
        None, True, _encode_candidates(verdict["candidates"]),
        probe_weight=blended.get("probe_weight") if blended else None,
        probe_examples=blended["probe_examples"] if blended else None,
        confidence=confidence,
    )
    if verdict["override"]:
        log.info("Frigate later said %s for %s, but %d confirmed example(s) of your own say "
                 "%s (score=%.3f margin=%.3f) — the learned name stands.",
                 label, ref, blended["probe_examples"], verdict["name"],
                 verdict["score"], verdict["margin"])
    else:
        log.info("Frigate later named %s %s%s; the identifier had been unsure — Frigate's "
                 "label stands (runner-up %s).", ref, label,
                 f" ({score:.2f})" if score is not None else "", verdict["runner_up"])


# Old and new "other bird" subjects whose embeddings are at least this alike are the same
# bird re-found on a re-identify, so a human label or rejection on the old one carries
# over. BioCLIP puts the same crop set at ~0.9+; two different birds well below.
_CARRY_SIMILARITY = 0.85


async def _store_subjects(row: dict[str, Any], result: dict, embed_key: str) -> None:
    """Persist the service's ``subjects[]`` for a detection (aviary-id 0.10.0+).

    The primary (idx 0) gets a row for uniform labelling but the detection row remains
    its source of truth. Each OTHER bird is put through the probe and the user's
    thresholds exactly like a primary — no consensus rescue, to keep the rule simple —
    and stored with its own crop and embedding. Labels and rejections the user already
    gave to other birds in this event are carried to whichever new subject matches them.
    Any failure here is logged and swallowed: subjects are a bonus on top of the answer.
    """
    try:
        det_id = row.get("id")
        if not det_id:
            det = await asyncio.to_thread(db.detection_by_ref, row["source"], row["source_ref"])
            det_id = det["id"] if det else None
        if not det_id:
            return
        ref = row["source_ref"]
        subjects = result.get("subjects") or []
        if not subjects:
            # An older service: the whole event is one subject, and the detection row is it.
            subjects = [{
                "idx": 0, "primary": True, "anchored": True,
                "common_name": result.get("common_name"),
                "scientific_name": result.get("scientific_name"),
                "species_code": result.get("species_code"),
                "score": result.get("score"), "margin": result.get("margin"),
                "candidates": result.get("candidates"), "embedding": result.get("embedding"),
                "n_frames": result.get("frames_used"),
            }]
        old = await asyncio.to_thread(db.subjects_for, det_id)
        old_rejections = await asyncio.to_thread(db.subject_rejections, det_id)
        blacklist = set(await asyncio.to_thread(_blacklist_names)) if _settings.identify_exclude_blacklisted else set()
        await asyncio.to_thread(crops.remove_subjects, ref)

        rows: list[dict] = []
        carried: list[tuple[int, int]] = []   # (old idx, new idx)
        for sub in subjects:
            idx = int(sub.get("idx") or 0)
            primary = bool(sub.get("primary")) or idx == 0
            embedding = sub.get("embedding")
            slim = [
                {"name": c.get("common_name") or c.get("name"),
                 "sci": c.get("scientific_name") or c.get("sci"),
                 "code": c.get("species_code") or c.get("code"),
                 "score": float(c.get("score") or 0.0)}
                for c in (sub.get("candidates") or [])
                if isinstance(c, dict) and (c.get("common_name") or c.get("name"))
            ]
            name = sub.get("common_name")
            sci = sub.get("scientific_name")
            code = sub.get("species_code")
            score = float(sub.get("score") or 0.0)
            margin = float(sub.get("margin") or 0.0)
            shortlist = _encode_candidates(sub.get("candidates"))
            status = "ok"
            manual_name = manual_sci = None
            if primary:
                # Mirror of the detection row; the row itself is what the UI reads.
                status = "ok"
            else:
                # Match this bird to the old subjects the user has touched, by embedding.
                match = _match_old_subject(embedding, old, carried)
                rejected = set(n.lower() for n in old_rejections.get(match["idx"], [])) if match else set()
                exclude = rejected | {b.lower() for b in blacklist}
                blended = probe.blend(embedding or "", slim, embed_key, exclude=exclude)
                if blended:
                    name, score, margin = blended["name"], blended["score"], blended["margin"]
                    sci = blended.get("sci") or sci
                    code = blended.get("code") or code
                    shortlist = _encode_candidates(blended["candidates"]) or shortlist
                if name and name.lower() in exclude:
                    # The service's own top answer is one the user ruled out for this bird
                    # (rejections are add-on side; the service's exclude is per call).
                    ranked = [c for c in slim if c["name"].lower() not in exclude]
                    if ranked:
                        name, sci, code = ranked[0]["name"], ranked[0]["sci"], ranked[0]["code"]
                        score = ranked[0]["score"]
                        margin = score - (ranked[1]["score"] if len(ranked) > 1 else 0.0)
                    else:
                        name = None
                passes = bool(name) and score >= _settings.identify_min_score \
                    and margin >= _settings.identify_min_margin
                status = "ok" if passes else "low_confidence"
                if not passes:
                    name = sci = code = None
                if match:
                    carried.append((match["idx"], idx))
                    if match.get("manual_name"):
                        manual_name, manual_sci = match["manual_name"], match.get("manual_sci")
                        status = "manual"
                    elif match.get("id_status") == "rejected" and not passes:
                        status = "rejected"
                await asyncio.to_thread(crops.save, ref, sub.get("best_crop"), idx)
            rows.append({
                "idx": idx, "is_primary": primary, "common_name": name,
                "scientific_name": sci, "species_code": code, "score": score,
                "margin": margin, "candidates": shortlist, "id_status": status,
                "manual_name": manual_name, "manual_sci": manual_sci,
                "embedding_model": embed_key if embedding else None,
                "embedding": embedding,
                "crop_file": crops.basename(ref, idx) if not primary else crops.basename(ref),
                "anchored": sub.get("anchored", True), "n_frames": sub.get("n_frames"),
                # Which species this subject's frame was chosen for: the service's own
                # winner for it unless the request named a target it honoured.
                "embedding_target": (sub.get("embedding_target") or sub.get("common_name")
                                     if embedding else None),
                "purity": sub.get("purity"),
            })
        dropped = [o for o in old if not o.get("is_primary") and (o.get("manual_name")
                   or old_rejections.get(o["idx"])) and o["idx"] not in {c[0] for c in carried}]
        for o in dropped:
            log.info("Re-identify of %s: the other bird labelled %r no longer matches any "
                     "subject; its label was not carried over.", ref,
                     o.get("manual_name") or "(rejections)")
        await asyncio.to_thread(db.replace_subjects, det_id, rows)
        # Rewrite carried rejections onto their new idx, drop the rest.
        await asyncio.to_thread(_remap_rejections, det_id, carried)
        others = [r for r in rows if not r["is_primary"]]
        if others:
            log.info("Event %s: %d other bird(s) in view: %s", ref, len(others),
                     ", ".join(f"{r['manual_name'] or r['common_name'] or 'unidentified'}"
                               f" ({r['id_status']}, {r['score']:.2f})" for r in others))
            # A named other bird is a sighting of ITS species too — maybe its first.
            for r in others:
                if r["id_status"] in ("ok", "manual"):
                    ingest.touch_keepsake(r["manual_name"] or r["common_name"])
    except Exception:  # noqa: BLE001 — never let the bonus break the answer
        log.exception("Storing subjects for %s failed.", row.get("source_ref"))


def _match_old_subject(embedding: Optional[str], old: list[dict],
                       carried: list[tuple[int, int]]) -> Optional[dict]:
    """The old OTHER-bird subject this embedding is the same bird as, if any."""
    if not embedding:
        return None
    vec = probe.decode(embedding)
    if vec is None:
        return None
    taken = {c[0] for c in carried}
    best, best_sim = None, _CARRY_SIMILARITY
    for o in old:
        if o.get("is_primary") or o["idx"] in taken or not o.get("embedding"):
            continue
        if not (o.get("manual_name") or o.get("id_status") == "rejected"):
            continue  # nothing to carry
        ov = probe.decode(o["embedding"])
        if ov is None:
            continue
        sim = float(vec @ ov)
        if sim >= best_sim:
            best, best_sim = o, sim
    return best


def _remap_rejections(det_id: int, carried: list[tuple[int, int]]) -> None:
    """Move rejections (and learning flags) from old subject idx to new; delete those
    nothing carried."""
    with db._connect() as conn:  # noqa: SLF001 — one transaction across the remap
        db._remap_learning_flags(conn, det_id, carried)  # noqa: SLF001
        rows = conn.execute(
            "SELECT idx, species, rejected_at FROM detection_subject_rejections "
            "WHERE detection_id = ?", (det_id,)
        ).fetchall()
        conn.execute("DELETE FROM detection_subject_rejections WHERE detection_id = ?", (det_id,))
        mapping = dict(carried)
        for r in rows:
            new_idx = mapping.get(r["idx"])
            if new_idx is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO detection_subject_rejections "
                "(detection_id, idx, species, rejected_at) VALUES (?, ?, ?, ?)",
                (det_id, new_idx, r["species"], r["rejected_at"]),
            )


def _blacklist_names() -> list[str]:
    return [n for n, _ in db.blacklist_names()]


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
                        zoom: Optional[dict] = None,
                        target: Optional[str] = None,
                        target_subject: int = 0,
                        mode: Optional[str] = None,
                        frigate_label: Optional[str] = None,
                        frigate_score: Optional[float] = None) -> Optional[dict]:
    """POST to the service, retrying once. Returns the parsed body or None.

    ``target`` asks the service (0.11.0+) to choose the returned embedding and best
    crop for THAT species on subject ``target_subject`` rather than for its winner; an
    older service ignores unknown fields, and one that rejects them (422) is retried
    without. ``mode="confirm"`` (0.12.0+) asks for Frigate's crops only, no clip, with
    Frigate's label along for the echo; same fallback.
    """
    if _http.client is None:
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
    if target:
        payload["target"] = target
        payload["target_subject"] = int(target_subject or 0)
    if mode:
        payload["mode"] = mode
    if frigate_label:
        payload["frigate_label"] = frigate_label
        if frigate_score is not None:
            payload["frigate_label_score"] = float(frigate_score)
    url = f"{_settings.identify_url}/identify"
    _newer_fields = ("target", "target_subject", "mode", "frigate_label", "frigate_label_score")

    for attempt in (1, 2):
        try:
            resp = await _http.client.post(url, json=payload, headers=_auth_headers())
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
            if resp.status_code == 422 and any(k in payload for k in _newer_fields):
                # A strict older service that rejects fields it does not know: ask again
                # the way it understands. The answer is then chosen for its own winner,
                # and a confirm request becomes a full identification.
                for key in _newer_fields:
                    payload.pop(key, None)
                continue
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
    if _http.client is None:
        return {"configured": True, "ok": False, "error": "client not initialized"}
    try:
        resp = await _http.client.get(
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
    if not enabled() or _http.client is None:
        return {"ok": False, "species": []}
    try:
        resp = await _http.client.get(f"{_settings.identify_url}/species",
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


_target_support: Optional[tuple[float, bool]] = None


def _version_at_least(version: Any, floor: tuple[int, ...]) -> bool:
    try:
        parts = tuple(int(p) for p in str(version or "").strip().split(".")[:3])
    except ValueError:
        return False
    return bool(parts) and parts >= floor


async def supports_target() -> bool:
    """Whether the service honours ``target`` (aviary-id 0.11.0+). From /healthz, cached.

    Used to choose the SAFE behaviour on an older service — fill a missing embedding,
    never replace one — not to block a user-initiated re-embed, which just reports
    what happened.
    """
    global _target_support
    now = time.monotonic()
    if _target_support and now - _target_support[0] < _PROBE_HEAL_INTERVAL:
        return _target_support[1]
    data = await health()
    ok = bool(data.get("ok")) and _version_at_least(data.get("version"), (0, 11, 0))
    _target_support = (now, ok)
    return ok


_confirm_support: Optional[tuple[float, bool]] = None
# The last answer, readable without awaiting: ingest asks from the MQTT thread.
_confirm_ready = False


async def supports_confirm() -> bool:
    """Whether the service has confirm mode (aviary-id 0.12.0+). From /healthz, cached.

    Also the switch ``confirm_available()`` reads. False while the service is down or
    still loading, which makes "service unreachable → announce Frigate's label at end"
    happen at the ingest step rather than after a queue wait.
    """
    global _confirm_support, _confirm_ready
    now = time.monotonic()
    if _confirm_support and now - _confirm_support[0] < _PROBE_HEAL_INTERVAL:
        return _confirm_support[1]
    data = await health()
    ok = bool(data.get("ok")) and _version_at_least(data.get("version"), (0, 12, 0))
    if _settings and _settings.identify_confirm_frigate:
        if ok and not _confirm_ready:
            log.info("aviary-id %s: Frigate's own bird classifications will be confirmed "
                     "before they are announced.", data.get("version"))
        elif not ok and data.get("ok") and _confirm_support is None:
            log.info("aviary-id %s predates confirm mode (0.12.0); Frigate's own "
                     "classifications are announced as-is.", data.get("version"))
    _confirm_support = (now, ok)
    _confirm_ready = ok
    return ok


def confirm_available() -> bool:
    """Whether an ended, Frigate-named event should be held for confirmation now.

    Synchronous and cheap on purpose — called on the MQTT thread for every such event.
    Off until the first successful health check, so a fresh start announces Frigate's
    labels at end (today's behaviour) rather than parking them.
    """
    return bool(_settings and _settings.identify_confirm_frigate and enabled()
                and _confirm_ready)


async def _capability_loop() -> None:
    while True:
        try:
            await supports_confirm()
        except Exception:  # noqa: BLE001 — a health hiccup must not kill the loop
            log.debug("Capability check failed", exc_info=True)
        await asyncio.sleep(_PROBE_HEAL_INTERVAL)


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


async def reembed(row: dict[str, Any], target: str, subject_idx: int = 0,
                  only_if_missing: bool = False) -> dict:
    """Harvest an embedding of the bird the person NAMED, leaving the label alone.

    The frame a normal identification keeps is the one that best backed the service's
    winner. When a person corrects that answer, it is the wrong frame to learn from — it
    is the one that looked most like the mistake — so this re-runs the event asking the
    service (0.11.0+) to choose the frame for ``target`` instead. The label, status and
    confidence are never touched: the human's answer outranks anything the service says.

    Statuses: ``ok`` (stored, chosen for the target), ``untargeted`` (stored, but chosen
    for the service's winner — an older service, or a name outside its vocabulary; only
    when nothing was stored before), ``kept`` (``only_if_missing`` and one exists),
    ``failed``, ``busy``, ``disabled``, ``not_frigate``, ``no_label``,
    ``subject_not_found``.
    """
    if not enabled():
        return {"status": "disabled"}
    ref, det_id = row.get("source_ref"), row.get("id")
    if not ref or row.get("source") != "frigate" or not det_id:
        return {"status": "not_frigate"}
    target = (target or "").strip()
    if not target or target.lower() == db.UNNAMED:
        return {"status": "no_label"}
    if only_if_missing and subject_idx == 0:
        has, _ = await asyncio.to_thread(db.embedding_target_for, det_id)
        if has:
            return {"status": "kept"}
    mine = None
    if subject_idx:
        old = await asyncio.to_thread(db.subjects_for, det_id)
        mine = next((o for o in old if o["idx"] == subject_idx and not o.get("is_primary")), None)
        if mine is None:
            return {"status": "subject_not_found"}
    with _inflight_lock:
        if ref in _inflight:
            return {"status": "busy"}
        _inflight.add(ref)
    # Same zoom decision as a live identification: the embedding should come from the
    # same footage a live run would have looked at.
    zoom = _zoom_for(row)
    if zoom is not None and not await _zoom_allowed(row):
        zoom = None
    try:
        result = await _call_service(ref, {}, [], zoom, target=target, target_subject=subject_idx)
        if subject_idx and result and result.get("status") == "ok":
            # The service numbers subjects afresh each run; the one we mean is found by
            # its embedding. If it landed under another index, ask once more for THAT one.
            match = _closest_subject(mine.get("embedding"), result.get("subjects"))
            if match is not None and int(match.get("idx") or 0) != subject_idx:
                again = await _call_service(ref, {}, [], zoom, target=target,
                                            target_subject=int(match["idx"]))
                if again and again.get("status") == "ok":
                    result = again
    finally:
        _discard(ref)
    if not result or result.get("status") != "ok":
        service = (result or {}).get("status", "no answer")
        log.debug("Re-embed for %s returned %s.", ref, service)
        return {"status": "failed", "service": service}
    key = (result.get("embedding_key")
           or db.embedding_key_from(result.get("model_version") or ""))
    if not key:
        return {"status": "failed", "service": "no embedding key"}

    if subject_idx == 0:
        embedding = result.get("embedding")
        if not embedding:
            return {"status": "failed", "service": "no embedding"}
        honoured = (result.get("embedding_target") or "").strip()
        if honoured.lower() != target.lower():
            has, _ = await asyncio.to_thread(db.embedding_target_for, det_id)
            if has:
                # Chosen for the service's winner — which is what is already stored.
                return {"status": "untargeted"}
            status, honoured = "untargeted", ""
        else:
            status = "ok"
        await asyncio.to_thread(db.put_detection_embedding, det_id, key, embedding,
                                honoured or None)
        # The thumbnail should be the frame the example came from.
        if await asyncio.to_thread(crops.save, ref, result.get("best_crop")):
            await asyncio.to_thread(db.set_has_crop, ref, True)
        # The same run also found every other bird in the event; keep those too, if this
        # detection has none yet (its label/status are untouched — see above).
        if not await asyncio.to_thread(db.subjects_for, det_id):
            await _store_subjects(dict(row), result, key)
        log.info("Re-embedded %s for %r (%s).", ref, target, status)
        return {"status": status, "target": honoured or None,
                "score": result.get("embedding_target_score")}

    match = _closest_subject(mine.get("embedding"), result.get("subjects"))
    if match is None or not match.get("embedding"):
        return {"status": "subject_not_found"}
    honoured = (match.get("embedding_target") or "").strip()
    if honoured.lower() != target.lower():
        return {"status": "untargeted"}
    crop_file = None
    if await asyncio.to_thread(crops.save, ref, match.get("best_crop"), subject_idx):
        crop_file = crops.basename(ref, subject_idx)
    await asyncio.to_thread(db.set_subject_embedding, det_id, subject_idx, key,
                            match["embedding"], honoured, crop_file)
    log.info("Re-embedded other bird %d of %s for %r.", subject_idx, ref, target)
    return {"status": "ok", "target": honoured, "score": match.get("embedding_target_score")}


def _closest_subject(embedding: Optional[str], subjects: Any) -> Optional[dict]:
    """The service subject (not the primary) whose embedding is the same bird as ours."""
    vec = probe.decode(embedding or "")
    if vec is None:
        return None
    best, best_sim = None, _CARRY_SIMILARITY
    for s in subjects or []:
        if s.get("primary") or not s.get("embedding"):
            continue
        ov = probe.decode(s["embedding"])
        if ov is None or ov.size != vec.size:
            continue
        sim = float(vec @ ov)
        if sim >= best_sim:
            best, best_sim = s, sim
    return best


async def backfill_embedding(row: dict[str, Any]) -> bool:
    """Store an embedding for a manually-labelled detection that never got one.

    Thin wrapper over ``reembed`` that never replaces an existing embedding. True when
    one was stored (chosen for the label where the service can, else for its winner).
    """
    out = await reembed(row, row.get("common_name") or "", only_if_missing=True)
    return out.get("status") in ("ok", "untargeted")


async def reharvest_manual(limit: int = 50) -> dict:
    """Give hand-named detections examples chosen for their label. Returns counts.

    Two passes, newest first because recent events are the ones whose media Frigate
    still has: rows with no embedding at all (an identification that failed, predates
    embeddings, or was just forgotten), then rows whose stored frame was chosen for a
    different species (a relabel from before 0.32.0, or a re-embed that failed) — the
    second only with a service that can choose a frame for a named species. Sequential
    on purpose: this shares one GPU with live identifications and has no deadline.
    """
    out = {"recovered": 0, "retargeted": 0, "tried": 0}
    if not enabled():
        return out
    for row in await asyncio.to_thread(db.manual_rows_missing_embeddings, limit):
        out["tried"] += 1
        try:
            out["recovered"] += bool(await backfill_embedding(row))
        except Exception:
            log.exception("Embedding backfill failed for %s", row.get("source_ref"))
    if await supports_target():
        for row in await asyncio.to_thread(db.manual_rows_untargeted, limit):
            out["tried"] += 1
            try:
                res = await reembed(row, row.get("common_name") or "")
                out["retargeted"] += res.get("status") == "ok"
            except Exception:
                log.exception("Re-embed failed for %s", row.get("source_ref"))
    if out["recovered"] or out["retargeted"]:
        log.info("Re-harvested examples for %d hand-named detection(s) (%d had none, %d were "
                 "chosen for another species); their labels now teach the probe.",
                 out["recovered"] + out["retargeted"], out["recovered"], out["retargeted"])
    return out


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
