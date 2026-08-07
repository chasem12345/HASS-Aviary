"""Media proxy endpoints — stream clips/snapshots/audio live from the source."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from .. import db, proxy, species_audio, species_info
from ..notify import species_slug

log = logging.getLogger("aviary.media")

_FFMPEG_TIMEOUT_S = 120

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


@router.get("/species/{name}/reference-audio", name="species_reference_audio")
async def species_reference_audio(name: str, request: Request, kind: str = "song"):
    """Stream one of a species' reference recordings from its provider.

    ``kind`` selects the variant — ``song``/``call`` (xeno-canto) or ``any`` (the
    iNaturalist fallback); the API endpoint tells the page which ones exist.

    Proxied rather than linked directly so the upstream URL — and the xeno-canto API key
    that may be needed to fetch it — stays server-side, and so the audio loads on
    http-served Home Assistant instances. Needs no configured upstream — unlike the
    Frigate/BirdNET-Go media routes, this works on a fresh install.
    """
    sci = await run_in_threadpool(db.scientific_name_for, name)
    url = await species_audio.file_url(name, sci, kind)
    if not url:
        return JSONResponse({"error": "no reference audio for species"}, status_code=404)
    return await proxy.stream_upstream(
        request, url, headers={"User-Agent": species_info.USER_AGENT}
    )


@router.get("/frigate/{event_id}/download.mp4")
async def frigate_download(event_id: str, request: Request):
    """Download a clip as a standard MP4 with a correct duration header.

    Frigate serves clips as streaming (fragmented) MP4s whose header declares zero
    duration — they play, but fail upload validation elsewhere (e.g. Discord sees an
    "empty" video). Remux with ffmpeg (-c copy: lossless, fast) into a regular
    faststart MP4 before sending; if ffmpeg is unavailable or fails, fall back to
    the original bytes so the download still works.
    """
    base = request.app.state.settings.frigate_url
    if not base:
        return JSONResponse({"error": "frigate_url not configured"}, status_code=503)

    tmpdir = tempfile.mkdtemp(prefix="aviary-dl-")
    cleanup = BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True)
    src = os.path.join(tmpdir, "src.mp4")
    out = os.path.join(tmpdir, "out.mp4")

    if not await proxy.fetch_to_file(proxy.frigate_clip_url(base, event_id), src):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return JSONResponse({"error": "could not fetch clip from Frigate"}, status_code=502)

    path = src
    if shutil.which("ffmpeg"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", out,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT_S)
            if proc.returncode == 0 and os.path.getsize(out) > 0:
                path = out
            else:
                log.warning("ffmpeg remux failed for %s (rc=%s): %s",
                            event_id, proc.returncode, (stderr or b"")[-300:])
        except (asyncio.TimeoutError, OSError) as exc:
            log.warning("ffmpeg remux errored for %s: %s", event_id, exc)
    else:
        log.warning("ffmpeg not found; serving the original clip for %s", event_id)

    # Friendly filename: species + local detection time.
    det = await run_in_threadpool(db.detection_by_ref, "frigate", event_id)
    if det:
        stamp = datetime.fromtimestamp(det.get("start_time") or time.time()).strftime("%Y%m%d-%H%M%S")
        fname = f"{species_slug(det['common_name'])}-{stamp}.mp4"
    else:
        fname = f"aviary-{event_id}.mp4"
    return FileResponse(path, media_type="video/mp4", filename=fname, background=cleanup)


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
