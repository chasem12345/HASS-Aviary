"""New-species Home Assistant notifications.

When a species is stored for the first time, an ``aviary_new_species`` event is fired
on the Home Assistant event bus (through the Supervisor's Core API proxy, which needs
``homeassistant_api: true`` in config.yaml). A notification image — the Frigate
snapshot, else BirdNET-Go's generic species photo — is staged under HA's ``www``
folder first so companion apps can fetch it at ``/local/aviary/<slug>.<ext>``.

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

_EVENT_URL = "http://supervisor/core/api/events/aviary_new_species"
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


async def send_new_species(row: dict, test: bool = False) -> dict:
    """Stage an image and fire the event for one detection row.

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
    image_url: Optional[str] = None
    fetched = await _resolve_image(row)
    if fetched:
        image_url = _save_image(*fetched, slug=species_slug(common_name))

    payload = {
        "common_name": common_name,
        "scientific_name": row.get("scientific_name"),
        "source": source,
        "verb": "seen" if source == "frigate" else "heard",
        "confidence": row.get("confidence"),
        "location": row.get("location"),
        "image": image_url,
        "detected_at": _iso(row.get("start_time")),
    }
    if test:
        payload["test"] = True

    error = await _fire_event(payload)
    if error:
        log.warning("aviary_new_species for %s not delivered: %s", common_name, error)
        return {"fired": False, "image": image_url, "error": error}
    log.info(
        "New species %s: fired aviary_new_species (image=%s%s)",
        common_name, image_url or "none", ", test" if test else "",
    )
    return {"fired": True, "image": image_url, "error": None}


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
    return f"/local/aviary/{name}"


async def _fire_event(payload: dict) -> Optional[str]:
    """POST the event to the Core API proxy. Returns an error string, or None on success."""
    if _client is None:
        return "notify HTTP client not initialized"
    headers = {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}
    last = "unknown error"
    for attempt in range(_EVENT_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_EVENT_RETRY_S)
        try:
            resp = await _client.post(_EVENT_URL, json=payload, headers=headers)
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
