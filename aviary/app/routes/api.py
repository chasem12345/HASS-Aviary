"""JSON endpoints backing the Chart.js visualizations and live refresh."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from .. import (
    backfill, bootstrap, crops, db, identify, inat, ingest, keepsakes, kept, notbird, notify, probe,
    proxy,
    seasonality, species_audio, species_info, species_photos, traits,
)
from .. import hastats
from . import ingress_url, norm_source, set_theme

log = logging.getLogger("aviary.api")

router = APIRouter()

# Strong references to fire-and-forget tasks (the event loop keeps only weak ones).
_background_tasks: set = set()


@router.get("/summary")
def summary(request: Request, source: Optional[str] = Query(None),
            days: int = Query(7, ge=1, le=3650)):
    src = norm_source(source)
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
        days=days, source=norm_source(source), species=species, since=since
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
        source=norm_source(source), since=effective_since, species=species
    )
    return {"data": data}


@router.get("/latest")
def latest(source: Optional[str] = Query(None), species: Optional[str] = Query(None)):
    """Cheap change marker polled by the Recent page for live refresh."""
    return db.change_marker(source=norm_source(source), species=species)


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
    deleted = await run_in_threadpool(db.delete_detection, det_id)
    ingest.add_tombstone(det["source"], det["source_ref"])
    await run_in_threadpool(crops.remove, det["source_ref"])
    await run_in_threadpool(_forget_if_gone, det["common_name"])
    # Any species keepsake that sat on this event is released and re-picked.
    if deleted:
        await keepsakes.on_detection_deleted(deleted)
    # A deleted detection is usually a misclassification — the probe must stop learning
    # from its embedding immediately, not at the next restart.
    await _refresh_probe()
    hastats.touch()
    return {"ok": True, "common_name": det["common_name"], "source_result": source_result}


@router.delete("/species/{name:path}")
async def delete_species(name: str, request: Request, source_action: Optional[str] = Query(None)):
    """Remove every detection of a species (e.g. a misclassification-only species),
    tombstoning each ref, optionally clearing/deleting them at the source too."""
    action = _norm_source_action(source_action)
    # A species can exist only as a named other-bird in view (no tracked detections of
    # its own); delete_species rules those out too, so "found" is judged beforehand.
    existed = bool((await run_in_threadpool(db.species_stats, name)).get("total") or 0)
    # Its keepsakes go first, while their events still exist to be released at Frigate.
    await keepsakes.forget(name)
    rows = await run_in_threadpool(db.delete_species, name)
    if not rows and not existed:
        return {"ok": False, "error": "species not found", "deleted": 0}
    source_errors = []
    for det in rows:
        ingest.add_tombstone(det["source"], det["source_ref"])
        await run_in_threadpool(crops.remove, det["source_ref"])
        result = await _source_action(request.app.state.settings, det, action)
        if result and not result["ok"]:
            source_errors.append(f"{det['source']} {det['source_ref']}: {result['error']}")
        # A two-bird event kept as ANOTHER species' keepsake went with this delete.
        if det.get("keepsakes"):
            await keepsakes.on_detection_deleted(det)
    ingest.forget_species(name)
    # Its embeddings and its confirmation are gone; the probe must unlearn them now.
    await _refresh_probe()
    hastats.touch()  # its sensor is removed from Home Assistant
    # The local link to its iNaturalist observation goes too — the observation itself is
    # the user's, and stays until they delete it there; the URL is handed back for that.
    inat_row = await run_in_threadpool(db.inat_forget, name)
    return {
        "ok": True,
        "deleted": len(rows),
        "source_errors": source_errors[:5],
        "source_error_count": len(source_errors),
        "inat_url": (inat_row or {}).get("observation_url"),
    }


# ----------------------------------------------------------------------- identification

@router.post("/detections/{det_id}/identify")
async def reidentify(
    det_id: int,
    reject: int = Query(0, description="record the current species as wrong, then re-run"),
    reset: int = Query(0, description="clear this detection's rejections first"),
    force: int = Query(0, description="re-run even on a detection named by hand"),
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
    if det.get("id_status") == "manual" and not (force or reject):
        # A plain re-run would overwrite the person's label with the service's answer —
        # the one thing labelling by hand exists to override. Rejecting it is different:
        # that IS the person saying the label was wrong.
        return {"ok": False, "error": "this detection was named by hand; re-identifying "
                                      "would discard that label. Use ✗ wrong to reject "
                                      "the name, or force=1 to re-run anyway"}

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


class BulkIds(BaseModel):
    """Detection ids for a bulk action, from the Unidentified page's select mode."""
    ids: list[int]


# Caps on one bulk call. Not limits on what the user can do — the UI can call again —
# but a bound on how much one request is allowed to chew through.
_BULK_IDENTIFY_MAX = 500
_BULK_DELETE_MAX = 1000


@router.post("/detections/bulk-identify")
async def bulk_reidentify(payload: BulkIds):
    """Queue many detections for re-identification, without waiting for answers.

    Deliberately asynchronous where the single re-identify holds for the result: a
    backlog (say, after the GPU host was down) is drained by the normal identification
    workers, which already pace one another against the GPU. Rows still land in
    'pending' and resolve as results come back.

    ``skipped`` counts ids that were not queued: unknown ids, audio (BirdNET) rows,
    detections already in flight, or a full queue — re-run the action later for those.
    """
    if not identify.enabled():
        return {"ok": False, "error": "identification is not configured"}
    queued = skipped = 0
    for det_id in payload.ids[:_BULK_IDENTIFY_MAX]:
        det = await run_in_threadpool(db.detection_by_id, det_id)
        if det is None or det["source"] != "frigate":
            skipped += 1
            continue
        # Mark it pending like ingest does, BEFORE submitting so the status can't land
        # after the worker's result. The card badge tells the truth while the queue
        # drains, and — because the startup requeue resubmits pending rows — a restart
        # mid-drain resumes the backlog instead of stranding the remainder. Both
        # submit-failure paths self-correct: a full queue overwrites this with
        # 'failed', and an already-in-flight row gets its real status from its worker.
        await run_in_threadpool(
            db.set_identification, det["source"], det["source_ref"], "pending"
        )
        if identify.submit(det):
            queued += 1
        else:
            skipped += 1
    log.info("Bulk re-identify: queued %d, skipped %d.", queued, skipped)
    return {"ok": True, "queued": queued, "skipped": skipped}


@router.post("/detections/bulk-delete")
async def bulk_delete(payload: BulkIds):
    """Delete many detections at once.

    Same bookkeeping as the single delete — tombstoned so backfill can't re-import
    them, species forgotten if nothing remains, probe refreshed — but with no
    ``source_action``: bulk delete never touches Frigate/BirdNET-Go. Removing events
    at the source in bulk is exactly the kind of irreversible sweep that should stay
    a deliberate per-detection choice.
    """
    deleted = 0
    names: set[str] = set()
    for det_id in payload.ids[:_BULK_DELETE_MAX]:
        det = await run_in_threadpool(db.detection_by_id, det_id)
        if det is None:
            continue
        await run_in_threadpool(db.delete_detection, det_id)
        ingest.add_tombstone(det["source"], det["source_ref"])
        await run_in_threadpool(crops.remove, det["source_ref"])
        names.add(det["common_name"])
        deleted += 1
    for name in names:
        await run_in_threadpool(_forget_if_gone, name)
    # One refresh at the end, not per row — same reasoning as the single delete's
    # immediate refresh, minus the N-1 redundant rebuilds.
    if deleted:
        await _refresh_probe()
    log.info("Bulk delete removed %d detection(s).", deleted)
    return {"ok": True, "deleted": deleted}


@router.post("/detections/{det_id}/retain")
async def retain_detection(det_id: int, request: Request,
                           keep: int = Query(1, description="1 = keep forever, 0 = release")):
    """Pin (or release) this event's clip as kept-forever at Frigate.

    Flips Frigate's ``retain_indefinitely`` flag on the event, exempting it from
    Frigate's normal retention expiry, and records the state here so the card shows it
    and Aviary's own unidentified-row purge leaves the row alone.

    On a paired camera (identify_zoom_map), pinning ALSO exports the partner camera's
    recordings for the event window — the zoomed footage, which the retain flag cannot
    reach. Export failure never fails the pin: the wide clip is already protected, and
    ``zoomed_export`` in the response says what happened to the other half.
    """
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if det is None:
        return {"ok": False, "error": "detection not found"}
    if det["source"] != "frigate":
        return {"ok": False, "error": "only Frigate events have clips to retain"}
    settings = request.app.state.settings
    if not settings.frigate_url:
        return {"ok": False, "error": "frigate_url not configured"}

    # A species keepsake on the same event holds Frigate's flag in its own right: the
    # user's pin comes off, the event stays kept, and the response says why.
    held_by_keepsake = bool(await run_in_threadpool(db.keepsake_roles, det_id))
    if keep or not held_by_keepsake:
        err = await kept.set_frigate_retain(settings, det["source_ref"], bool(keep))
        if err == kept.GONE:
            if keep:
                return {"ok": False, "error": "Frigate no longer has this event"}
            err = None  # nothing left at Frigate to release; clear our side anyway
        if err:
            return {"ok": False, "error": err}

    await run_in_threadpool(db.set_retained, det_id, bool(keep))

    zoomed: Optional[str] = None
    if keep:
        export_id, zoomed = await kept.create_kept_export(det, settings)
        if export_id:
            await run_in_threadpool(db.set_kept_export, det_id, export_id)
        if zoomed == "not applicable":
            zoomed = None
        elif zoomed.startswith("failed"):
            log.warning("Kept export for detection %s %s", det_id, zoomed)
    elif det.get("kept_export_id"):
        # Best-effort: the pin is released regardless. An export already gone must not
        # block unpinning, and one still processing can be removed from Frigate's own
        # Export UI later — the response says so instead of wedging the toggle.
        zoomed = await kept.delete_kept_export(det["kept_export_id"], settings)
        if zoomed:
            log.warning("Kept export for detection %s %s", det_id, zoomed)
        await run_in_threadpool(db.set_kept_export, det_id, None, None)

    result = {"ok": True, "retained": bool(keep)}
    if zoomed:
        result["zoomed_export"] = zoomed
    if not keep and held_by_keepsake:
        result["held_by"] = "keepsake"
    return result


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
    # Make the stored example a frame of the bird just NAMED. A missing embedding (the
    # identification failed, or predates embeddings) is harvested; one chosen for a
    # different species is replaced — see _reembed_then_refresh. In the background: it
    # costs a GPU round-trip, and the user shouldn't wait on it.
    _spawn(_reembed_then_refresh(dict(det), name, 0))
    previous = det.get("common_name")
    if previous and previous != name:
        await run_in_threadpool(_forget_if_gone, previous)
        keepsakes.schedule(previous)  # it may have lost its first or latest
    keepsakes.schedule(name)
    hastats.touch()
    return {"ok": True, "common_name": name, "scientific_name": sci}


@router.get("/detections/{det_id}/subjects")
async def list_subjects(det_id: int):
    """Every bird the identification service found in this detection's event."""
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if det is None:
        return {"ok": False, "error": "detection not found"}
    return {"ok": True, "subjects": await run_in_threadpool(db.subjects_for, det_id)}


@router.post("/detections/{det_id}/subjects/{idx}/species")
async def set_subject_species(det_id: int, idx: int, species: str = Query(..., min_length=1),
                              scientific: Optional[str] = Query(None)):
    """Name one OTHER bird in an event by hand.

    The primary (idx 0) is the detection itself and goes through ``set_species`` so the
    two paths cannot drift. A label here is stored on that bird's own row and its own
    embedding — the whole point of subjects: it can never train the probe on a picture
    of the tracked bird.
    """
    if idx == 0:
        return await set_species(det_id, species, scientific)
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if det is None:
        return {"ok": False, "error": "detection not found"}
    name = species.strip()
    if not name or name.lower() == db.UNNAMED:
        return {"ok": False, "error": "that is not a species name"}
    if await run_in_threadpool(db.is_blacklisted_name, name):
        return {"ok": False, "error": f"{name} is blacklisted; remove it from the "
                                      f"blacklist first"}
    sci = (scientific or "").strip() or await run_in_threadpool(db.scientific_name_for, name)
    ok = await run_in_threadpool(db.set_subject_species_manually, det_id, idx, name, sci)
    if not ok:
        return {"ok": False, "error": "no such bird in this detection"}
    # A person naming it is the strongest signal there is — same reasoning as set_species.
    await run_in_threadpool(db.confirm_species, name)
    await _refresh_probe()
    _spawn(_reembed_then_refresh(dict(det), name, idx))
    keepsakes.schedule(name)
    hastats.touch()
    return {"ok": True, "common_name": name, "scientific_name": sci}


@router.post("/detections/{det_id}/subjects/{idx}/reject")
async def reject_subject_species(det_id: int, idx: int, species: str = Query(..., min_length=1)):
    """"That other bird is not a <species>." Recorded for this bird only; no GPU call —
    its own shortlist is already stored, so the card offers the next candidate."""
    if idx == 0:
        return {"ok": False, "error": "use ✗ wrong on the detection for the tracked bird"}
    ok = await run_in_threadpool(db.reject_subject, det_id, idx, species.strip())
    if not ok:
        return {"ok": False, "error": "no such bird in this detection"}
    await _refresh_probe()
    keepsakes.schedule(species.strip())  # this event may have been its first or latest
    hastats.touch()
    remaining = [s for s in await run_in_threadpool(db.secondary_subjects_for, det_id)
                 if s["idx"] == idx]
    return {"ok": True, "subject": remaining[0] if remaining else None}


# ------------------------------------------------------------------------ not a bird

def _learn_not_bird(det: dict, idx: int, embedding: tuple[str, str], label: Optional[str]) -> int:
    """Store one "Not a bird" example (with a copy of its crop). Returns its id."""
    model, vec = embedding
    ex_id = db.not_bird_add(model, vec, source_ref=det.get("source_ref"),
                            camera=det.get("location"), label=label)
    crop_file = crops.copy(det["source_ref"], idx, f"notbird-{ex_id}")
    if crop_file:
        with db._connect() as conn:  # noqa: SLF001 — one column, not worth a helper
            conn.execute("UPDATE not_bird_examples SET crop_file = ? WHERE id = ?",
                         (crop_file, ex_id))
    notbird.invalidate()
    return ex_id


@router.post("/detections/{det_id}/not-bird")
async def mark_detection_not_bird(det_id: int, request: Request,
                                  source_action: Optional[str] = Query(None)):
    """"That was not a bird": learn the crop as a negative, then remove the detection.

    ``source_action`` is the delete menu's (none | clear | delete). A later crop this
    close to it is set aside rather than named (identify_not_bird_similarity). Without
    a stored embedding there is nothing to learn from; the detection is still removed and
    ``learned`` says so.
    """
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if det is None:
        return {"ok": False, "error": "detection not found"}
    emb = await run_in_threadpool(db.embedding_for, det_id)
    label = det.get("frigate_label") or det.get("common_name")
    ex_id = None
    if emb:
        ex_id = await run_in_threadpool(_learn_not_bird, det, 0, emb, label)
    out = await delete_detection(det_id, request, source_action)
    out.update({"learned": ex_id is not None, "example_id": ex_id})
    return out


@router.post("/detections/{det_id}/subjects/{idx}/not-bird")
async def mark_subject_not_bird(det_id: int, idx: int):
    """"That other bird in view is not a bird": learn it and drop it from the event."""
    if idx <= 0:
        return {"ok": False, "error": "use Not a bird on the detection for the tracked bird"}
    det = await run_in_threadpool(db.detection_by_id, det_id)
    sub = next((s for s in await run_in_threadpool(db.subjects_for, det_id)
                if s["idx"] == idx), None)
    if det is None or sub is None:
        return {"ok": False, "error": "no such bird in this detection"}
    ex_id = None
    if sub.get("embedding") and sub.get("embedding_model"):
        ex_id = await run_in_threadpool(
            _learn_not_bird, det, idx, (sub["embedding_model"], sub["embedding"]),
            sub.get("manual_name") or sub.get("common_name"))
    await run_in_threadpool(db.drop_subject, det_id, idx)
    named = sub.get("manual_name") or sub.get("common_name")
    await _refresh_probe()
    if named:
        keepsakes.schedule(named)
    hastats.touch()
    return {"ok": True, "learned": ex_id is not None, "example_id": ex_id}


@router.get("/not-bird")
async def list_not_bird():
    """The "Not a bird" examples, newest first, with the gate's current threshold."""
    rows = await run_in_threadpool(db.not_bird_list)
    return {"ok": True, "threshold": notbird.threshold(), "examples": rows}


@router.delete("/not-bird/{example_id}")
async def forget_not_bird(example_id: int):
    """Forget one example (it was a bird after all, or too broad)."""
    row = await run_in_threadpool(db.not_bird_remove, example_id)
    if row is None:
        return {"ok": False, "error": "example not found"}
    await run_in_threadpool(crops.remove_file, row.get("crop_file"))
    notbird.invalidate()
    return {"ok": True}


def _spawn(coro) -> None:
    """Fire-and-forget on the event loop, which only holds weak references to tasks."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _reembed_then_refresh(det: dict, name: str, subject_idx: int = 0) -> None:
    """After a label: make the stored example a frame of the bird that was NAMED.

    Nothing to do when the stored frame was already chosen for this species. Otherwise,
    with a service that honours ``target`` (aviary-id 0.11.0+), the old example is
    dropped — a frame picked because it looked most like the WRONG answer must not be
    learned under the right one — and a fresh one is harvested for the label. With an
    older service the 0.31 behaviour stands: fill a missing embedding, never replace one.
    """
    try:
        det_id = det["id"]
        if subject_idx == 0:
            has, target = await run_in_threadpool(db.embedding_target_for, det_id)
        else:
            subs = await run_in_threadpool(db.subjects_for, det_id)
            mine = next((s for s in subs if s["idx"] == subject_idx), None)
            if mine is None:
                return
            has, target = bool(mine.get("embedding")), mine.get("embedding_target")
        if has and (target or "").lower() == name.lower():
            return
        if not await identify.supports_target():
            if subject_idx == 0:
                out = await identify.reembed(det, name, only_if_missing=True)
            else:
                return
        else:
            if has:
                if subject_idx == 0:
                    await run_in_threadpool(db.delete_detection_embedding, det_id)
                else:
                    await run_in_threadpool(db.clear_subject_embedding, det_id, subject_idx)
                await _refresh_probe()
            out = await identify.reembed(det, name, subject_idx=subject_idx)
        status = out.get("status")
        if status in ("ok", "untargeted"):
            await _refresh_probe()
        elif status != "kept":
            log.info("Re-embed of detection %s%s for %r did not complete: %s", det_id,
                     f" (other bird {subject_idx})" if subject_idx else "", name, out)
    except Exception:  # a background task's exception would otherwise vanish silently
        log.exception("Re-embed failed for detection %s", det.get("id"))


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


@router.post("/visits/rebuild")
async def rebuild_visits(request: Request):
    """Forget every visit and re-import Frigate's review items from scratch.

    Visits are a mirror of Frigate's review items, so there is nothing to "recompute"
    locally — this re-pulls whatever Frigate currently holds. For recovering from a
    Frigate-side change (review settings, a restore) or a suspected mismatch. Events
    whose review item Frigate no longer retains simply end up ungrouped again.
    """
    settings = request.app.state.settings
    if not settings.frigate_url:
        return {"ok": False, "error": "frigate_url not configured"}
    if not settings.frigate_review_topic:
        return {"ok": False, "error": "visits are disabled (frigate_review_topic is blank)"}
    await run_in_threadpool(db.reset_visits)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        imported = await backfill.backfill_frigate_reviews(client, settings)
    stats = await run_in_threadpool(db.visit_stats)
    return {"ok": True, "review_items": imported, **stats}


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


@router.get("/probe/examples")
async def probe_species_summary():
    """Per species: how many examples the probe holds, uses, and has flagged."""
    return {"ok": True, "species": probe.species_summary()}


@router.get("/probe/examples/{species:path}")
async def probe_examples(species: str):
    """Every labelled example of one species, with which detection each came from and
    how it sits against the other species' examples (the mislabel signal)."""
    return {"ok": True, "species": species, "examples": await run_in_threadpool(
        probe.examples, species)}


@router.post("/probe/examples/{det_id}/{idx}/exclude")
async def probe_exclude(det_id: int, idx: int):
    """"Do not learn from this one." The detection and its label stay; only the probe forgets."""
    await run_in_threadpool(db.set_learning_excluded, det_id, idx, True)
    await _refresh_probe()
    return {"ok": True, "excluded": True}


@router.delete("/probe/examples/{det_id}/{idx}/exclude")
async def probe_include(det_id: int, idx: int):
    await run_in_threadpool(db.set_learning_excluded, det_id, idx, False)
    await _refresh_probe()
    return {"ok": True, "excluded": False}


@router.post("/probe/examples/{det_id}/{idx}/reembed")
async def probe_reembed(det_id: int, idx: int):
    """Replace one example with a frame chosen for its label (aviary-id 0.11.0+).

    Synchronous: the user pressed the button and is waiting to see the new crop.
    """
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if det is None:
        return {"ok": False, "error": "detection not found"}
    if idx == 0:
        label = det.get("common_name")
    else:
        subs = await run_in_threadpool(db.subjects_for, det_id)
        mine = next((s for s in subs if s["idx"] == idx), None)
        label = (mine or {}).get("manual_name") or (mine or {}).get("common_name")
    if not label or label.lower() == db.UNNAMED:
        return {"ok": False, "error": "this example has no species to re-embed for"}
    if not await identify.supports_target():
        return {"ok": False, "error": "the identification service is older than 0.11.0 "
                                      "and cannot choose a frame for a species"}
    out = await identify.reembed(det, label, subject_idx=idx)
    if out.get("status") == "ok":
        await _refresh_probe()
        return {"ok": True, **out}
    return {"ok": False, "error": f"re-embed {out.get('status')}"
                                  + (f" ({out['service']})" if out.get("service") else ""),
            **out}


@router.post("/probe/flag")
async def probe_flag():
    """Audit every example: flag those nearer another species' examples than their own."""
    if not probe.ready():
        return {"ok": False, "error": "the probe has no examples yet"}
    return {"ok": True, **await run_in_threadpool(probe.flag_suspicious)}


@router.post("/probe/forget")
async def probe_forget():
    """Wipe every learned example — the probe's whole memory of your birds.

    Card names, the species registry and history all stay; so do reference-photo
    embeddings and any "do not learn from this one" marks. The re-harvest then runs in
    the background exactly as at startup, giving hand-named cards whose media Frigate
    still has a fresh example chosen for their label.
    """
    counts = await run_in_threadpool(db.forget_learning)
    await _refresh_probe()

    async def _reharvest() -> None:
        try:
            out = await identify.reharvest_manual(50)
            if out["recovered"] or out["retargeted"]:
                await _refresh_probe()
        except Exception:
            log.exception("Re-harvest after forgetting learning failed")

    _spawn(_reharvest())
    return {"ok": True, "forgotten": counts, "reharvesting": identify.enabled()}


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
    keepsakes.schedule(species)  # now in the registry: keep its first and latest
    inat.on_confirmed(species)   # and, with inat_auto_post, onto the life list
    hastats.touch()              # and it gets its sensor in Home Assistant
    return {"ok": True, "species": species, "confirmed": True}


@router.delete("/species-confirm/{name:path}")
async def unconfirm_species(name: str):
    """Send an approved species back to the review queue (undo a mistaken confirmation)."""
    removed = await run_in_threadpool(db.unconfirm_species, name)
    if not removed:
        return {"ok": False, "error": "species was not confirmed"}
    # Unconfirmed means its detections are no longer trusted labels; unlearn them now.
    await _refresh_probe()
    keepsakes.schedule(name)  # back out of the registry: its keepsakes are released
    hastats.touch()           # and its sensor leaves Home Assistant
    return {"ok": True, "species": name, "confirmed": False}


# ---------------------------------------------------------------- iNaturalist life list
# Top-level like the blacklist routes: `DELETE /species/{name:path}` above would swallow
# a nested "/species/<name>/inat" and delete the species instead.

@router.get("/species-inat")
async def inat_preview(species: str = Query(..., min_length=1)):
    """What posting this species would send — shown to the user before they agree."""
    return await inat.preview(species)


@router.post("/species-inat")
async def inat_post(species: str = Query(..., min_length=1)):
    """Create the species' observation on iNaturalist now (once; never a duplicate)."""
    return await inat.post_species(species)


@router.delete("/species-inat/{name:path}")
async def inat_forget(name: str):
    """Forget the local link only. The observation on iNaturalist is untouched."""
    row = await run_in_threadpool(db.inat_forget, name)
    return {
        "ok": bool(row), "species": name,
        "observation_url": (row or {}).get("observation_url"),
        "note": "The observation on iNaturalist was not touched; delete it there if you want it gone.",
    }


@router.get("/inat/status")
async def inat_status():
    """Configured / account reachable / how many posted — never any credential."""
    return await inat.status()


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
        await run_in_threadpool(crops.remove, det["source_ref"])
        result = await _source_action(request.app.state.settings, det, action)
        if result and not result["ok"]:
            source_errors.append(f"{det['source']} {det['source_ref']}: {result['error']}")
    ingest.forget_species(species)

    await run_in_threadpool(db.blacklist_add, species, scientific, 1 if rows else 0)
    ingest.add_blacklist(species, scientific)
    # The purge above deleted the species' detections and confirmation; unlearn them.
    await _refresh_probe()
    hastats.touch()
    inat_row = await run_in_threadpool(db.inat_forget, species)
    return {
        "ok": True,
        "species": species,
        "scientific_name": scientific,
        "deleted": len(rows),
        "source_errors": source_errors[:5],
        "source_error_count": len(source_errors),
        "inat_url": (inat_row or {}).get("observation_url"),
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


@router.get("/seasonality")
async def seasonality_endpoint(
    name: str = Query(..., min_length=1),
    sci: Optional[str] = Query(None),
):
    """When a species is around: iNaturalist's regional observations by month (within
    ``seasonality.RADIUS_KM`` of the Home Assistant location; None without a location),
    this yard's own sightings by month, and AVONET's migration class and body mass.
    """
    region = await seasonality.resolve(name, sci)
    yard = await run_in_threadpool(db.monthly_counts, name)
    info = await species_info.resolve(name, sci)
    scientific = info.get("scientific_name") or sci
    tr = await run_in_threadpool(traits.lookup, scientific) or {}
    return {
        "region": region,
        "yard": yard,
        "migration": tr.get("migration"),
        "mass_g": tr.get("mass_g"),
        "month_now": time.localtime().tm_mon,
    }


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
