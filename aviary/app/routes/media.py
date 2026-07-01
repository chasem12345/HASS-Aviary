"""Media proxy endpoints — stream clips/snapshots/audio live from the source."""

from __future__ import annotations

from fastapi import APIRouter, Request
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


@router.get("/birdnet/{det_id}/clip")
async def birdnet_clip(det_id: int, request: Request):
    base = request.app.state.settings.birdnet_url
    if not base:
        return JSONResponse({"error": "birdnet_url not configured"}, status_code=503)
    det = db.detection_by_id(det_id)
    if not det or not det.get("clip_ref"):
        return JSONResponse({"error": "no clip for detection"}, status_code=404)
    return await proxy.stream_upstream(request, proxy.birdnet_clip_url(base, det["clip_ref"]))
