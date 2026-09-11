"""Media audit: stop believing in footage Frigate has already expired.

``detections.has_clip`` / ``has_snapshot`` are written once, from the MQTT payload, and
never re-checked — so a species with no visit inside Frigate's retention still renders
players, ▶ buttons and thumbnail URLs that 404. This pass asks Frigate which bird events
it still has (one paged listing, the same call the backfill uses) and brings our flags
in line: an event Frigate no longer lists, or lists with both flags off, is marked
**expired** (flags zeroed, ``media_expired_at`` stamped); one it lists with a single
flag off is synced; one we had stamped but Frigate lists with media again is restored.

Rows are never deleted: history is not footage. The feed hides expired events that have
no crop of their own (``db.feed_page``); every aggregate, the recap and the direct pages
still see them.

Safety rule: only a COMPLETE listing is acted on. A transport error or an error page
mid-way means we cannot tell "gone" from "not seen yet", so nothing changes. Likewise an
empty listing while we ingested Frigate events today is treated as a misconfiguration,
not as everything having expired.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional
from urllib.parse import urlencode

from . import db, proxy

log = logging.getLogger("aviary.media_audit")

_PAGE = 200
_MAX_PAGES = 500          # 100k events — well past any sane retention
_GRACE_S = 3600.0         # events younger than this are still being finalised
_PERIOD_S = 6 * 3600

_settings = None
# Set once the first complete audit after start-up has been applied; keepsakes wait on
# it so their "oldest surviving sighting" is answered by the database, not by probing.
first_pass = asyncio.Event()


def configure(settings) -> None:
    global _settings
    _settings = settings


def enabled() -> bool:
    return bool(_settings and getattr(_settings, "frigate_url", ""))


async def list_frigate_events(base: str) -> Optional[dict[str, tuple[int, int]]]:
    """{event_id: (has_clip, has_snapshot)} for every bird event Frigate still has, or
    None when the listing could not be completed."""
    found: dict[str, tuple[int, int]] = {}
    before: Optional[float] = None
    for _ in range(_MAX_PAGES):
        params = {"labels": "bird", "limit": _PAGE, "include_thumbnails": 0}
        if before is not None:
            params["before"] = f"{before:.6f}"
        try:
            status, data = await proxy.get_json(f"{base}/api/events?{urlencode(params)}")
        except Exception as exc:  # noqa: BLE001 — httpx transport errors
            log.info("Media audit: Frigate unreachable (%s); skipping this pass.", exc)
            return None
        if status >= 400 or not isinstance(data, list):
            log.info("Media audit: Frigate events listing failed (%s); skipping this pass.", status)
            return None
        min_start = None
        for ev in data:
            if not isinstance(ev, dict) or not ev.get("id"):
                continue
            found[str(ev["id"])] = (1 if ev.get("has_clip") else 0, 1 if ev.get("has_snapshot") else 0)
            st = ev.get("start_time")
            if st is not None and (min_start is None or st < min_start):
                min_start = st
        if len(data) < _PAGE or min_start is None:
            return found
        next_before = min_start - 0.0001
        if before is not None and next_before >= before:
            return found  # no progress; the listing is as complete as it gets
        before = next_before
    log.warning("Media audit: gave up after %d pages; treating the listing as incomplete.",
                _MAX_PAGES)
    return None


async def run_audit() -> Optional[dict]:
    """One complete pass. Returns counters, or None when nothing was applied."""
    if not enabled():
        return None
    listed = await list_frigate_events(_settings.frigate_url)
    if listed is None:
        return None
    now = time.time()
    if not listed:
        recent = await asyncio.to_thread(db.recent_frigate_count, now - 86400)
        if recent:
            log.warning("Media audit: Frigate lists no bird events but %d arrived in the last "
                        "day; not marking anything (check frigate_url / labels).", recent)
            return None
    rows = await asyncio.to_thread(db.frigate_media_rows, now - _GRACE_S)
    expired: list[int] = []
    synced: dict[int, tuple[int, int]] = {}
    restored: dict[int, tuple[int, int]] = {}
    for r in rows:
        ours = (int(r["has_clip"] or 0), int(r["has_snapshot"] or 0))
        theirs = listed.get(str(r["source_ref"]))
        if theirs is None or theirs == (0, 0):
            if ours != (0, 0) or r["media_expired_at"] is None:
                expired.append(r["id"])
        elif r["media_expired_at"] is not None:
            restored[r["id"]] = theirs
        elif theirs != ours:
            synced[r["id"]] = theirs
    if expired or synced or restored:
        await asyncio.to_thread(db.apply_media_audit, expired, synced, restored)
    result = {"listed": len(listed), "expired": len(expired), "synced": len(synced),
              "restored": len(restored)}
    if expired or synced or restored:
        log.info("Media audit: Frigate lists %d bird events; %d detection(s) expired (footage "
                 "gone), %d restored, %d flag(s) synced.",
                 len(listed), len(expired), len(restored), len(synced))
    else:
        log.debug("Media audit: Frigate lists %d bird events; nothing to change.", len(listed))
    return result


async def run(after: Optional[asyncio.Task] = None) -> None:
    """Background task: audit once start-up settles, then every ``_PERIOD_S``."""
    if not enabled():
        first_pass.set()  # nothing to wait for
        return
    if after is not None:
        try:
            await after
        except Exception:  # noqa: BLE001 — the backfill logs its own failures
            pass
    await asyncio.sleep(10)
    while True:
        started = time.monotonic()
        try:
            await run_audit()
        except Exception:  # noqa: BLE001
            log.exception("Media audit failed; will retry next period.")
        first_pass.set()
        await asyncio.sleep(max(60.0, _PERIOD_S - (time.monotonic() - started)))
