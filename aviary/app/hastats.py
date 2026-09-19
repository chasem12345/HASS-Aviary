"""Bird statistics for Home Assistant, published over MQTT discovery.

Two things Home Assistant cannot work out for itself: how frequent a visitor each species
is, and which bird is in which Frigate zone right now. Frigate's own zone occupancy
sensors know that *a bird* is at the feeder, never which one. Aviary knows both, so it
publishes them as real entities under one "Aviary" device:

* one sensor per confirmed species — state is a **rarity** score from 0 (seen every day of
  the window) to 100 (seldom or never), with the all-time and recent counts, days active,
  tier and first/last seen as attributes;
* ``sensor.aviary_ptz_target`` — every zone Aviary has ever seen, scored by the rarest bird
  known to be in it right now, else by the zone's usual visitors (its *baseline*). A PTZ
  automation reads ``zone_scores`` and points at the rarest occupied zone instead of a
  fixed zone order.

Everything here is best-effort: a broker hiccup or a bad row must never break ingest or a
page. Publishes are debounced (a visit's fragments end seconds apart), compared against the
last payload so a quiet feeder publishes nothing, and retained so Home Assistant has the
values back after its own restart. Discovery is the device-based form — one retained
payload — and a species that is deleted, blacklisted or unconfirmed is removed by the
documented stub-component step, so no ghost entities are left behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Optional

from . import db, recap, solar

log = logging.getLogger("aviary.hastats")

STATUS_TOPIC = "aviary/status"
ZONES_TOPIC = "aviary/zones/state"
DEVICE_ID = "aviary"

# A visit is many events ending seconds apart; a species refresh is one query over the
# whole table, so coalesce. Zone changes feed a camera decision and should not wait.
_SPECIES_DEBOUNCE_S = 5.0
_ZONES_DEBOUNCE_S = 1.0
# Periodic refresh: the recent counters roll off by the day and days-active rolls at
# local midnight; 15 minutes keeps both honest without a per-event cost.
_PERIOD_S = 15 * 60
# An in-progress row Frigate never closed must not pin a zone forever.
_STALE_S = 2 * 3600
# A zone with no history at all: the same value for every such zone, so the automation's
# own order decides, exactly as before this feature existed.
NEUTRAL = 50
_PREF_SLUGS = "hastats_slugs"

_settings = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_ingestor = None
_lock = threading.Lock()
_pending: set[str] = set()
_drains: dict[str, asyncio.Task] = {}
_refresh_lock: Optional[asyncio.Lock] = None
_expiry: Optional[asyncio.TimerHandle] = None
# Last payload per topic, so a refresh that changes nothing publishes nothing.
_sent: dict[str, str] = {}
# From the last species refresh: rarity by lower-cased name, and zone baselines.
_rarity: dict[str, int] = {}
_baselines: dict[str, int] = {}
_slugs: dict[str, str] = {}


# ------------------------------------------------------------------------ set-up

def configure(settings) -> None:
    global _settings
    _settings = settings


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the app loop so the MQTT thread's nudges can be scheduled onto it."""
    global _loop
    _loop = loop


def enabled() -> bool:
    return bool(_settings and getattr(_settings, "ha_stats", False)
                and getattr(_settings, "mqtt_enabled", False))


def attach(ingestor) -> None:
    """Wire the MQTT client: a will, the connect hook, and Home Assistant's birth topic."""
    global _ingestor
    if not enabled():
        return
    _ingestor = ingestor
    ingestor.enable_status(STATUS_TOPIC)
    ingestor.on_connect_hooks.append(_on_connect)
    ingestor.add_subscription(f"{_prefix()}/status", _on_ha_status)


def _prefix() -> str:
    return (getattr(_settings, "mqtt_discovery_prefix", "") or "homeassistant").strip("/")


def _on_connect() -> None:
    """Paho thread, on every (re)connect: say we are here, then republish everything."""
    if _ingestor is None:
        return
    _ingestor.publish(STATUS_TOPIC, "online", retain=True)
    _sent.clear()
    schedule_species()
    schedule_zones()


def _on_ha_status(payload: bytes) -> None:
    """Home Assistant (re)started: it re-reads retained discovery itself, but a fresh
    publish costs nothing and covers a broker that dropped the retained messages."""
    if (payload or b"").strip().lower() == b"online":
        _on_connect()


# ---------------------------------------------------------------------- triggers

def on_hook(kind: str) -> None:
    """ingest's stats hook: ``"species"`` or ``"zones"``."""
    schedule_species() if kind == "species" else schedule_zones()


def schedule_species() -> None:
    _schedule("species")


def schedule_zones() -> None:
    _schedule("zones")


def touch() -> None:
    """A mutation from the UI (a label, a delete, a confirmation) moved both."""
    schedule_species()
    schedule_zones()


def _schedule(kind: str) -> None:
    """Thread-safe; never raises."""
    if not enabled():
        return
    with _lock:
        _pending.add(kind)
    loop = _loop
    if loop is None or loop.is_closed():
        return  # tests, or before start-up: the first periodic pass picks it up
    try:
        loop.call_soon_threadsafe(_ensure_drain, kind)
    except RuntimeError:
        pass  # loop shutting down


def _ensure_drain(kind: str) -> None:
    task = _drains.get(kind)
    if task is None or task.done():
        _drains[kind] = asyncio.get_running_loop().create_task(_drain(kind))


async def _drain(kind: str) -> None:
    await asyncio.sleep(_SPECIES_DEBOUNCE_S if kind == "species" else _ZONES_DEBOUNCE_S)
    while True:
        with _lock:
            due = kind in _pending
            _pending.discard(kind)
        if not due:
            return
        try:
            if kind == "species":
                await refresh_species()
            else:
                await refresh_zones()
        except Exception:  # noqa: BLE001 — statistics must never take anything down
            log.exception("Publishing %s statistics failed.", kind)
            return


# --------------------------------------------------------------------- refreshes

def _refresh_guard() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


async def refresh_species() -> int:
    """Publish (discovery and) states for every confirmed species. Returns publishes."""
    if not enabled() or _ingestor is None:
        return 0
    async with _refresh_guard():
        now = time.time()
        window_days = int(_settings.ha_stats_window_days)
        recent_days = int(_settings.ha_stats_recent_days)
        window_start, recent_start = window_starts(now, window_days, recent_days)
        only_confirmed = bool(_settings.require_species_confirmation)
        rows = await asyncio.to_thread(db.species_frequency, window_start, recent_start,
                                       only_confirmed)
        base_rows = await asyncio.to_thread(db.zone_baselines, window_start, only_confirmed)

        rarity_by_name: dict[str, int] = {}
        for r in rows:
            score, tier_key = rarity(int(r.get("days_active") or 0), window_days)
            r["rarity"], r["tier"] = score, tier_key
            rarity_by_name[r["common_name"].lower()] = score
        slugs = slugs_for([r["common_name"] for r in rows])
        published = 0

        # Discovery: republish when the entity set changed (or after a reconnect, when
        # the change cache is empty). Removals go first, so a vanished species' stub is
        # sent before the clean payload replaces it.
        previous = _load_slugs()
        current = set(slugs.values())
        vanished = previous - current
        device = discovery_payload(slugs, _settings.addon_version)
        topic = f"{_prefix()}/device/{DEVICE_ID}/config"
        if vanished:
            stubbed = json.loads(json.dumps(device))
            for slug_ in sorted(vanished):
                stubbed["components"][f"species_{slug_}"] = {"platform": "sensor"}
            _publish(topic, _dumps(stubbed), force=True)
            for slug_ in sorted(vanished):
                _publish(f"aviary/species/{slug_}/state", "", force=True)
                _sent.pop(f"aviary/species/{slug_}/state", None)
            published += 1
            log.info("Removed %d species sensor(s) from Home Assistant: %s.",
                     len(vanished), ", ".join(sorted(vanished)))
        if _publish(topic, _dumps(device)):
            published += 1
        if current != previous:
            await asyncio.to_thread(db.set_pref, _PREF_SLUGS, json.dumps(sorted(current)))

        for r in rows:
            payload = species_payload(r, window_days, recent_days)
            if _publish(f"aviary/species/{slugs[r['common_name']]}/state", _dumps(payload)):
                published += 1

        _rarity.clear()
        _rarity.update(rarity_by_name)
        _baselines.clear()
        _baselines.update(baseline(base_rows, rarity_by_name))
        _slugs.clear()
        _slugs.update(slugs)
        if published:
            log.debug("Published %d species statistic message(s) for %d species.",
                      published, len(rows))
    # Scores may have moved; the zone map depends on them.
    return published + await refresh_zones()


async def refresh_zones() -> int:
    """Publish the per-zone map. Returns 1 when something was published."""
    global _expiry
    if not enabled() or _ingestor is None:
        return 0
    now = time.time()
    memory_s = float(_settings.ha_stats_zone_memory_s)
    occupants = await asyncio.to_thread(db.active_zone_occupants, now, memory_s, _STALE_S)
    known = await asyncio.to_thread(db.distinct_zones)
    payload = zones_payload(occupants, _rarity, _baselines, known, now,
                            memory_s=memory_s, window_days=int(_settings.ha_stats_window_days))
    # A remembered bird expires by the clock, not by a message: re-arm once for the
    # earliest expiry so the zone drops back to its baseline on time.
    untils = [o["until"] for o in occupants if o.get("until")]
    if _expiry is not None:
        _expiry.cancel()
        _expiry = None
    if untils and _loop is not None and not _loop.is_closed():
        delay = max(0.5, min(untils) - now + 0.2)
        _expiry = _loop.call_later(delay, schedule_zones)
    return 1 if _publish(ZONES_TOPIC, _dumps(payload)) else 0


async def run(after: Optional[asyncio.Task] = None) -> None:
    """Background task: publish once start-up settles, then every ``_PERIOD_S``."""
    if not enabled():
        return
    log.info("Publishing bird statistics to Home Assistant over MQTT discovery "
             "(%d-day window, %d-day recent counters, %ds zone memory).",
             _settings.ha_stats_window_days, _settings.ha_stats_recent_days,
             _settings.ha_stats_zone_memory_s)
    if after is not None:
        try:
            await after  # imported history changes every count
        except Exception:  # noqa: BLE001 — the backfill logs its own failures
            pass
    while True:
        started = time.monotonic()
        try:
            await refresh_species()
        except Exception:  # noqa: BLE001
            log.exception("Statistics refresh failed; will retry next period.")
        await asyncio.sleep(max(60.0, _PERIOD_S - (time.monotonic() - started)))


def _publish(topic: str, payload: str, force: bool = False) -> bool:
    if not force and _sent.get(topic) == payload:
        return False
    if _ingestor is None or not _ingestor.publish(topic, payload, retain=True):
        return False
    _sent[topic] = payload
    return True


def _load_slugs() -> set[str]:
    try:
        return set(json.loads(db.get_pref(_PREF_SLUGS, "[]")))
    except (ValueError, TypeError):
        return set()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------ pure helpers

def slug(name: str) -> str:
    """``"Bewick's Wren"`` → ``bewicks_wren``: ASCII, lower, underscores; entity-id safe."""
    folded = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    folded = folded.replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "_", folded.lower()).strip("_") or "species"


def slugs_for(names: list[str]) -> dict[str, str]:
    """Name → slug, unique. A collision gives the alphabetically later name a short,
    deterministic hash suffix so its entity id never moves."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for name in sorted(names, key=str.lower):
        s = slug(name)
        if s in taken:
            s = f"{s}_{hashlib.sha1(name.encode()).hexdigest()[:4]}"
        taken.add(s)
        out[name] = s
    return out


def rarity(days_active: int, window_days: int) -> tuple[int, str]:
    """(0–100 score, tier key). 0 = seen every day of the window, 100 = not at all."""
    window = max(1, int(window_days))
    ratio = min(1.0, max(0, int(days_active)) / window)
    score = int(round(100 * (1.0 - ratio)))
    tier_key = "rare"
    for key, threshold, _hint in recap.TIERS:
        if ratio >= threshold:
            tier_key = key
            break
    return score, tier_key


def window_starts(now: float, window_days: int, recent_days: int) -> tuple[float, float]:
    """Local-midnight-aligned starts: a 30-day window is today plus 29 whole days."""
    today = date.fromtimestamp(now)
    return (solar.local_midnight(today - timedelta(days=max(1, window_days) - 1)),
            solar.local_midnight(today - timedelta(days=max(1, recent_days) - 1)))


def iso_local(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def species_payload(row: dict, window_days: int, recent_days: int) -> dict:
    return {
        "common_name": row["common_name"],
        "scientific_name": row.get("scientific_name"),
        "rarity": int(row["rarity"]),
        "tier": row["tier"],
        "seen_all_time": int(row.get("seen_all_time") or 0),
        "heard_all_time": int(row.get("heard_all_time") or 0),
        f"seen_{recent_days}d": int(row.get("seen_recent") or 0),
        f"heard_{recent_days}d": int(row.get("heard_recent") or 0),
        f"count_{recent_days}d": int(row.get("seen_recent") or 0) + int(row.get("heard_recent") or 0),
        f"days_active_{window_days}d": int(row.get("days_active") or 0),
        "window_days": int(window_days),
        "recent_days": int(recent_days),
        "first_seen": iso_local(row.get("first_seen")),
        "last_seen": iso_local(row.get("last_seen")),
        "confirmed": bool(row.get("confirmed", True)),
    }


def discovery_payload(slugs: dict[str, str], version: str) -> dict:
    """The device-based discovery document: one Aviary device, one component per species
    plus the PTZ target. ``default_entity_id`` pins the entity ids; the device name is
    prefixed onto the friendly names by Home Assistant ("Aviary Northern Cardinal")."""
    components: dict[str, dict] = {}
    for name, s in sorted(slugs.items(), key=lambda kv: kv[1]):
        components[f"species_{s}"] = {
            "platform": "sensor",
            "name": name,
            "unique_id": f"aviary_species_{s}",
            "default_entity_id": f"sensor.aviary_{s}",
            "state_topic": f"aviary/species/{s}/state",
            "value_template": "{{ value_json.rarity }}",
            "json_attributes_topic": f"aviary/species/{s}/state",
            "state_class": "measurement",
            "icon": "mdi:bird",
        }
    components["ptz_target"] = {
        "platform": "sensor",
        "name": "PTZ target",
        "unique_id": "aviary_ptz_target",
        "default_entity_id": "sensor.aviary_ptz_target",
        "state_topic": ZONES_TOPIC,
        "value_template": "{{ value_json.target }}",
        "json_attributes_topic": ZONES_TOPIC,
        "icon": "mdi:cctv",
    }
    return {
        "device": {"identifiers": [DEVICE_ID], "name": "Aviary", "manufacturer": "HASS-Aviary",
                   "model": "Aviary add-on", "sw_version": version},
        "origin": {"name": "aviary", "sw": version,
                   "url": "https://github.com/chasem12345/HASS-Aviary"},
        "availability_topic": STATUS_TOPIC,
        "components": components,
    }


def split_zones(combo: Optional[str]) -> list[str]:
    """``"bath, platform"`` → ``["bath", "platform"]`` (lower-cased, deduped, ordered)."""
    seen: list[str] = []
    for z in (combo or "").split(","):
        z = z.strip().lower()
        if z and z not in seen:
            seen.append(z)
    return seen


def baseline(rows: list[dict], rarity_by_name: dict[str, int],
             neutral: int = NEUTRAL) -> dict[str, int]:
    """Per zone, the visit-weighted median rarity of its visitors in the window.

    ``rows`` are ``db.zone_baselines`` rows: ``{zone (comma-joined), common_name, visits}``.
    A zone with twenty hummingbird visits and one warbler visit scores the hummingbird's
    rarity — that is what an unnamed bird there most likely is. No history → ``neutral``.
    """
    weights: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        score = rarity_by_name.get((r.get("common_name") or "").lower())
        n = int(r.get("visits") or 0)
        if score is None or n <= 0:
            continue
        for z in split_zones(r.get("zone")):
            weights.setdefault(z, []).append((score, n))
    out: dict[str, int] = {}
    for z, pairs in weights.items():
        pairs.sort()
        total = sum(n for _, n in pairs)
        acc = 0
        for score, n in pairs:
            acc += n
            if acc * 2 >= total:
                out[z] = score
                break
        else:
            out[z] = neutral
    return out


def zones_payload(occupants: list[dict], rarity_by_name: dict[str, int],
                  baselines: dict[str, int], known_zones: list[str], now: float, *,
                  memory_s: float, window_days: int, neutral: int = NEUTRAL) -> dict:
    """The PTZ-target document. See the module docstring for the rule."""
    present: dict[str, dict] = {}
    for o in occupants:
        name = o.get("species")
        key = (name or "").lower()
        for z in split_zones(o.get("zone")):
            entry = present.setdefault(z, {"species": None, "rarity": None, "since": None,
                                           "source": None})
            since = o.get("since")
            if entry["since"] is None or (since and since < entry["since"]):
                entry["since"] = since
            if name and key in rarity_by_name:
                score = rarity_by_name[key]
                if entry["rarity"] is None or score > entry["rarity"]:
                    entry.update(species=name, rarity=score, source=o.get("source"))
            elif entry["species"] is None and name:
                entry["source"] = "unconfirmed"
    zones: dict[str, dict] = {}
    scores: dict[str, int] = {}
    target, target_score = "none", -1
    for z in sorted({*(k.lower() for k in known_zones), *present}):
        base = int(baselines.get(z, neutral))
        e = present.get(z)
        if e and e["rarity"] is not None:
            score, source, species = int(e["rarity"]), e["source"] or "frigate", e["species"]
            if score > target_score:
                target, target_score = z, score
        else:
            score, source, species = base, (e or {}).get("source") or "baseline", None
        scores[z] = score
        zones[z] = {"species": species, "rarity": score,
                    "since": iso_local(e["since"]) if e and e.get("since") else None,
                    "source": source, "baseline": base}
    return {
        "target": target,
        "zone_scores": scores,
        "zones": zones,
        "baselines": {z: int(b) for z, b in sorted(baselines.items())},
        "neutral": neutral,
        "memory_s": int(memory_s),
        "window_days": int(window_days),
        "updated": iso_local(now),
    }
