"""Home Assistant detection events for notifications.

Every classified detection fires an ``aviary_detection`` event on the Home Assistant
event bus (through the Supervisor's Core API proxy — ``homeassistant_api: true``),
carrying everything a notification automation needs to filter: seen/heard verb,
``is_new_species``, how long the species had been quiet
(``seconds_since_species_last_detected``), the Frigate event id (``source_ref``),
and the Aviary panel path for tap actions. First-ever species ALSO fire the legacy
``aviary_new_species`` event for older automations.

A notification image — the Frigate snapshot, else BirdNET-Go's generic species
photo — is staged under HA's ``www`` folder first so companion apps can fetch it at
``/local/aviary/<slug>.<ext>``.

The bundled automation blueprint (``blueprints/new_species_notification.yaml``) is
copied into HA's config at startup so users can wire the event to a mobile
notification without writing YAML.

All failures here are soft: a missed notification must never affect ingest.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from . import db, proxy
from .settings import Settings

log = logging.getLogger("aviary.notify")

_EVENTS_URL = "http://supervisor/core/api/events/{}"
_SELF_INFO_URL = "http://supervisor/addons/self/info"
_BLUEPRINT_SRC = Path(__file__).resolve().parent.parent / "blueprints" / "new_species_notification.yaml"

# Frigate's first MQTT message for an event usually predates the snapshot; later
# update/end messages upsert snapshot_ref into the same row, so poll briefly.
_SNAPSHOT_WAIT_S = 10.0
_SNAPSHOT_POLL_S = 2.5

_EVENT_ATTEMPTS = 3   # covers a short HA core restart window
_EVENT_RETRY_S = 5.0

_EXT_BY_TYPE = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

_client: Optional[httpx.AsyncClient] = None
_settings: Optional[Settings] = None
# Aviary's HA sidebar path (/hassio/ingress/<slug>) for notification tap actions.
# Discovered lazily from the Supervisor self-info API; False = lookup failed, retry.
_panel_path_cache: Optional[str | bool] = None


def configure(settings: Settings) -> None:
    global _settings
    _settings = settings
    if not enabled():
        reason = (
            "disabled in the add-on options"
            if not settings.notify_new_species
            else "SUPERVISOR_TOKEN is not set (not running as a Home Assistant add-on?)"
        )
        log.info("New-species notifications off: %s", reason)


def enabled() -> bool:
    return bool(
        _settings
        and _settings.notify_new_species
        and os.environ.get("SUPERVISOR_TOKEN")
    )


def init_client() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def install_blueprint() -> None:
    """Copy the bundled blueprint into HA's config (idempotent; refreshes on upgrade)."""
    if _settings is None:
        return
    cfg = Path(_settings.ha_config_dir)
    if not cfg.is_dir():
        log.info("HA config not mounted at %s; skipping blueprint install.", cfg)
        return
    try:
        src_bytes = _BLUEPRINT_SRC.read_bytes()
    except OSError:
        log.warning("Bundled blueprint missing at %s", _BLUEPRINT_SRC)
        return
    dst = cfg / "blueprints" / "automation" / "aviary" / _BLUEPRINT_SRC.name
    try:
        if dst.is_file() and dst.read_bytes() == src_bytes:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src_bytes)
        log.info("Installed notification blueprint at %s", dst)
    except OSError:
        log.warning("Could not install blueprint to %s", dst, exc_info=True)


def species_slug(common_name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", common_name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug or "sp-" + hashlib.sha1(common_name.encode()).hexdigest()[:10]


async def send_detection(row: dict, is_new: bool, test: bool = False) -> dict:
    """Stage an image and fire the aviary_detection event for one detection row.

    Returns ``{"fired": bool, "image": str|None, "error": str|None}`` — consumed by
    the test endpoint; fire-and-forget callers can ignore it. Never raises (it is
    scheduled unsupervised from the MQTT thread).
    """
    common_name = row.get("common_name") or "bird"
    if _settings is None or not _settings.notify_new_species:
        return {"fired": False, "image": None, "error": "notify_new_species is disabled in the add-on options"}
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return {"fired": False, "image": None, "error": "SUPERVISOR_TOKEN missing — not running under the Supervisor"}

    source = row.get("source") or "birdnet"
    source_ref = str(row.get("source_ref") or "")

    # Per-species quiet gaps (overall and per source), for the blueprint's cooldown.
    last_times = await asyncio.to_thread(db.species_last_times, common_name, source, source_ref)
    start_time = row.get("start_time")

    def _gap(key: str) -> Optional[float]:
        prev = last_times.get(key)
        if prev is None or start_time is None:
            return None
        return max(0.0, float(start_time) - prev)

    image_url: Optional[str] = None
    fetched = await _resolve_image(row)
    if fetched:
        image_url = _save_image(*fetched, slug=species_slug(common_name))

    payload = {
        "common_name": common_name,
        "scientific_name": row.get("scientific_name"),
        "source": source,
        "source_ref": source_ref,  # Frigate event id — used for the clip tap action
        "verb": "seen" if source == "frigate" else "heard",
        "confidence": row.get("confidence"),
        "location": row.get("location"),
        "image": image_url,
        "detected_at": _iso(row.get("start_time")),
        "is_new_species": bool(is_new),
        "seconds_since_species_last_detected": _gap("any"),
        "seconds_since_species_last_seen": _gap("seen"),
        "seconds_since_species_last_heard": _gap("heard"),
        "panel_path": await _panel_path(),
    }
    if test:
        payload["test"] = True

    error = await _fire_event("aviary_detection", payload)
    if not error and is_new:
        # Legacy event, kept so pre-0.4.0 automations continue to work.
        await _fire_event("aviary_new_species", payload)
    if error:
        log.warning("aviary_detection for %s not delivered: %s", common_name, error)
        return {"fired": False, "image": image_url, "error": error}
    log.info(
        "Detection %s (%s%s): fired aviary_detection (image=%s%s)",
        common_name, payload["verb"], ", new species" if is_new else "",
        image_url or "none", ", test" if test else "",
    )
    return {"fired": True, "image": image_url, "error": None}


async def _panel_path() -> Optional[str]:
    """``/hassio/ingress/<slug>`` for this add-on, from the Supervisor self-info API.

    Needs ``hassio_api: true``. Cached after the first success; a failure is cached
    as False and retried on the next detection.
    """
    global _panel_path_cache
    if isinstance(_panel_path_cache, str):
        return _panel_path_cache
    if _client is None:
        return None
    try:
        resp = await _client.get(
            _SELF_INFO_URL,
            headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"},
        )
        slug = (resp.json().get("data") or {}).get("slug") if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        slug = None
    if slug:
        _panel_path_cache = f"/hassio/ingress/{slug}"
        return _panel_path_cache
    if _panel_path_cache is None:  # log the first failure only
        log.warning("Could not resolve add-on slug from the Supervisor (is hassio_api enabled?); "
                    "notification tap actions for audio detections will be omitted.")
    _panel_path_cache = False
    return None


# ------------------------------------------------------------------------ internals

async def _resolve_image(row: dict) -> Optional[tuple[bytes, str]]:
    """Best (bytes, content-type) for the notification image, or None."""
    try:
        if row.get("source") == "frigate" and _settings.frigate_url:
            deadline = time.monotonic() + _SNAPSHOT_WAIT_S
            while True:
                # Re-read by (source, source_ref): later Frigate messages upsert the
                # snapshot into the same row after the first "new" message.
                fresh = await asyncio.to_thread(
                    db.detection_by_ref, row["source"], str(row["source_ref"])
                ) or row
                ref = fresh.get("snapshot_ref")
                if ref:
                    data = await _fetch_image(
                        proxy.frigate_snapshot_url(_settings.frigate_url, ref)
                    )
                    if data:
                        return data
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(_SNAPSHOT_POLL_S)
        # Audio detections (and frigate fallback): BirdNET-Go's generic species photo.
        if _settings.birdnet_url:
            sci = row.get("scientific_name") or await asyncio.to_thread(
                db.scientific_name_for, row.get("common_name") or ""
            )
            if sci:
                return await _fetch_image(
                    proxy.birdnet_species_image_url(_settings.birdnet_url, sci)
                )
    except Exception:  # noqa: BLE001 - the image is best-effort, never fatal
        log.exception("Notification image resolution failed for %s", row.get("common_name"))
    return None


async def _fetch_image(url: str) -> Optional[tuple[bytes, str]]:
    if _client is None:
        return None
    try:
        resp = await _client.get(url)
    except httpx.HTTPError as exc:
        log.debug("Image fetch failed for %s: %s", url, exc)
        return None
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if resp.status_code != 200 or not ctype.startswith("image/"):
        return None
    return resp.content, ctype


def _save_image(data: bytes, ctype: str, slug: str) -> Optional[str]:
    """Write under HA's www folder; return the /local/ URL companion apps can fetch."""
    cfg = Path(_settings.ha_config_dir)
    if not cfg.is_dir():
        log.info("HA config not mounted at %s; sending notification without image.", cfg)
        return None
    name = f"{slug}.{_EXT_BY_TYPE.get(ctype, 'jpg')}"
    path = cfg / "www" / "aviary" / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        log.warning("Could not write notification image %s", path, exc_info=True)
        return None
    # Cache-buster: HA serves /local with a ~month-long max-age, and this URL is
    # reused per species — without it phones show the previously cached image.
    return f"/local/aviary/{name}?v={int(time.time())}"


async def _fire_event(event_type: str, payload: dict) -> Optional[str]:
    """POST an event to the Core API proxy. Returns an error string, or None on success."""
    if _client is None:
        return "notify HTTP client not initialized"
    headers = {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}
    last = "unknown error"
    for attempt in range(_EVENT_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_EVENT_RETRY_S)
        try:
            resp = await _client.post(_EVENTS_URL.format(event_type), json=payload, headers=headers)
        except httpx.HTTPError as exc:
            last = f"Supervisor API unreachable: {exc}"
            continue
        if resp.status_code < 400:
            return None
        last = f"Supervisor API returned {resp.status_code}: {resp.text[:200]}"
    return last


def _iso(epoch) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None
