"""Normalize Frigate and BirdNET-Go MQTT payloads into ``detections`` rows."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import db, notify

log = logging.getLogger("aviary.ingest")

# When True, detections with no species (generic "bird") are dropped. Configured at
# startup via configure(); applies to both live MQTT ingest and HTTP backfill because
# both store rows through store_row().
_ignore_unclassified = False

# Lowercased Frigate camera names whose detections are never recorded — e.g. a wide
# zone-detection camera pointed at the same feeder as a zoomed classifying camera, whose
# species guesses would otherwise pollute the registry. Same configure()/store_row() path
# as above, so it covers backfill too.
_ignore_cameras: tuple[str, ...] = ()

# Notification state, guarded by one lock because store_row runs on both paho's MQTT
# thread and the backfill asyncio task:
#  - _known_species: species already in the DB (seeded at startup); anything not in
#    here is a "new species".
#  - _announced_refs: detections already announced as an aviary_detection event.
#    Frigate sends several MQTT messages per event (new/update/end) that upsert the
#    same row — announce each detection exactly once. Insertion-ordered dict trimmed
#    to a cap so memory stays bounded.
#  - _tombstones: refs the user deleted; ingest and backfill silently drop these so
#    a removed misclassification can't come back from the source's history.
#  - _blacklist: lowercased names of species the user never wants ingested again.
#    Holds both common and scientific names (see _is_blacklisted).
_known_species: set[str] = set()
_announced_refs: dict[str, None] = {}
_tombstones: set[str] = set()
_blacklist: set[str] = set()
_ANNOUNCED_CAP = 4096
_known_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None

# Set by main() to identify.submit when external identification is configured. Injected
# rather than imported so this module keeps no reference to identify (which imports this
# one), and so tests can substitute a fake.
_identify_hook: Optional[Callable[[dict], bool]] = None

# Set by main() to identify.confirm_available: whether an ended Frigate event that already
# carries Frigate's own species label should be held for aviary-id's confirmation rather
# than announced on the spot. A callable, not a flag, because the answer depends on the
# service version seen at /healthz and on the identify_confirm_frigate option.
_confirm_capable: Optional[Callable[[], bool]] = None

# Set by main() to identify.resolve_late_label: called with (row, label, score) when
# Frigate's label lands for a row the identifier has ALREADY finished with and found
# uncertain (or failed on), so the label can be weighed against what was learned without
# a second GPU pass. Same injection reasoning as _identify_hook.
_late_label_hook: Optional[Callable[[dict, str, Optional[float]], None]] = None

# Set by main() to hastats.on_hook: nudged with "species" when a species' counts may have
# changed and with "zones" when what is in a Frigate zone may have. The publisher
# debounces; this side only says "something moved". Same injection reasoning.
_stats_hook: Optional[Callable[[str], None]] = None

# Set by main() to keepsakes.schedule: called with a species name whenever a live Frigate
# event ends with that species on it, so its first/latest keepsakes can be reconsidered.
# Same injection reasoning as _identify_hook. Never called during backfill — the startup
# pass reconciles history in one sweep instead.
_keepsake_hook: Optional[Callable[[str], None]] = None

# Set by main() to inat.on_new_species when inat_auto_post is on and the confirmation
# gate is off: called with a species name the first time Aviary ever records it live.
# With the gate on, confirmation (the API) is the moment instead. Same injection reasoning.
_new_species_hook: Optional[Callable[[str], None]] = None


def configure(ignore_unclassified: bool, ignore_cameras: tuple[str, ...] = ()) -> None:
    global _ignore_unclassified, _ignore_cameras
    _ignore_unclassified = ignore_unclassified
    _ignore_cameras = tuple(c.strip().lower() for c in ignore_cameras if c.strip())
    if _ignore_cameras:
        log.info("Ignoring detections from cameras: %s", ", ".join(_ignore_cameras))


def set_identify_hook(hook: Optional[Callable[[dict], bool]]) -> None:
    """Route unclassified Frigate detections to external identification."""
    global _identify_hook
    _identify_hook = hook


def set_confirm_capable(check: Optional[Callable[[], bool]]) -> None:
    """Route Frigate-named detections through confirmation when ``check()`` says so."""
    global _confirm_capable
    _confirm_capable = check


def set_late_label_hook(hook: Optional[Callable[[dict, str, Optional[float]], None]]) -> None:
    """Hand a Frigate label that arrived after identification finished to the identifier."""
    global _late_label_hook
    _late_label_hook = hook


def set_keepsake_hook(hook: Optional[Callable[[str], None]]) -> None:
    """Tell the keepsakes module when a species gained a finished camera sighting."""
    global _keepsake_hook
    _keepsake_hook = hook


def set_stats_hook(hook: Optional[Callable[[str], None]]) -> None:
    """Tell the Home Assistant statistics publisher when counts or zones may have moved."""
    global _stats_hook
    _stats_hook = hook


def _stats(kind: str) -> None:
    """Nudge the statistics publisher. Best-effort: it must never break ingest."""
    if _stats_hook is None:
        return
    try:
        _stats_hook(kind)
    except Exception:  # noqa: BLE001
        log.exception("Statistics hook failed (%s)", kind)


def set_new_species_hook(hook: Optional[Callable[[str], None]]) -> None:
    """Tell the life-list module when a species is recorded for the very first time."""
    global _new_species_hook
    _new_species_hook = hook


def touch_keepsake(common_name: Optional[str]) -> None:
    """Fire the keepsake hook for a species named outside the normal ingest path (an
    other-bird subject the identifier named). Best-effort."""
    if not common_name:
        return
    _stats("species")  # a named other-bird is a sighting of its species too
    if _keepsake_hook is None:
        return
    try:
        _keepsake_hook(common_name)
    except Exception:  # noqa: BLE001
        log.exception("Keepsake hook failed for %s", common_name)


def seed_notify_state() -> None:
    """Seed known species (all-time) and recently announced refs from the DB.

    Recent refs are pre-marked announced so an in-progress Frigate event doesn't
    re-notify after an add-on restart.
    """
    with _known_lock:
        # Tracked-bird (or heard) species only: a bird known solely as an other-bird in
        # view still announces as new on its first tracked event.
        _known_species.update(name.lower() for name in db.distinct_species(include_subjects=False))
        for source, ref in db.recent_refs(time.time() - 3600):
            _announced_refs[f"{source}:{ref}"] = None
        _tombstones.update(f"{s}:{r}" for s, r in db.tombstoned_refs())
        for common, sci in db.blacklist_names():
            _blacklist.update(_blacklist_keys(common, sci))
        species, refs = len(_known_species), len(_announced_refs)
        blacklisted = len(_blacklist)
    log.info(
        "Seeded notification state: %d known species, %d recent refs, %d blacklist keys.",
        species, refs, blacklisted,
    )


def add_tombstone(source: str, source_ref: str) -> None:
    """Mark a deleted ref so re-ingest (live or backfill) skips it."""
    with _known_lock:
        _tombstones.add(f"{source}:{source_ref}")


def _blacklist_keys(common_name: str, scientific_name: Optional[str]) -> set[str]:
    """Lowercased match keys for a blacklist entry.

    Both names are keyed because blacklisting purges the species' detections, and
    _canonicalize() maps a scientific-name label to its common name *by querying those
    very rows*. With them gone, a Frigate event labelled with the scientific name would
    otherwise sail past a common-name-only check.
    """
    return {n.strip().lower() for n in (common_name, scientific_name) if n and n.strip()}


def add_blacklist(common_name: str, scientific_name: Optional[str] = None) -> None:
    """Start dropping a species at ingest (live and backfill)."""
    with _known_lock:
        _blacklist.update(_blacklist_keys(common_name, scientific_name))


def remove_blacklist(common_name: str, scientific_name: Optional[str] = None) -> None:
    """Stop dropping a species. Purged history is not restored."""
    with _known_lock:
        _blacklist.difference_update(_blacklist_keys(common_name, scientific_name))


def _is_blacklisted(name: Optional[str]) -> bool:
    if not name:
        return False
    with _known_lock:
        return name.strip().lower() in _blacklist


def forget_species(common_name: str) -> None:
    """Drop a species from the known set (after its last detection was deleted),
    so a genuine future detection announces as a new species again."""
    with _known_lock:
        _known_species.discard(common_name.lower())


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the app's event loop so the MQTT thread can schedule notifications."""
    global _loop
    _loop = loop


def is_unclassified(row: dict) -> bool:
    return (row.get("common_name") or "").strip().lower() == "bird"


def store_row(row: Optional[dict], live: bool = True, announce: bool = True,
              pending_id: bool = False, authoritative: bool = False) -> bool:
    """Upsert a built row unless it's filtered out. Returns True if stored.

    ``authoritative=True`` is for the identifier writing its decision: it may replace a
    species already settled on the row. Source messages and backfill leave that off, so
    a re-import can never undo an identification or a hand label (see
    ``db.upsert_detection``).

    ``live=False`` (backfill) still records the species/refs as seen but never fires
    detection events — historical rows are not news. ``announce=False`` stores the
    row without marking it announced (Frigate in-progress messages: the detection is
    announced once, on the event's ``end`` message, so the notification carries the
    final species/score and the clip exists when tapped).

    ``pending_id=True`` marks a row that is on its way to the identification service and
    skips the unclassified gate ONLY. With Frigate's own classifier turned off every
    detection arrives as generic 'bird', so that gate — which defaults to on — would
    otherwise discard every single one before it ever reached a classifier. Every other
    filter still runs, in the same order: an ignored camera, a deleted ref or a
    blacklisted name is rejected here whether or not identification is pending.
    """
    if row is None:
        return False
    if _ignore_unclassified and is_unclassified(row) and not pending_id:
        log.debug("Skipping unclassified %s detection (%s)", row["source"], row["source_ref"])
        return False
    # Frigate only: a BirdNET-Go node that happens to share a camera's name must not be
    # caught by a camera filter.
    if row.get("source") == "frigate" and (row.get("location") or "").lower() in _ignore_cameras:
        log.debug("Skipping detection from ignored camera %s (%s)",
                  row.get("location"), row["source_ref"])
        return False
    with _known_lock:
        tombstoned = f"{row['source']}:{row['source_ref']}" in _tombstones
    if tombstoned:
        log.debug("Skipping deleted %s detection (%s)", row["source"], row["source_ref"])
        return False
    if _is_blacklisted(row["common_name"]) or _is_blacklisted(row.get("scientific_name")):
        log.debug(
            "Skipping blacklisted species %r (%s %s)",
            row["common_name"], row["source"], row["source_ref"],
        )
        return False
    if not is_unclassified(row):
        _canonicalize(row)
        # Canonicalization can rename an incoming label *into* a blacklisted species
        # (e.g. a scientific-name label resolving to a blacklisted common name), so the
        # check has to run again on the final name.
        if _is_blacklisted(row["common_name"]):
            log.debug(
                "Skipping blacklisted species %r after canonicalization (%s %s)",
                row["common_name"], row["source"], row["source_ref"],
            )
            return False
    db.upsert_detection(row, authoritative=authoritative)
    if live and row.get("source") == "frigate":
        _stats("zones")  # an event started, moved, was named or ended
    if announce:
        _announce(row, live)
        # A finished, named camera sighting may be the species' first or its newest.
        # Best-effort and off the ingest path's critical section — the hook only queues.
        if live and row.get("source") == "frigate" and not is_unclassified(row) \
                and _keepsake_hook is not None:
            try:
                _keepsake_hook(row["common_name"])
            except Exception:  # noqa: BLE001 — keepsakes must never break ingest
                log.exception("Keepsake hook failed for %s", row.get("common_name"))
    return True


def _canonicalize(row: dict) -> None:
    """Unify species naming across sources, in place.

    Frigate's classifier can emit the scientific name (or a different
    capitalization) where BirdNET-Go emits the common name; adopt the canonical
    naming already in the database so one bird doesn't split into two species.
    """
    canon = db.canonical_species(row["common_name"])
    if not canon:
        return
    if canon["common_name"] != row["common_name"]:
        log.debug("Canonicalized species %r -> %r", row["common_name"], canon["common_name"])
        row["common_name"] = canon["common_name"]
    if not row.get("scientific_name") and canon.get("scientific_name"):
        row["scientific_name"] = canon["scientific_name"]


def _announce(row: dict, live: bool) -> None:
    """Fire an aviary_detection event once per classified detection — or, for a Frigate
    event that belongs to a visit, once per species per VISIT.

    An unclassified row is skipped here but NOT marked announced, so a Frigate event
    that gains its ``sub_label`` on a later message gets announced at that point —
    exactly when the species becomes known.

    The visit rule is what stops a bird Frigate tracked as a dozen short objects from
    sending a dozen notifications: the first member identified as a species claims the
    (visit, species) pair in the database, later members of the same species find it
    taken and stay silent, and a *different* species in the same visit still announces.
    Events with no visit (no review item retained, or visits disabled) fall back to the
    per-event rule.
    """
    if is_unclassified(row):  # generic 'bird' is never a species
        return
    name = row["common_name"].lower()  # known-species set is case-insensitive
    key = f"{row['source']}:{row['source_ref']}"
    visit_id = row.get("visit_id") if row.get("source") == "frigate" else None
    if visit_id is not None:
        # Persisted claim, not the in-memory set: identification results land seconds
        # after the event and may straddle a restart. Backfilled rows claim too (without
        # notifying), exactly as they mark themselves announced below.
        if not db.claim_visit_announcement(int(visit_id), row["common_name"], row.get("id")):
            return
    with _known_lock:
        if key in _announced_refs:
            return
        _announced_refs[key] = None
        while len(_announced_refs) > _ANNOUNCED_CAP:
            _announced_refs.pop(next(iter(_announced_refs)))
        is_new = name not in _known_species
        if is_new:
            _known_species.add(name)
    if live:
        _stats("species")  # its counts moved
        _stats("zones")    # and a visit's bird just became known
    # Accepted race: a live detection during a first-run backfill can announce a
    # species/ref the backfill was about to import — genuinely first-seen by Aviary.
    if live and _loop is not None and notify.enabled():
        asyncio.run_coroutine_threadsafe(notify.send_detection(dict(row), is_new=is_new), _loop)
    if live and is_new and _new_species_hook is not None:
        try:
            _new_species_hook(row["common_name"])  # queues only; must never break ingest
        except Exception:  # noqa: BLE001
            log.exception("New-species hook failed for %s", row["common_name"])


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- Frigate

def build_frigate_row(obj: dict) -> Optional[dict]:
    """Build a detections row from a Frigate object.

    Works for both the MQTT event ``after`` object and an object from the
    ``GET /api/events`` HTTP API (the fields we use overlap; the HTTP API also nests the
    score under ``data``). Returns ``None`` for non-bird objects or when there's no id.
    """
    if not isinstance(obj, dict) or obj.get("label") != "bird":
        return None
    event_id = obj.get("id")
    if not event_id:
        return None

    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}

    sub_label = obj.get("sub_label")
    # Frigate's own classification. Over MQTT ``sub_label`` is a [name, score] pair (the
    # score is the classifier's confidence in the NAME, distinct from the object score
    # below); over the events HTTP API it is a plain string with the score under
    # ``data.sub_label_score``. Older builds sent a bare string with no score at all.
    frigate_score: Optional[float] = None
    if isinstance(sub_label, (list, tuple)):
        frigate_score = _as_float(sub_label[1]) if len(sub_label) > 1 else None
        sub_label = sub_label[0] if sub_label else None
    if frigate_score is None:
        frigate_score = _as_float(data.get("sub_label_score"))
    sub_label = (str(sub_label).strip() or None) if sub_label else None
    common_name = sub_label or "bird"
    # First non-None wins (an `or` chain would drop a legitimate 0.0 score).
    confidence = _as_float(_first_not_none(
        obj.get("top_score"),
        obj.get("score"),
        data.get("top_score"),
        data.get("score"),
    ))

    has_clip = 1 if obj.get("has_clip") else 0
    has_snapshot = 1 if obj.get("has_snapshot") else 0

    # Zones the object entered. MQTT uses `entered_zones` (cumulative over the event —
    # the right one) with `current_zones` as the instantaneous fallback; the HTTP events
    # API calls the same cumulative list `zones`. Kept separate from `location` (the
    # camera), which the notification blueprint's camera allowlist filters on.
    zones = (obj.get("entered_zones") or obj.get("zones")
             or obj.get("current_zones") or [])
    zone = ", ".join(str(z) for z in zones if z) or None

    return {
        "source": "frigate",
        "source_ref": str(event_id),
        "common_name": common_name,
        "scientific_name": None,
        "species_code": None,
        "confidence": confidence,
        # Kept apart from common_name, which identification may later overwrite: what
        # Frigate itself said, and how sure it was, stay on the row as provenance.
        "frigate_label": sub_label,
        "frigate_score": frigate_score if sub_label else None,
        "location": obj.get("camera"),
        "zone": zone,
        "start_time": _as_float(obj.get("start_time")) or _now(),
        "end_time": _as_float(obj.get("end_time")),
        "has_clip": has_clip,
        "has_snapshot": has_snapshot,
        # The proxy resolves these refs back to Frigate API URLs using the event id.
        "clip_ref": str(event_id) if has_clip else None,
        "snapshot_ref": str(event_id) if has_snapshot else None,
        "native_id": str(event_id),
        "raw_json": _raw_json(obj),
        "created_at": _now(),
    }


def handle_frigate(payload: bytes) -> None:
    """Handle a ``frigate/events`` message.

    Frigate publishes ``{"type": "new"|"update"|"end", "before": {...}, "after": {...}}``.
    We only track ``bird`` objects and upsert on the event id so the final species (from
    ``sub_label``) and best score win.
    """
    try:
        msg = json.loads(payload)
    except (ValueError, TypeError):
        log.warning("Frigate: could not decode payload")
        return

    after = msg.get("after") or msg.get("before") or {}
    row = build_frigate_row(after)
    if row is None:
        return

    ended = msg.get("type") == "end"
    # A tracked object that ends with neither a clip nor a snapshot is one Frigate
    # itself throws away — its API 404s the event id moments later. These are the
    # seconds-long fly-throughs and wind triggers; there is nothing to identify and
    # nothing a human could review, so keeping the row would only put an unreviewable
    # "no media" card in the queue. Earlier new/update messages may already have stored
    # a provisional row for it — remove that too. drop_detection writes no tombstone on
    # purpose: if Frigate somehow does keep the event, backfill may re-import it, and
    # backfill only ever imports events that have media.
    if ended and not row["has_clip"] and not row["has_snapshot"]:
        db.drop_detection(row["source"], row["source_ref"])
        log.debug("Dropped Frigate event %s: ended with no media (Frigate keeps no "
                  "record of it either).", row["source_ref"])
        _stats("zones")
        return

    if ended and not is_unclassified(row):
        # A second `end` for a row the identifier already has or had (a broker replay, a
        # duplicate publish): the label is provenance now, not a new detection. Routing
        # it through store_row would re-upsert and — for a row awaiting confirmation —
        # announce Frigate's label ahead of the answer. One indexed lookup, on ended
        # named messages only.
        existing = db.detection_by_ref(row["source"], row["source_ref"])
        if existing is not None and existing.get("id_status"):
            apply_frigate_label(row["source_ref"], row["frigate_label"] or row["common_name"],
                                row.get("frigate_score"), existing)
            return
        if _identify_hook is not None and (_is_blacklisted(row["common_name"])
                                           or _is_blacklisted(row.get("scientific_name"))):
            # Frigate named a species you have blacklisted as one classifiers get wrong.
            # Dropping the event (what store_row would do) throws away a bird that is
            # most likely something else; identifying it with that species ruled out
            # (identify_exclude_blacklisted) names it properly. Frigate's label stays on
            # the row as provenance; the species goes back to unnamed for the run.
            log.info("Frigate labelled %s as blacklisted %r; identifying it instead.",
                     row["source_ref"], row["common_name"])
            row["common_name"] = "bird"
            row["scientific_name"] = None
            _dispatch_for_identification(row)
            return
        if _identify_hook is not None and _confirm_capable is not None and _confirm_capable():
            # Frigate named it and aviary-id can take a quick second look at Frigate's
            # crop: hold the announcement until it has. See _dispatch_for_confirmation.
            _dispatch_for_confirmation(row)
            return

    # An ended event with no species, and somewhere to send it: hand it to the
    # identification service instead of announcing or discarding it. Only on `end` —
    # in-progress messages would mean paying for a GPU pass on a bird that is still
    # moving through the frame, several times per event.
    if ended and is_unclassified(row) and _identify_hook is not None:
        _dispatch_for_identification(row)
        return

    # Announce only when the event ends: the species/score are final and Frigate has
    # (or is about to have) the finished clip for the notification's tap action.
    if store_row(row, announce=ended):
        log.debug("Frigate detection upserted: %s (%s)", row["common_name"], row["source_ref"])


def handle_frigate_object_update(payload: bytes) -> None:
    """Handle a ``frigate/tracked_object_update`` message.

    Only ``{"type": "classification", "id", "sub_label", "score", ...}`` matters here: a
    species Frigate's classifier settled on for a tracked object, published on its own
    rather than inside the object's event messages. When it arrives after the event's
    ``end`` — which the events topic can no longer carry — this is the only way to hear
    about it. Everything else on the topic (faces, plates, descriptions) is ignored.
    """
    try:
        msg = json.loads(payload)
    except (ValueError, TypeError):
        log.warning("Frigate object update: could not decode payload")
        return
    if not isinstance(msg, dict) or msg.get("type") != "classification":
        return
    ref, label = msg.get("id"), msg.get("sub_label")
    if isinstance(label, (list, tuple)):
        label = label[0] if label else None
    if not ref or not label:
        return
    outcome = apply_frigate_label(str(ref), str(label), _as_float(msg.get("score")))
    log.debug("Frigate classification %r for %s (score %s): %s.",
              label, ref, msg.get("score"), outcome)


def apply_frigate_label(source_ref: str, label: Optional[str], score: Optional[float],
                        existing: Optional[dict] = None) -> str:
    """Fold a Frigate label that arrived AFTER the row's `end` was handled into the row.

    Frigate applies a sub_label the moment its classifier clears the threshold, which
    can be on any message of the object's life — or after it. Every ordering must leave
    exactly one row and one announcement per (visit, species), so what happens depends
    on where the row is:

    * ``pending`` / ``confirming`` — the identifier has not answered yet. The label is
      recorded on the row and ``identify._process`` reads it before deciding.
    * ``low_confidence`` / ``failed`` — it answered and could not name the bird. Frigate
      now can: the label is weighed against the learned birds using the embedding
      already stored (no second GPU pass) and the result announced, once.
    * ``ok`` / ``manual`` / ``frigate`` — the species is settled. Provenance only; a
      disagreement is logged, never re-announced or renamed.
    * no status — identification never touched this row; nothing to reconcile.

    Returns what was done, for the caller's log line.
    """
    label = (label or "").strip()
    if not label or is_unclassified({"common_name": label}):
        return "ignored"
    det = existing or db.detection_by_ref("frigate", source_ref)
    if det is None:
        return "ignored"  # a filtered or dropped event, or one Aviary never saw
    db.set_frigate_label("frigate", source_ref, label, score)
    status = det.get("id_status")
    if status in ("pending", "confirming"):
        return "recorded"
    if status in ("low_confidence", "failed"):
        if _late_label_hook is None:
            return "recorded"
        try:
            _late_label_hook(dict(det), label, score)
        except Exception:  # noqa: BLE001 — never let this break the MQTT loop
            log.exception("Resolving Frigate's late label for %s failed.", source_ref)
            return "recorded"
        return "resolved"
    if status in ("ok", "manual", "frigate"):
        if (det.get("common_name") or "").strip().lower() != label.lower():
            log.info("Frigate later said %r for %s; keeping %s (%s).",
                     label, source_ref, det.get("common_name"), status)
        return "recorded"
    return "recorded"


def build_review_row(obj: dict) -> Optional[dict]:
    """Build a visits row from a Frigate review item — the MQTT ``after`` object or an
    item from ``GET /api/review`` (same shape). None unless a bird was involved and the
    camera is not ignored.

    ``refs`` is the item's ``data.detections`` — the tracked-object ids Frigate grouped
    under it, which is the whole point: those are the events that become one visit.
    """
    if not isinstance(obj, dict):
        return None
    review_id = obj.get("id")
    camera = obj.get("camera")
    start = _as_float(obj.get("start_time"))
    if not review_id or not camera or start is None:
        return None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    objects = {str(o).strip().lower() for o in (data.get("objects") or []) if o}
    if "bird" not in objects:
        return None
    if str(camera).strip().lower() in _ignore_cameras:
        return None
    zones = ", ".join(str(z) for z in (data.get("zones") or []) if z) or None
    return {
        "review_id": str(review_id),
        "camera": str(camera).strip().lower(),
        "start_time": start,
        "end_time": _as_float(obj.get("end_time")),
        "severity": obj.get("severity"),
        "zones": zones,
        "raw_json": _raw_json(obj),
        "refs": [str(d) for d in (data.get("detections") or []) if d],
    }


def handle_frigate_review(payload: bytes) -> None:
    """Handle a ``frigate/reviews`` message: ``{"type": new|update|end, "before", "after"}``.

    Every message re-stamps the item's full member list (Frigate appends to it as more
    objects join the activity), and ``end`` carries the final bounds. Nothing here is
    timed or inferred: the visit is exactly Frigate's review item.
    """
    try:
        msg = json.loads(payload)
    except (ValueError, TypeError):
        log.warning("Frigate reviews: could not decode payload")
        return
    after = msg.get("after") or msg.get("before") or {}
    row = build_review_row(after)
    if row is None:
        return
    visit_id = db.upsert_visit(row)
    _stats("zones")  # a visit opened, grew or closed
    log.debug("Visit %s (%s) %s: review %s, %d member(s)%s", visit_id, row["camera"],
              msg.get("type"), row["review_id"], len(row["refs"]),
              "" if row["end_time"] is None else ", ended")


def _dispatch_for_identification(row: dict) -> None:
    """Store an unclassified Frigate row as pending and queue it for identification.

    Stored before it is queued, deliberately: if the add-on dies between the two, a row
    marked ``pending`` in the database is recoverable at the next start
    (``identify.requeue_pending``), whereas an event that was only ever in memory is
    gone for good.
    """
    if not store_row(row, announce=False, pending_id=True):
        return  # filtered — ignored camera, deleted ref, or blacklisted
    db.set_identification(row["source"], row["source_ref"], status="pending")
    if not _identify_hook(row):
        log.debug("Identification not queued for %s (already in flight or queue full).",
                  row["source_ref"])


def _dispatch_for_confirmation(row: dict) -> None:
    """Store a Frigate-named row as ``confirming`` and queue it for a second look.

    The row is stored named — Frigate's label shows on the card straight away — but NOT
    announced: that happens once when the identifier has either let the label stand or
    overridden it (``identify._process_confirm``), or, if it cannot be reached, with
    Frigate's label as-is. Stored before it is queued for the same restart-recovery
    reason as ``_dispatch_for_identification``.
    """
    if not store_row(row, announce=False):
        return  # filtered — ignored camera, deleted ref, or blacklisted
    # store_row canonicalized the name; keep the canonical form as the label the
    # decision compares against and the UI reports ("Frigate said ...").
    row["frigate_label"] = row["common_name"]
    db.set_identification(row["source"], row["source_ref"], status="confirming")
    row["_mode"] = "confirm"
    if not _identify_hook(row):
        log.debug("Confirmation not queued for %s (already in flight or queue full).",
                  row["source_ref"])


# ------------------------------------------------------------------------- BirdNET-Go

def build_birdnet_row(msg: dict) -> Optional[dict]:
    """Build a detections row from a BirdNET-Go detection.

    Expects the MQTT payload shape: PascalCase ``datastore.Note`` keys (``CommonName``,
    ``ScientificName``, ``SpeciesCode``, ``Confidence``, ``ClipName``, ``BeginTime``,
    ``EndTime``, ``Date``, ``Time``, ``SourceNode``) plus the camelCase ``detectionId``
    (the database id, present on current builds; older ones sent ``ID``). The HTTP-API
    backfill maps its camelCase response into this shape first (see
    ``backfill.birdnet_msg_from_api``), so both paths produce identical ``source_ref``
    values and dedupe cleanly.
    """
    if not isinstance(msg, dict):
        return None

    common_name = msg.get("CommonName") or "bird"
    start_time = _birdnet_epoch(msg) or _now()

    # The database id arrives as camelCase `detectionId` on current BirdNET-Go builds
    # (top-level `ID` was removed from the MQTT payload; on v0.6.4 it was always 0).
    # The HTTP backfill maps its `id` into `ID`. Both therefore key identically here,
    # so live and backfilled rows dedupe. Without an id, fall back to species+second,
    # which collapses duplicate publishes of the same detection.
    raw_id = _first_not_none(msg.get("detectionId"), msg.get("ID"))
    has_native_id = raw_id not in (None, 0, "", "0")
    source_ref = (
        f"{raw_id}-{int(start_time)}" if has_native_id
        else f"{common_name}-{int(start_time)}"
    )

    # Audio clip locator: BirdNET-Go payloads may include a clip filename under one of a
    # few keys depending on version. Store whatever is present; the proxy resolves it
    # against birdnet_url. With a native id the by-id audio endpoint works even
    # without a filename.
    clip_ref = _first(msg, "ClipName", "Clip", "File", "InputFile")
    has_clip = 1 if (clip_ref or has_native_id) else 0

    return {
        "source": "birdnet",
        "source_ref": source_ref,
        "common_name": common_name,
        "scientific_name": msg.get("ScientificName"),
        "species_code": msg.get("SpeciesCode"),
        "confidence": _as_float(msg.get("Confidence")),
        "location": _first(msg, "SourceNode", "sourceName"),
        "start_time": start_time,
        "end_time": _birdnet_epoch(msg, key="EndTime"),
        "has_clip": has_clip,
        "has_snapshot": 0,
        "clip_ref": clip_ref,
        "snapshot_ref": None,
        # BirdNET-Go's own database id — lets the media proxy use the by-id API
        # endpoints (audio/spectrogram) instead of guessing clip file paths.
        "native_id": str(raw_id) if has_native_id else None,
        "raw_json": _raw_json(msg),
        "created_at": _now(),
    }


def handle_birdnet(payload: bytes) -> None:
    """Handle a BirdNET-Go detection message on the ``birdnet`` topic."""
    try:
        msg = json.loads(payload)
    except (ValueError, TypeError):
        log.warning("BirdNET-Go: could not decode payload")
        return
    row = build_birdnet_row(msg)
    if store_row(row):
        log.debug("BirdNET detection stored: %s (%s)", row["common_name"], row["source_ref"])


# ----------------------------------------------------------------------------- helpers

def _first_not_none(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def _raw_json(obj: Any, limit: int = 8000) -> Optional[str]:
    """Serialize the source payload for debugging; never store truncated/invalid JSON.

    Oversized payloads (e.g. Frigate events with bulky path/region data) get their
    largest values dropped until the document fits; if it still doesn't fit, store
    nothing rather than a sliced document.
    """
    try:
        text = json.dumps(obj)
    except (TypeError, ValueError):
        return None
    if len(text) <= limit:
        return text
    if isinstance(obj, dict):
        slim = dict(obj)
        # Drop the largest values first until it fits.
        for k in sorted(slim, key=lambda k: len(str(slim[k])), reverse=True):
            slim.pop(k)
            text = json.dumps(slim)
            if len(text) <= limit:
                return text
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(msg: dict, *keys: str) -> Optional[str]:
    for k in keys:
        v = msg.get(k)
        if v:
            return str(v)
    return None


def _birdnet_epoch(msg: dict, key: str = "BeginTime") -> Optional[float]:
    """Derive epoch seconds from BirdNET-Go's timestamp fields.

    Prefers the ISO ``BeginTime``/``EndTime``; falls back to combining the ``Date`` and
    ``Time`` fields (assumed local time).
    """
    iso = msg.get(key)
    if iso:
        parsed = _parse_iso(str(iso))
        if parsed is not None:
            return parsed
    if key == "BeginTime":
        date = msg.get("Date")
        clock = msg.get("Time")
        if date and clock:
            try:
                dt = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S")
                return dt.timestamp()
            except ValueError:
                return None
    return None


def _parse_iso(value: str) -> Optional[float]:
    # BirdNET-Go emits RFC3339 with offset, e.g. 2024-04-06T08:23:48.736132138+11:00.
    # Python's fromisoformat handles offsets but not nanosecond precision, so trim to 6.
    v = value.strip()
    if v in ("", "0001-01-01T00:00:00Z"):
        return None
    v = v.replace("Z", "+00:00")
    if "." in v:
        head, _, tail = v.partition(".")
        frac = tail
        offset = ""
        for sign in ("+", "-"):
            idx = tail.find(sign)
            if idx > 0:
                frac, offset = tail[:idx], tail[idx:]
                break
        frac = frac[:6]
        v = f"{head}.{frac}{offset}"
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None
