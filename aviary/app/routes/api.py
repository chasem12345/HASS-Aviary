"""JSON endpoints backing the Chart.js visualizations and live refresh."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool

from .. import (
    bootstrap, db, identify, ingest, notify, probe, proxy, species_audio, species_info,
    species_photos, traits,
)
from . import ingress_url, set_theme

log = logging.getLogger("aviary.api")

router = APIRouter()

# Strong references to fire-and-forget tasks (the event loop keeps only weak ones).
_background_tasks: set = set()


def _norm_source(source: Optional[str]) -> Optional[str]:
    return source if source in ("frigate", "birdnet") else None


@router.get("/summary")
def summary(request: Request, source: Optional[str] = Query(None),
            days: int = Query(7, ge=1, le=3650)):
    src = _norm_source(source)
    since = time.time() - days * 86400
    # Same confirmation filter as the dashboard view, or the JSON and the page it backs
    # would report different species counts.
    gated = request.app.state.settings.require_species_confirmation
    return {
        "stats": db.summary_stats(source=src, since=since, only_confirmed=gated),
        "top_species": db.top_species(limit=10, source=src, since=since, only_confirmed=gated),
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
            status, text = await _birdnet_delete(settings.birdnet_url, det["native_id"])
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"source unreachable: {exc}"}
    if status >= 400:
        return {"ok": False, "error": f"source returned {status}: {text}"}
    return {"ok": True, "error": None}


async def _birdnet_delete(base: str, native_id: str) -> tuple[int, str]:
    """DELETE a BirdNET-Go detection, satisfying its CSRF middleware.

    BirdNET-Go compares an ``X-CSRF-Token`` header against a ``csrf`` cookie; the shared
    proxy client replays the cookie, so only the header needs adding. A 403 is retried
    once with a freshly minted token — the cached one goes stale whenever BirdNET-Go
    restarts, and the first delete after that would otherwise fail for no visible reason.
    """
    url = proxy.birdnet_detection_url(base, native_id)
    token = await proxy.birdnet_csrf_token(base)
    status, text = await proxy.call_upstream(
        "DELETE", url, headers={"X-CSRF-Token": token} if token else None
    )
    if status == 403:
        token = await proxy.birdnet_csrf_token(base, refresh=True)
        if token:
            status, text = await proxy.call_upstream(
                "DELETE", url, headers={"X-CSRF-Token": token}
            )
        if status == 403:
            # Distinguish this from an auth failure, which also surfaces as 403 once
            # BirdNET-Go has authentication enabled.
            text = f"{text} (CSRF token rejected; is BirdNET-Go authentication enabled?)"
    return status, text


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
    # A deleted detection is usually a misclassification — the probe must stop learning
    # from its embedding immediately, not at the next restart.
    await _refresh_probe()
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
    # Its embeddings and its confirmation are gone; the probe must unlearn them now.
    await _refresh_probe()
    return {
        "ok": True,
        "deleted": len(rows),
        "source_errors": source_errors[:5],
        "source_error_count": len(source_errors),
    }


# ----------------------------------------------------------------------- identification

@router.post("/detections/{det_id}/identify")
async def reidentify(
    det_id: int,
    reject: int = Query(0, description="record the current species as wrong, then re-run"),
    reset: int = Query(0, description="clear this detection's rejections first"),
):
    """Re-run identification for one detection, waiting for the answer.

    Three uses, all the same call:

    * plain — retry after the GPU host was down, or after changing a threshold.
    * ``reject=1`` — "that's not what it is". The current species is remembered as wrong
      for this detection and ruled out of the candidate set, so the model returns its next
      best answer rather than the same one. Press it repeatedly to walk down the ranking.
    * ``reset=1`` — clear the rejections and start over, for when they have narrowed
      things down to nonsense.
    """
    if not identify.enabled():
        return {"ok": False, "error": "identification is not configured"}
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if det is None:
        return {"ok": False, "error": "detection not found"}
    if det["source"] != "frigate":
        # BirdNET rows are audio; there is no image to identify.
        return {"ok": False, "error": "only Frigate detections can be identified"}

    if reset:
        await run_in_threadpool(db.clear_rejections, det_id)
    rejected_name = None
    if reject and det.get("common_name"):
        rejected_name = det["common_name"]
        await run_in_threadpool(db.reject_identification, det_id, rejected_name)
        # Clear the name before re-running. If the reroll lands below threshold it never
        # re-stores the row, and without this the species the user just rejected would
        # stay on the card. See db.reset_species.
        await run_in_threadpool(db.reset_species, det_id)
        det = dict(det, common_name="bird", scientific_name=None, species_code=None)

    result = await identify.identify_one(det)
    if result.get("status") == "already_running":
        return {"ok": False, "error": "identification already in progress"}
    updated = result.get("detection") or {}
    # If that was the rejected species' only detection, let it announce as new again
    # should it genuinely turn up later — same reasoning as deleting a misclassification.
    if rejected_name and rejected_name != updated.get("common_name"):
        await run_in_threadpool(_forget_if_gone, rejected_name)
    rejected = await run_in_threadpool(db.rejections_for, det_id)
    return {
        "ok": True,
        "common_name": updated.get("common_name"),
        "id_status": updated.get("id_status"),
        "score": updated.get("id_score"),
        "margin": updated.get("id_margin"),
        "rejected": rejected,
    }


@router.post("/detections/{det_id}/retain")
async def retain_detection(det_id: int, request: Request,
                           keep: int = Query(1, description="1 = keep forever, 0 = release")):
    """Pin (or release) this event's clip as kept-forever at Frigate.

    Flips Frigate's ``retain_indefinitely`` flag on the event, exempting it from
    Frigate's normal retention expiry, and records the state here so the card shows it
    and Aviary's own unidentified-row purge leaves the row alone.
    """
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if det is None:
        return {"ok": False, "error": "detection not found"}
    if det["source"] != "frigate":
        return {"ok": False, "error": "only Frigate events have clips to retain"}
    settings = request.app.state.settings
    if not settings.frigate_url:
        return {"ok": False, "error": "frigate_url not configured"}

    url = proxy.frigate_retain_url(settings.frigate_url, det["source_ref"])
    try:
        status, text = await proxy.call_upstream("POST" if keep else "DELETE", url)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Frigate unreachable: {exc}"}
    if status >= 400:
        return {"ok": False, "error": f"Frigate returned {status}: {text}"}

    await run_in_threadpool(db.set_retained, det_id, bool(keep))
    return {"ok": True, "retained": bool(keep)}


@router.post("/detections/{det_id}/species")
async def set_species(det_id: int, species: str = Query(..., min_length=1),
                      scientific: Optional[str] = Query(None)):
    """Name a detection by hand.

    The last word when identification cannot get there: the user picks one of the model's
    own candidates, or types the species themselves. Also the answer to "it failed and I
    can see perfectly well what it is".
    """
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if det is None:
        return {"ok": False, "error": "detection not found"}

    name = species.strip()
    if not name or name.lower() == db.UNNAMED:
        return {"ok": False, "error": "that is not a species name"}
    if await run_in_threadpool(db.is_blacklisted_name, name):
        return {"ok": False, "error": f"{name} is blacklisted; remove it from the "
                                      f"blacklist first"}

    # Fill in the scientific name from anything already known about the species, so a
    # manually-named detection is as complete as an automatic one.
    sci = (scientific or "").strip() or await run_in_threadpool(db.scientific_name_for, name)
    updated = await run_in_threadpool(db.set_species_manually, det_id, name, sci)
    if updated is None:
        return {"ok": False, "error": "detection not found"}

    # A person naming a species is a stronger signal than any classifier, so it does not
    # also need to queue for confirmation — that gate exists to stop misclassifications
    # inflating the registry, which is not what this is.
    await run_in_threadpool(db.confirm_species, name)
    await _refresh_probe()
    # If this detection has no stored embedding (its identification failed, or predates
    # embeddings), the label just given teaches the probe nothing. Harvest one in the
    # background — it costs a GPU round-trip, and the user shouldn't wait on it.
    task = asyncio.create_task(_backfill_then_refresh(dict(det)))
    # The loop only holds weak references to tasks; keep one or it can be GC'd mid-run.
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    previous = det.get("common_name")
    if previous and previous != name:
        await run_in_threadpool(_forget_if_gone, previous)
    return {"ok": True, "common_name": name, "scientific_name": sci}


async def _backfill_then_refresh(det: dict) -> None:
    try:
        if await identify.backfill_embedding(det):
            await _refresh_probe()
    except Exception:  # a background task's exception would otherwise vanish silently
        log.exception("Embedding backfill failed for detection %s", det.get("id"))


@router.get("/identify-species")
async def identify_species():
    """The identification service's candidate species list, for the manual-entry picker.

    Proxied rather than fetched from the browser: the service may not be reachable from
    wherever the UI is open, and its bearer token must not reach the page.
    """
    return await identify.species_list()


@router.get("/probe")
async def probe_stats():
    """What the few-shot probe has learned so far."""
    return probe.stats()


@router.post("/probe/rebuild")
async def probe_rebuild(request: Request):
    """Reload the probe's examples from confirmed detections. Cheap; safe to call any time."""
    model = await identify.probe_model()
    if not model:
        return {"ok": False, "error": "identification service unreachable"}
    return {"ok": True, **await run_in_threadpool(probe.rebuild, model)}


@router.post("/probe/bootstrap")
async def probe_bootstrap(request: Request):
    """Embed cached reference photos so brand-new species start with examples."""
    model = await identify.probe_model()
    if not model:
        return {"ok": False, "error": "identification service unreachable"}
    bootstrap.start(request.app.state.settings, model)
    return {"ok": True, "started": True}


@router.get("/probe/evaluate")
async def probe_evaluate():
    """Leave-one-out accuracy over your own confirmed birds.

    The honest measure of whether the probe helps *here*, rather than on a benchmark. It
    scores stored embeddings against example pools rebuilt without them, so it touches no
    clips and is unaffected by changes to the crop pipeline.
    """
    model = await identify.probe_model()
    if not model:
        return {"ok": False, "error": "identification service unreachable"}
    return {"ok": True, **await run_in_threadpool(probe.evaluate, model)}


@router.get("/identify-health")
async def identify_health():
    """Status of the companion identification service, for the settings page."""
    return await identify.health()


# --------------------------------------------------------------------- confirmation
# Top-level for the same reason as the blacklist routes below: a path nested under
# /species/{name} would be swallowed by `DELETE /species/{name:path}`.
#
# Rejecting a species needs no endpoint of its own — it reuses DELETE /species/{name}
# and POST /blacklist, which already purge, tombstone, act at the source and forget the
# species. Confirming is the only genuinely new verb.

async def _refresh_probe() -> None:
    """Rebuild the probe's examples after the set of confirmed labels changes.

    Confirming a species is what turns its detections into training examples — and
    unconfirming or deleting one is what should make the probe forget them — so the probe
    should reflect either at once rather than at the next restart. A full rebuild is cheap
    enough (a few hundred species of 768 floats) that incremental updating would be more
    code and more ways to go stale.

    Deliberately NOT gated on probe.ready(): an empty probe (fresh install, or a service
    that was unreachable at startup) is exactly the one that must be buildable by the
    first confirmation, and the old early-return silently discarded every label's rebuild
    until a restart.
    """
    if not identify.enabled():
        return
    model = await identify.probe_model()
    if model:
        await run_in_threadpool(probe.rebuild, model)


@router.post("/species-confirm")
async def confirm_species(species: str = Query(..., min_length=1)):
    """Approve a species into the registry, giving it a dex number and its place in the stats."""
    await run_in_threadpool(db.confirm_species, species)
    await _refresh_probe()
    return {"ok": True, "species": species, "confirmed": True}


@router.delete("/species-confirm/{name:path}")
async def unconfirm_species(name: str):
    """Send an approved species back to the review queue (undo a mistaken confirmation)."""
    removed = await run_in_threadpool(db.unconfirm_species, name)
    if not removed:
        return {"ok": False, "error": "species was not confirmed"}
    # Unconfirmed means its detections are no longer trusted labels; unlearn them now.
    await _refresh_probe()
    return {"ok": True, "species": name, "confirmed": False}


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
    # The purge above deleted the species' detections and confirmation; unlearn them.
    await _refresh_probe()
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


@router.get("/reference-photos")
async def reference_photos(
    request: Request,
    name: str = Query(..., min_length=1),
    sci: Optional[str] = Query(None),
):
    """Licensed reference photos for a species (cached; lazy-loaded).

    ``media_url`` points at Aviary's own proxy so the upstream URL stays server-side.
    Attribution is always returned — the UI is required to display it.
    """
    photos = await species_photos.resolve(name, sci)
    return {
        "ok": bool(photos),
        "photos": [
            {
                "attribution": p["attribution"],
                "license_code": p["license_code"],
                "source_url": p["source_url"],
                "media_url": ingress_url(
                    request, "species_reference_photo", name=name, position=p["position"]
                ),
            }
            for p in photos
        ],
    }


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
