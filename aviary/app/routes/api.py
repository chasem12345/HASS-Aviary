"""JSON endpoints backing the Chart.js visualizations and live refresh."""

from __future__ import annotations

import time
from typing import Optional

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool

from .. import db, ingest, notify, proxy, species_audio, species_info, traits
from . import ingress_url, set_theme

router = APIRouter()


def _norm_source(source: Optional[str]) -> Optional[str]:
    return source if source in ("frigate", "birdnet") else None


@router.get("/summary")
def summary(source: Optional[str] = Query(None), days: int = Query(7, ge=1, le=3650)):
    src = _norm_source(source)
    since = time.time() - days * 86400
    return {
        "stats": db.summary_stats(source=src, since=since),
        "top_species": db.top_species(limit=10, source=src, since=since),
    }


@router.get("/per-day")
def per_day(
    source: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=3650),
    species: Optional[str] = Query(None),
    since: Optional[float] = Query(None, ge=0),
):
    data = db.detections_per_day(
        days=days, source=_norm_source(source), species=species, since=since
    )
    return {"data": data}


@router.get("/hourly")
def hourly(
    source: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=3650),
    species: Optional[str] = Query(None),
    since: Optional[float] = Query(None, ge=0),
):
    effective_since = since if since is not None else time.time() - days * 86400
    data = db.hourly_activity(
        source=_norm_source(source), since=effective_since, species=species
    )
    return {"data": data}


@router.get("/latest")
def latest(source: Optional[str] = Query(None), species: Optional[str] = Query(None)):
    """Cheap change marker polled by the Recent page for live refresh."""
    return db.change_marker(source=_norm_source(source), species=species)


def _norm_source_action(action: Optional[str]) -> str:
    return action if action in ("clear", "delete") else "none"


async def _source_action(settings, det: dict, action: str) -> Optional[dict]:
    """Apply the requested action at the detection's source. Returns a status dict.

    'clear' removes the species label but keeps the event (Frigate only — for
    BirdNET-Go the detection IS the classification, so 'clear' deletes it).
    """
    if action == "none":
        return None
    try:
        if det["source"] == "frigate":
            if not settings.frigate_url:
                return {"ok": False, "error": "frigate_url not configured"}
            if action == "clear":
                status, text = await proxy.call_upstream(
                    "POST",
                    proxy.frigate_sub_label_url(settings.frigate_url, det["source_ref"]),
                    json={"subLabel": ""},
                )
            else:
                status, text = await proxy.call_upstream(
                    "DELETE", proxy.frigate_event_api_url(settings.frigate_url, det["source_ref"])
                )
        else:
            if not settings.birdnet_url:
                return {"ok": False, "error": "birdnet_url not configured"}
            if not det.get("native_id"):
                return {"ok": False, "error": "no BirdNET-Go id recorded for this detection"}
            status, text = await proxy.call_upstream(
                "DELETE", proxy.birdnet_detection_url(settings.birdnet_url, det["native_id"])
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"source unreachable: {exc}"}
    if status >= 400:
        return {"ok": False, "error": f"source returned {status}: {text}"}
    return {"ok": True, "error": None}


def _forget_if_gone(common_name: str) -> None:
    """After deletions, let the species announce as new again if nothing remains."""
    if not (db.species_stats(common_name).get("total") or 0):
        ingest.forget_species(common_name)


@router.delete("/detections/{det_id}")
async def delete_detection(det_id: int, request: Request, source_action: Optional[str] = Query(None)):
    """Remove a (mis)classified detection. The ref is tombstoned so backfill can't
    re-import it. ``source_action``: none | clear (drop Frigate's species label,
    keep the event) | delete (remove the event/detection at the source)."""
    action = _norm_source_action(source_action)
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if det is None:
        return {"ok": False, "error": "detection not found"}
    source_result = await _source_action(request.app.state.settings, det, action)
    await run_in_threadpool(db.delete_detection, det_id)
    ingest.add_tombstone(det["source"], det["source_ref"])
    await run_in_threadpool(_forget_if_gone, det["common_name"])
    return {"ok": True, "common_name": det["common_name"], "source_result": source_result}


@router.delete("/species/{name:path}")
async def delete_species(name: str, request: Request, source_action: Optional[str] = Query(None)):
    """Remove every detection of a species (e.g. a misclassification-only species),
    tombstoning each ref, optionally clearing/deleting them at the source too."""
    action = _norm_source_action(source_action)
    rows = await run_in_threadpool(db.delete_species, name)
    if not rows:
        return {"ok": False, "error": "species not found", "deleted": 0}
    source_errors = []
    for det in rows:
        ingest.add_tombstone(det["source"], det["source_ref"])
        result = await _source_action(request.app.state.settings, det, action)
        if result and not result["ok"]:
            source_errors.append(f"{det['source']} {det['source_ref']}: {result['error']}")
    ingest.forget_species(name)
    return {
        "ok": True,
        "deleted": len(rows),
        "source_errors": source_errors[:5],
        "source_error_count": len(source_errors),
    }


# ------------------------------------------------------------------------- blacklist
# Deliberately *not* nested under /species/{name} — the existing
# `DELETE /species/{name:path}` route would greedily match a trailing "/blacklist" as
# part of the species name.

@router.get("/blacklist")
async def list_blacklist():
    """Species that are never ingested, for the settings page."""
    return {"entries": await run_in_threadpool(db.blacklist_entries)}


@router.post("/blacklist")
async def add_blacklist(
    request: Request,
    species: str = Query(..., min_length=1),
    source_action: Optional[str] = Query(None),
):
    """Blacklist a species: purge everything it has, then refuse it at ingest forever.

    Same ``source_action`` semantics as ``DELETE /species/{name}``: none | clear | delete.
    The species' scientific name is captured *before* the purge — afterwards there are no
    rows left to look it up from, and ingest needs it to recognise Frigate's
    scientific-name labels (see ``ingest._blacklist_keys``).
    """
    action = _norm_source_action(source_action)
    scientific = await run_in_threadpool(db.scientific_name_for, species)

    rows = await run_in_threadpool(db.delete_species, species)
    source_errors = []
    for det in rows:
        ingest.add_tombstone(det["source"], det["source_ref"])
        result = await _source_action(request.app.state.settings, det, action)
        if result and not result["ok"]:
            source_errors.append(f"{det['source']} {det['source_ref']}: {result['error']}")
    ingest.forget_species(species)

    await run_in_threadpool(db.blacklist_add, species, scientific, 1 if rows else 0)
    ingest.add_blacklist(species, scientific)
    return {
        "ok": True,
        "species": species,
        "scientific_name": scientific,
        "deleted": len(rows),
        "source_errors": source_errors[:5],
        "source_error_count": len(source_errors),
    }


@router.delete("/blacklist/{name:path}")
async def remove_blacklist(name: str):
    """Un-blacklist a species so it can be ingested again.

    Detections purged when it was blacklisted are not restored — they're gone.
    """
    entries = await run_in_threadpool(db.blacklist_entries)
    match = next((e for e in entries if e["common_name"].lower() == name.strip().lower()), None)
    removed = await run_in_threadpool(db.blacklist_remove, name)
    if not removed:
        return {"ok": False, "error": "not blacklisted"}
    ingest.remove_blacklist(name, match.get("scientific_name") if match else None)
    return {"ok": True, "species": name}


# ----------------------------------------------------------------------------- theme

@router.post("/theme")
async def set_theme_endpoint(theme: str = Query(..., min_length=1)):
    """Persist the UI theme ('auto' or 'dex'). Applies to every client immediately."""
    value = await run_in_threadpool(set_theme, theme)
    return {"ok": True, "theme": value}


@router.post("/test-notification")
async def test_notification():
    """Fire a test ``aviary_detection`` event so notifications can be verified
    without waiting for a real detection. Uses the latest detection (real image
    pipeline included), marked as a new species so the default blueprint settings
    notify; returns the delivery status for troubleshooting."""
    rows = await run_in_threadpool(db.recent_detections, 1)
    row = rows[0] if rows else {
        # Empty database: fire a synthetic audio detection with no image.
        "source": "birdnet",
        "source_ref": "aviary-test",
        "common_name": "Aviary Test Bird",
        "scientific_name": None,
        "confidence": 1.0,
        "location": "Aviary",
        "start_time": time.time(),
    }
    return await notify.send_detection(dict(row), is_new=True, test=True)


@router.get("/species-info")
async def species_info_endpoint(
    name: str = Query(..., min_length=1),
    sci: Optional[str] = Query(None),
):
    """Wikipedia blurb + iNaturalist taxonomy + bundled ecological traits for a species.

    Cached and lazy-loaded. ``traits`` (diet/foraging/habitat) comes from the local AVONET
    subset, keyed on the scientific name — preferring the one iNaturalist resolved, since
    Frigate-only species often have none recorded. It is None when the species isn't in
    the table.
    """
    info = await species_info.resolve(name, sci)
    scientific = info.get("scientific_name") or sci
    # First call decompresses the bundled table; keep that off the event loop.
    info["traits"] = await run_in_threadpool(traits.lookup, scientific)
    return info


@router.get("/reference-audio")
async def reference_audio(
    request: Request,
    name: str = Query(..., min_length=1),
    sci: Optional[str] = Query(None),
):
    """Metadata for a species' reference recordings (cached; lazy-loaded).

    Returns every variant that resolved — ``song`` and ``call`` from xeno-canto, or the
    untyped ``any`` from iNaturalist — so the page can offer a button per sound type.

    ``media_url`` points at Aviary's own proxy rather than the provider, so the upstream
    URL stays server-side and playback works on http-served instances. Attribution and
    licence are always returned — the UI is required to display them.
    """
    variants = await species_audio.resolve_all(name, sci)
    return {
        "ok": bool(variants),
        "variants": {
            kind: {
                "provider": info["provider"],
                "attribution": info["attribution"],
                "license_code": info["license_code"],
                "quality": info["quality"],
                "source_url": info["source_url"],
                "media_url": ingress_url(
                    request, "species_reference_audio", name=name
                ) + f"?kind={kind}",
            }
            for kind, info in variants.items()
        },
    }
