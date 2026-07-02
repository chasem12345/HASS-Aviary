"""Media proxy endpoints — stream clips/snapshots/audio live from the source."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import db, proxy

router = APIRouter()


@router.get("/frigate/{event_id}/clip.mp4")
async def frigate_clip(event_id: str, request: Request):
    base = request.app.state.settings.frigate_url
    if not base:
        return JSONResponse({"error": "frigate_url not configured"}, status_code=503)
    return await proxy.stream_upstream(request, proxy.frigate_clip_url(base, event_id))


@router.get("/frigate/{event_id}/snapshot.jpg")
async def frigate_snapshot(event_id: str, request: Request, thumbnail: bool = False):
    base = request.app.state.settings.frigate_url
    if not base:
        return JSONResponse({"error": "frigate_url not configured"}, status_code=503)
    url = proxy.frigate_snapshot_url(base, event_id, thumbnail=thumbnail)
    return await proxy.stream_upstream(request, url)


@router.get("/species/{name}/image")
async def species_image(name: str, request: Request):
    """Generic species photo (by common name) from BirdNET-Go's image cache.

    Used as a fallback wherever there's no Frigate snapshot — e.g. audio-only species.
    Returns 404 when the species has no scientific name on record (Frigate-only species)
    or the BirdNET-Go build doesn't have the endpoint; templates fall back via onerror.
    """
    base = request.app.state.settings.birdnet_url
    if not base:
        return JSONResponse({"error": "birdnet_url not configured"}, status_code=503)
    sci = await run_in_threadpool(db.scientific_name_for, name)
    if not sci:
        return JSONResponse({"error": "no scientific name for species"}, status_code=404)
    return await proxy.stream_upstream(request, proxy.birdnet_species_image_url(base, sci))


@router.get("/birdnet/{det_id}/clip")
async def birdnet_clip(det_id: int, request: Request):
    base = request.app.state.settings.birdnet_url
    if not base:
        return JSONResponse({"error": "birdnet_url not configured"}, status_code=503)
    # SQLite access is blocking (up to its lock timeout); keep it off the event loop.
    det = await run_in_threadpool(db.detection_by_id, det_id)
    urls = proxy.birdnet_audio_urls(base, det) if det else []
    if not urls:
        return JSONResponse({"error": "no clip for detection"}, status_code=404)
    return await proxy.stream_upstream(request, urls[0], fallbacks=tuple(urls[1:]))


@router.get("/birdnet/{det_id}/spectrogram")
async def birdnet_spectrogram(det_id: int, request: Request):
    base = request.app.state.settings.birdnet_url
    if not base:
        return JSONResponse({"error": "birdnet_url not configured"}, status_code=503)
    det = await run_in_threadpool(db.detection_by_id, det_id)
    urls = proxy.birdnet_spectrogram_urls(base, det) if det else []
    if not urls:
        return JSONResponse({"error": "no spectrogram for detection"}, status_code=404)
    return await proxy.stream_upstream(request, urls[0], fallbacks=tuple(urls[1:]))
