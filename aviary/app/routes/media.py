"""Media proxy endpoints — stream clips/snapshots/audio live from the source."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from .. import crops, db, proxy, species_audio, species_info, species_photos
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


@router.get("/frigate/{event_id}/crop.jpg")
async def frigate_crop(event_id: str):
    """The identification service's best crop for this event, when one was stored.

    Served from local disk, not proxied: the crop may show footage from a different
    camera than the event (a zoomed PTZ recording), so Frigate has no equivalent to
    proxy. 404 lets templates fall back to the normal Frigate thumbnail.
    """
    path = await run_in_threadpool(crops.path_if_exists, event_id)
    if not path:
        return JSONResponse({"error": "no stored crop for event"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg")


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


@router.get("/species/{name}/photo/{position}", name="species_reference_photo")
async def species_reference_photo(name: str, position: int, request: Request):
    """Stream one of a species' iNaturalist reference photos.

    Proxied like every other media asset, so the upstream URL stays server-side. Unlike
    ``/species/{name}/image`` (BirdNET-Go's single generic photo) this needs no configured
    upstream, so it works on a Frigate-only install.
    """
    sci = await run_in_threadpool(db.scientific_name_for, name)
    url = await species_photos.file_url(name, position, sci)
    if not url:
        return JSONResponse({"error": "no reference photo"}, status_code=404)
    return await proxy.stream_upstream(
        request, url, headers={"User-Agent": species_info.USER_AGENT}
    )


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


async def _remuxed_clip(url: str, label: str) -> Optional[tuple[str, str]]:
    """Fetch a Frigate MP4 (event clip or recordings window) and remux it into a
    seekable MP4. Returns (path, tmpdir). ``label`` is for log lines only.

    Frigate serves clips as streaming (fragmented) MP4s whose header declares zero
    duration. Two consequences: they fail upload validation elsewhere (Discord sees an
    "empty" video), and a browser can't seek them — it never learns the length, so the
    progress bar just grows as it plays. ``-c copy`` is lossless and fast (no re-encode);
    ``+faststart`` puts the moov atom first so playback and seeking work immediately.

    Returns None if the clip couldn't be fetched. If ffmpeg is missing or fails, returns
    the original bytes instead — degraded (still not seekable) but better than nothing.
    The caller owns ``tmpdir`` and must arrange its removal.
    """
    tmpdir = tempfile.mkdtemp(prefix="aviary-dl-")
    src = os.path.join(tmpdir, "src.mp4")
    out = os.path.join(tmpdir, "out.mp4")

    if not await proxy.fetch_to_file(url, src):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None

    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found; serving the original clip for %s (not seekable)", label)
        return src, tmpdir

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", out,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT_S)
        if proc.returncode == 0 and os.path.getsize(out) > 0:
            return out, tmpdir
        log.warning("ffmpeg remux failed for %s (rc=%s): %s",
                    label, proc.returncode, (stderr or b"")[-300:])
    except asyncio.TimeoutError:
        # Kill it: otherwise it keeps writing into a directory we're about to delete.
        log.warning("ffmpeg remux timed out for %s after %ss", label, _FFMPEG_TIMEOUT_S)
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):  # noqa: BLE001 - reaping is best-effort
                await proc.communicate()
    except OSError as exc:
        log.warning("ffmpeg remux errored for %s: %s", label, exc)
    log.debug("Falling back to the un-remuxed clip for %s; it will not be seekable.", label)
    return src, tmpdir


@router.get("/frigate/{event_id}/play.mp4", name="frigate_play")
async def frigate_play(event_id: str, request: Request):
    """A seekable version of a clip, for the expanded player.

    Same remux as the download route but served inline (no Content-Disposition). The
    player fetches this once into a blob, so all subsequent seeking happens in the
    browser against bytes it already holds — nothing is cached server-side.
    """
    base = request.app.state.settings.frigate_url
    if not base:
        return JSONResponse({"error": "frigate_url not configured"}, status_code=503)
    result = await _remuxed_clip(proxy.frigate_clip_url(base, event_id), event_id)
    if result is None:
        return JSONResponse({"error": "could not fetch clip from Frigate"}, status_code=502)
    path, tmpdir = result
    return FileResponse(
        path, media_type="video/mp4",
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


@router.get("/frigate/{event_id}/download.mp4")
async def frigate_download(event_id: str, request: Request):
    """Download a clip as a standard MP4 with a correct duration header."""
    base = request.app.state.settings.frigate_url
    if not base:
        return JSONResponse({"error": "frigate_url not configured"}, status_code=503)
    result = await _remuxed_clip(proxy.frigate_clip_url(base, event_id), event_id)
    if result is None:
        return JSONResponse({"error": "could not fetch clip from Frigate"}, status_code=502)
    path, tmpdir = result

    # Friendly filename: species + local detection time.
    det = await run_in_threadpool(db.detection_by_ref, "frigate", event_id)
    if det:
        stamp = datetime.fromtimestamp(det.get("start_time") or time.time()).strftime("%Y%m%d-%H%M%S")
        fname = f"{species_slug(det['common_name'])}-{stamp}.mp4"
    else:
        fname = f"aviary-{event_id}.mp4"
    return FileResponse(
        path, media_type="video/mp4", filename=fname,
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


# ------------------------------------------------------------ recordings by window
# Backs the cards' "view on the other camera" button: the same time window as an event,
# but from the paired camera's continuous recordings (Frigate's recordings-by-window
# endpoint works for any recorded camera, event or not).

# A malformed request must not make ffmpeg remux an hour of 4K. Sized to fit a long
# event plus clip_pad_seconds at its maximum (300 each side).
_RECORDING_MAX_S = 900.0
_CAMERA_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


def _recording_window(camera: str, start: float, end: float
                      ) -> Optional[tuple[str, float, float]]:
    """Validate a recordings request. None = reject with 400."""
    camera = (camera or "").strip().lower()
    if not _CAMERA_RE.match(camera):
        return None
    if not (end > start) or (end - start) > _RECORDING_MAX_S:
        return None
    return camera, float(start), float(end)


@router.get("/frigate/recordings/{camera}/clip.mp4", name="frigate_recording_clip")
async def frigate_recording_clip(camera: str, start: float, end: float, request: Request):
    """Straight passthrough of a recordings window — the player's non-seekable fallback."""
    base = request.app.state.settings.frigate_url
    if not base:
        return JSONResponse({"error": "frigate_url not configured"}, status_code=503)
    window = _recording_window(camera, start, end)
    if window is None:
        return JSONResponse({"error": "invalid camera or window"}, status_code=400)
    return await proxy.stream_upstream(
        request, proxy.frigate_recordings_url(base, *window))


@router.get("/frigate/recordings/{camera}/play.mp4", name="frigate_recording_play")
async def frigate_recording_play(camera: str, start: float, end: float, request: Request):
    """A seekable recordings window, same remux as an event's play.mp4."""
    base = request.app.state.settings.frigate_url
    if not base:
        return JSONResponse({"error": "frigate_url not configured"}, status_code=503)
    window = _recording_window(camera, start, end)
    if window is None:
        return JSONResponse({"error": "invalid camera or window"}, status_code=400)
    cam, w_start, w_end = window
    result = await _remuxed_clip(
        proxy.frigate_recordings_url(base, cam, w_start, w_end),
        f"{cam}@{w_start:.0f}-{w_end:.0f}",
    )
    if result is None:
        return JSONResponse(
            {"error": "no recordings from Frigate for that window"}, status_code=502)
    path, tmpdir = result
    return FileResponse(
        path, media_type="video/mp4",
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


@router.get("/frigate/export/{det_id}/video.mp4", name="frigate_kept_export")
async def frigate_kept_export(det_id: int, request: Request):
    """The kept zoomed footage: the Frigate export created when this event was pinned.

    Exports are asynchronous at Frigate and their filename is only known once finished,
    so it is resolved lazily from the exports list on first play and cached on the row.
    Frigate's exports are already normal seekable MP4s — streamed as-is, no remux.
    """
    base = request.app.state.settings.frigate_url
    if not base:
        return JSONResponse({"error": "frigate_url not configured"}, status_code=503)
    det = await run_in_threadpool(db.detection_by_id, det_id)
    if not det or not det.get("kept_export_id"):
        return JSONResponse({"error": "no kept export for detection"}, status_code=404)

    filename = det.get("kept_export_file")
    if not filename:
        try:
            status, data = await proxy.get_json(proxy.frigate_exports_url(base))
        except Exception:  # httpx transport errors — Frigate down
            return JSONResponse({"error": "Frigate unreachable"}, status_code=502)
        if status >= 400 or not isinstance(data, list):
            return JSONResponse({"error": f"exports list failed ({status})"}, status_code=502)
        record = next(
            (e for e in data if str(e.get("id")) == str(det["kept_export_id"])), None)
        if record is None:
            # Deleted at Frigate (their Export UI, or a failed job): clear the stored id
            # so the card stops offering a button that can never play.
            await run_in_threadpool(db.set_kept_export, det_id, None, None)
            return JSONResponse(
                {"error": "export no longer exists at Frigate"}, status_code=404)
        if record.get("in_progress"):
            return JSONResponse(
                {"error": "export still processing — try again shortly"}, status_code=425)
        filename = os.path.basename(record.get("video_path") or "")
        if not filename:
            return JSONResponse({"error": "export has no video file"}, status_code=502)
        await run_in_threadpool(db.set_kept_export_file, det_id, filename)

    return await proxy.stream_upstream(
        request, proxy.frigate_export_video_url(base, filename))


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
