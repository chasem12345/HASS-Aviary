"""Species keepsakes: keep the FIRST and the LATEST camera sighting of every species.

Frigate expires footage on a schedule that knows nothing about birds, so the one picture
of the Robin that first turned up in March is gone by April, and a species that stopped
visiting has no picture at all. This module keeps two events per species at Frigate,
automatically:

* **first** — the species' earliest sighting Frigate still has. Pinned once and left
  alone; if the true first is already past retention when this runs, the earliest
  *surviving* one is taken (a binary search over the timeline, since expiry is monotonic
  in time) and the row says so through its ``start_time``.
* **latest** — the newest sighting, re-pinned at most **once per local day**: a bird
  seen forty times a day costs one Frigate round-trip a day, and the previous "latest"
  is released so a species never holds more than two events. A species whose newest
  sighting IS its first has no separate latest.

"Keep" means ``retain_indefinitely`` on the event (row, snapshot and thumbnail survive)
plus — with ``keepsake_video`` — an **export** of the padded clip, because since Frigate
0.14 the retain flag no longer protects the recording segments behind a clip; an export
is the one form of footage Frigate never expires. Exports go through the same
primitives as 📌 keep (``kept.py``). A species that was only "also in view" in another
bird's event keeps that event (``subject_idx`` names the bird), which is also exactly
the footage that shows it.

Everything here is derived from the data and idempotent: ``reconcile(name)`` computes
the desired state from the sightings and moves Frigate towards it, so the triggers (an
event ending, a relabel, a delete, startup) need no bookkeeping of their own and a
missed trigger costs nothing but latency. The user's own 📌 pin is independent —
Frigate's flag is held while EITHER wants it — and with ``require_species_confirmation``
on, only confirmed species get keepsakes (a misclassification should not be enshrined).
Soft-failure rule applies: nothing in here may break ingest or a page render.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date
from typing import Optional

from . import db, kept

log = logging.getLogger("aviary.keepsakes")

ROLES = ("first", "latest")

# A visit is many events ending seconds apart; coalesce their triggers into one pass.
_DEBOUNCE_S = 5.0
# Safety-net sweep: anything a trigger missed (or a Frigate hiccup left undone) is
# caught here. Cheap when nothing changed — two SQL lookups per species, no HTTP.
_PERIOD_S = 6 * 3600
# Below this many sightings the surviving-first search just walks from the oldest.
_LINEAR_PROBE_MAX = 8
# Breather between species on the startup sweep, so a first run over a whole registry
# does not fire a hundred export jobs at Frigate in one second.
_PACE_S = 1.0

_settings = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_pending: set[str] = set()
_pending_lock = threading.Lock()
_drain_task: Optional[asyncio.Task] = None
_reconcile_lock: Optional[asyncio.Lock] = None


def configure(settings) -> None:
    global _settings
    _settings = settings


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the app loop so the MQTT thread's triggers can be scheduled onto it."""
    global _loop
    _loop = loop


def enabled() -> bool:
    return bool(_settings and getattr(_settings, "keepsakes", False)
                and getattr(_settings, "frigate_url", ""))


def _lock() -> asyncio.Lock:
    global _reconcile_lock
    if _reconcile_lock is None:
        _reconcile_lock = asyncio.Lock()
    return _reconcile_lock


# ------------------------------------------------------------------------ triggers

def schedule(name: Optional[str]) -> None:
    """Queue a species for a reconcile pass shortly. Thread-safe; never raises."""
    if not enabled() or not name or name.strip().lower() == db.UNNAMED:
        return
    with _pending_lock:
        _pending.add(name.strip())
    loop = _loop
    if loop is None or loop.is_closed():
        return  # tests, or before start-up: the sweep picks it up
    try:
        loop.call_soon_threadsafe(_ensure_drain)
    except RuntimeError:
        pass  # loop shutting down


def _ensure_drain() -> None:
    global _drain_task
    if _drain_task is None or _drain_task.done():
        _drain_task = asyncio.get_running_loop().create_task(_drain())


async def _drain() -> None:
    await asyncio.sleep(_DEBOUNCE_S)
    while True:
        with _pending_lock:
            names = sorted(_pending)
            _pending.clear()
        if not names:
            return
        for name in names:
            await reconcile(name)


async def reconcile(name: str, deep: bool = False) -> dict:
    """Bring one species' keepsakes in line with its sightings. Never raises.

    ``deep`` also asks whether an existing "first" could be replaced by an even older
    sighting that has since appeared (a backfill importing history). That is a
    binary search over the timeline, so it runs on the start-up sweep only — the
    everyday triggers have no way to make an older sighting appear.
    """
    stats = _Stats()
    if not enabled():
        return stats.as_dict()
    async with _lock():
        try:
            await _reconcile(name, stats, deep)
        except Exception:  # noqa: BLE001 — a keepsake is never worth a failed page or ingest
            log.exception("Keepsake reconcile failed for %s", name)
    return stats.as_dict()


# ------------------------------------------------------------------------ the pass

class _Stats:
    def __init__(self) -> None:
        self.pinned = 0
        self.released = 0
        self.exports = 0
        self.export_failed = 0
        self.frigate_calls = 0

    def as_dict(self) -> dict:
        return dict(pinned=self.pinned, released=self.released, exports=self.exports,
                    export_failed=self.export_failed, frigate_calls=self.frigate_calls)

    def add(self, other: "_Stats") -> None:
        for k in ("pinned", "released", "exports", "export_failed", "frigate_calls"):
            setattr(self, k, getattr(self, k) + getattr(other, k))


def _gated_out(name: str) -> bool:
    return bool(getattr(_settings, "require_species_confirmation", False)
                and not db.is_species_confirmed(name))


def _same_or_earlier_day(newer: float, older: float) -> bool:
    return date.fromtimestamp(newer) <= date.fromtimestamp(older)


async def _reconcile(name: str, stats: _Stats, deep: bool = False) -> None:
    have = await asyncio.to_thread(db.keepsakes_for_species, name)
    if await asyncio.to_thread(_gated_out, name):
        # Not (or no longer) in the registry: a keepsake would enshrine a possible
        # misclassification. Release anything held from before it was unconfirmed.
        for role in ROLES:
            if role in have:
                await _release(name, role, stats)
        return

    # A keepsake whose event no longer features this species (deleted, relabelled, the
    # subject rejected) is stale: let it go and choose again.
    for role in ROLES:
        row = have.get(role)
        if row is None:
            continue
        still = await asyncio.to_thread(db.sighting_by_detection, row["detection_id"], name)
        if still is None:
            await _release(name, role, stats)
            have.pop(role, None)

    oldest = await asyncio.to_thread(db.oldest_sighting, name)
    if oldest is None:
        # Nothing on camera any more (audio-only, or every event deleted).
        for role in ROLES:
            if role in have:
                await _release(name, role, stats)
        return

    # ---- first: the earliest sighting Frigate still has.
    first = have.get("first")
    if first is None or (deep and oldest["id"] != first["detection_id"]
                         and oldest["start_time"] < first["start_time"]):
        before = first["start_time"] if first else None
        winner = await _oldest_surviving(name, before, stats)
        if winner is not None and (first is None or winner["id"] != first["detection_id"]):
            if await _pin(name, "first", winner, stats):
                if first is not None:
                    await _release_event(name, first, stats)
                first = await asyncio.to_thread(db.keepsake, name, "first")

    # ---- latest: the newest sighting, moved at most once a local day.
    newest = await asyncio.to_thread(db.newest_sighting, name)
    latest = have.get("latest")
    if newest is None or (first is not None and newest["id"] == first["detection_id"]):
        # The newest sighting is the first itself: one keepsake covers both.
        if latest is not None:
            await _release(name, "latest", stats)
    elif latest is None or (newest["id"] != latest["detection_id"]
                            and not _same_or_earlier_day(newest["start_time"], latest["start_time"])):
        if await _pin(name, "latest", newest, stats):
            if latest is not None:
                await _release_event(name, latest, stats)

    # ---- exports: the padded clip(s), once per keepsake.
    if getattr(_settings, "keepsake_video", False):
        for role in ROLES:
            row = await asyncio.to_thread(db.keepsake, name, role)
            if row is not None and row.get("export_tried_at") is None:
                await _export(name, role, row, stats)


async def _oldest_surviving(name: str, before: Optional[float], stats: _Stats) -> Optional[dict]:
    """The earliest sighting (optionally strictly before ``before``) Frigate still has.

    Expiry is monotonic in time, so the timeline reads gone…gone, kept…kept and a binary
    search finds the boundary in ~log2(n) probes. A user's old 📌 pin breaks strict
    monotonicity but only ever makes the answer *earlier*, which is the right direction.
    None when Frigate has none of them, or could not be asked.
    """
    rows = await asyncio.to_thread(db.sightings_timeline, name)
    if before is not None:
        rows = [r for r in rows if r["start_time"] < before]
    if not rows:
        return None

    async def exists(i: int) -> Optional[bool]:
        stats.frigate_calls += 1
        return await kept.frigate_has_event(_settings, rows[i]["source_ref"])

    if len(rows) <= _LINEAR_PROBE_MAX:
        for i in range(len(rows)):
            ok = await exists(i)
            if ok is None:
                return None
            if ok:
                return await _full(rows[i], name)
        return None

    last = await exists(len(rows) - 1)
    if not last:
        return None  # even the newest candidate is gone (or Frigate unreachable)
    lo, hi = 0, len(rows) - 1  # invariant: rows[hi] exists
    while lo < hi:
        mid = (lo + hi) // 2
        ok = await exists(mid)
        if ok is None:
            return None
        if ok:
            hi = mid
        else:
            lo = mid + 1
    return await _full(rows[hi], name)


async def _full(slim: dict, name: str) -> Optional[dict]:
    """The full sighting row for a timeline entry (as this species)."""
    return await asyncio.to_thread(db.sighting_by_detection, slim["id"], name)


async def _pin(name: str, role: str, sighting: dict, stats: _Stats) -> bool:
    stats.frigate_calls += 1
    err = await kept.set_frigate_retain(_settings, sighting["source_ref"], True)
    if err is not None:
        level = log.info if err == kept.GONE else log.warning
        level("Keepsake %s for %s: could not retain event %s at Frigate: %s",
              role, name, sighting["source_ref"], err)
        return False
    await asyncio.to_thread(
        db.set_keepsake, name, role, sighting["id"], sighting.get("subject_idx") or 0,
        sighting["start_time"],
    )
    stats.pinned += 1
    log.info("Keepsake %s for %s: kept event %s (%s).", role, name, sighting["source_ref"],
             kept.export_stamp(sighting["start_time"]))
    return True


async def _release(name: str, role: str, stats: _Stats) -> None:
    """Drop a keepsake row and release what it held at Frigate."""
    row = await asyncio.to_thread(db.clear_keepsake, name, role)
    if row is not None:
        await _release_event(name, row, stats)


async def _release_event(name: str, row: dict, stats: _Stats) -> None:
    """Release a former keepsake at Frigate: its exports always; the retain flag only
    if nothing else still holds it (the user's 📌, or another keepsake — the same event
    can be another species' first, or this species' first after a latest rotation)."""
    for export_id in (row.get("export_id"), row.get("zoom_export_id")):
        if export_id:
            stats.frigate_calls += 1
            err = await kept.delete_kept_export(export_id, _settings)
            if err:
                log.info("Keepsake export %s for %s: %s", export_id, name, err)
    det_id = row["detection_id"]
    det = await asyncio.to_thread(db.detection_by_id, det_id)
    if det is not None:
        if det.get("retained_at") is not None or await asyncio.to_thread(db.keepsake_roles, det_id):
            stats.released += 1
            return
        source_ref = det["source_ref"]
    else:
        source_ref = row.get("source_ref")
    if source_ref:
        stats.frigate_calls += 1
        err = await kept.set_frigate_retain(_settings, source_ref, False)
        if err and err != kept.GONE:
            log.info("Keepsake release of %s for %s: %s", source_ref, name, err)
    stats.released += 1


async def _export(name: str, role: str, row: dict, stats: _Stats) -> None:
    """Export the padded clip of a keepsake: the event camera's window, and the paired
    camera's when there is one. Each is requested only while missing, so a retry after
    a partial failure never duplicates the half that worked."""
    det = await asyncio.to_thread(db.detection_by_id, row["detection_id"])
    if not det or not det.get("end_time") or not det.get("location") \
            or det["end_time"] <= det["start_time"]:
        await asyncio.to_thread(db.set_keepsake_exports, name, role,
                                row.get("export_id"), row.get("zoom_export_id"), "not applicable")
        return
    start, end = kept.padded_window(det["start_time"], det["end_time"], kept.view_pad(_settings))
    end = min(end, start + kept.RECORDING_MAX_S)
    label = f"Aviary · {name} · {role} sighting · {kept.export_stamp(det['start_time'])}"
    camera = (det["location"] or "").strip().lower()
    statuses: list[str] = []

    export_id = row.get("export_id")
    if not export_id:
        stats.frigate_calls += 1
        export_id, status = await kept.export_window(_settings, camera, start, end, label)
        statuses.append(status)
    zoom_id = row.get("zoom_export_id")
    other = _settings.camera_pairs.get(camera) if hasattr(_settings, "camera_pairs") else None
    if other and not zoom_id:
        stats.frigate_calls += 1
        zoom_id, zstatus = await kept.export_window(_settings, other, start, end, label + " · zoomed")
        statuses.append(f"zoomed {zstatus}")

    queued = sum(1 for s in statuses if "queued" in s)
    failed = len(statuses) - queued
    stats.exports += queued
    stats.export_failed += failed
    summary = "; ".join(statuses) if statuses else "not applicable"
    if failed:
        log.info("Keepsake %s for %s: export %s", role, name, summary)
    await asyncio.to_thread(db.set_keepsake_exports, name, role, export_id, zoom_id, summary)


# --------------------------------------------------------------- deletes & removals

async def on_detection_deleted(det: dict) -> None:
    """A detection was deleted: release the keepsakes that sat on it and re-pick.

    ``det["keepsakes"]`` are the rows ``db.delete_detection`` removed in the same
    transaction; their exports still exist at Frigate. The event's retain flag is
    released unconditionally — the user's pin died with the row.
    """
    if not enabled():
        return
    stats = _Stats()
    rows = det.get("keepsakes") or []
    for row in rows:
        row = dict(row, source_ref=det.get("source_ref"))
        await _release_event(det.get("common_name") or "", row, stats)
    for name in {r["common_name"] for r in rows} | {det.get("common_name") or ""}:
        schedule(name)


async def forget(name: str) -> None:
    """The species is being removed: drop its keepsakes and release them at Frigate."""
    if not enabled():
        return
    rows = await asyncio.to_thread(db.forget_keepsakes, name)
    stats = _Stats()
    for row in rows:
        await _release_event(name, row, stats)


# ------------------------------------------------------------------------ startup

async def reconcile_all(deep: bool = False) -> dict:
    """One sweep over every species (and every species holding a keepsake)."""
    total = _Stats()
    if not enabled():
        return total.as_dict()
    names = await asyncio.to_thread(db.distinct_species)
    held = await asyncio.to_thread(db.keepsake_species)
    seen = {n.lower() for n in names}
    names = list(names) + [n for n in held if n.lower() not in seen]
    for name in names:
        if name.lower() == db.UNNAMED:
            continue
        result = await reconcile(name, deep=deep)
        total.pinned += result["pinned"]
        total.released += result["released"]
        total.exports += result["exports"]
        total.export_failed += result["export_failed"]
        total.frigate_calls += result["frigate_calls"]
        if result["frigate_calls"]:
            await asyncio.sleep(_PACE_S)
    if total.frigate_calls:
        log.info(
            "Keepsakes: pinned %d and released %d event(s) across %d species; "
            "%d export(s) queued, %d window(s) had no recordings left.",
            total.pinned, total.released, len(names), total.exports, total.export_failed,
        )
    return total.as_dict()


async def run(after: Optional[asyncio.Task] = None) -> None:
    """Background task: sweep once start-up settles, then every ``_PERIOD_S``.

    Waits for the backfill (when one runs) so imported history is in place before the
    first sweep decides which sighting is a species' first.
    """
    if not enabled():
        return
    if after is not None:
        try:
            await after
        except Exception:  # noqa: BLE001 — the backfill logs its own failures
            pass
    # Let the identification workers and Frigate settle before the first sweep.
    await asyncio.sleep(20)
    deep = True  # the start-up sweep is the one that may see newly imported history
    while True:
        started = time.monotonic()
        try:
            await reconcile_all(deep=deep)
        except Exception:  # noqa: BLE001
            log.exception("Keepsake sweep failed; will retry next period.")
        deep = False
        await asyncio.sleep(max(60.0, _PERIOD_S - (time.monotonic() - started)))
