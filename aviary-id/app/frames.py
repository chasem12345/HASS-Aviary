"""Fetch a Frigate event's media and turn it into candidate images for classification.

Frigate stores one "best" snapshot per event plus the clip. The snapshot is Frigate's own
highest-scoring frame, so it is always worth including — but a single frame of a moving
bird is a coin flip on pose and occlusion, which is exactly where the built-in classifier
struggles. So we also sample across the clip and let the detector decide which frames
actually contain a usable bird.

Note that Frigate's ``crop``/``bbox`` query params only apply while an event is still in
progress; a finished event returns the stored clean full-frame snapshot. Everything here
therefore assumes full frames and does its own cropping.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional

import httpx
from PIL import Image

from .settings import Settings

log = logging.getLogger("aviary_id.frames")

# Frigate clips are usually 1080p or the detect-stream resolution. Cap the long edge so a
# 4K substream can't blow up memory, but stay high enough that a small distant bird still
# has pixels left after cropping.
_MAX_WIDTH = 1920


@dataclass
class Candidate:
    """One full frame pulled from the event, before localization."""
    image: Image.Image
    # Where it came from, for the per-frame debug output: "snapshot" or "clip@1.75s".
    origin: str


def clip_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/clip.mp4"


def snapshot_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/snapshot.jpg"


def event_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}"


async def _fetch(client: httpx.AsyncClient, url: str, headers: dict[str, str],
                 dest: Optional[str] = None) -> Optional[bytes]:
    """GET a URL, optionally streaming to a file. Returns bytes, b"" for a file, or None."""
    try:
        if dest is None:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                log.debug("GET %s -> %s", url, resp.status_code)
                return None
            return resp.content
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                log.debug("GET %s -> %s", url, resp.status_code)
                return None
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
        return b""
    except (httpx.HTTPError, OSError) as exc:
        log.warning("Fetch failed for %s: %s", url, exc)
        return None


async def _run(cmd: list[str], timeout: float) -> tuple[int, bytes]:
    """Run a subprocess, returning (returncode, stdout). Kills it on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("Command timed out after %ss: %s", timeout, cmd[0])
        proc.kill()
        await proc.wait()
        return 1, b""
    return proc.returncode or 0, stdout or b""


async def _probe_duration(path: str, timeout: float) -> Optional[float]:
    code, out = await _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
    ], timeout)
    if code != 0:
        return None
    try:
        duration = float(out.decode().strip())
    except (ValueError, UnicodeDecodeError):
        return None
    return duration if duration > 0 else None


async def _extract_frames(clip_path: str, outdir: str, settings: Settings) -> list[tuple[str, float]]:
    """Decode evenly-spaced JPEGs from the clip. Returns [(path, timestamp_seconds)].

    A single ffmpeg pass with an ``fps`` filter rather than N seek-and-grab invocations:
    one process, and it degrades sensibly on clips whose duration can't be probed.
    """
    duration = await _probe_duration(clip_path, settings.ffmpeg_timeout)
    if duration:
        # Slight over-request; ffmpeg's fps filter rounds and we would rather have one
        # extra frame than one fewer on a short clip.
        rate = max(settings.sample_frames / duration, 0.1)
    else:
        log.debug("Could not probe clip duration; falling back to a fixed sample rate.")
        rate = 2.0

    # Hard cap the output regardless of what the rate maths produced — a mis-probed
    # duration on a long clip must not fill the tmpdir.
    cap = settings.sample_frames * 2
    pattern = os.path.join(outdir, "f_%03d.jpg")
    code, _ = await _run([
        "ffmpeg", "-nostdin", "-y",
        "-i", clip_path,
        "-vf", f"fps={rate:.6f},scale='min({_MAX_WIDTH},iw)':-2",
        "-frames:v", str(cap),
        "-q:v", "2",
        pattern,
    ], settings.ffmpeg_timeout)
    if code != 0:
        log.warning("ffmpeg frame extraction failed for %s", clip_path)
        return []

    files = sorted(f for f in os.listdir(outdir) if f.startswith("f_"))
    step = 1.0 / rate
    return [(os.path.join(outdir, name), i * step) for i, name in enumerate(files)]


def _pick_evenly(items: list, count: int) -> list:
    """Evenly-spaced subsample, always keeping the first and last."""
    if len(items) <= count:
        return items
    stride = (len(items) - 1) / (count - 1)
    return [items[round(i * stride)] for i in range(count)]


async def gather_candidates(
    client: httpx.AsyncClient,
    event_id: str,
    frigate_url: str,
    settings: Settings,
) -> list[Candidate]:
    """Collect full frames for an event: Frigate's snapshot plus samples from the clip.

    Never raises for a missing clip or snapshot — an event may legitimately have only one
    of the two (Frigate's ``snapshots`` and ``record`` settings are independent). Returns
    an empty list only when neither is available.
    """
    headers = settings.frigate_headers
    candidates: list[Candidate] = []

    snapshot_bytes = await _fetch(client, snapshot_url(frigate_url, event_id), headers)
    if snapshot_bytes:
        try:
            img = Image.open(io.BytesIO(snapshot_bytes))
            img.load()
            candidates.append(Candidate(image=img.convert("RGB"), origin="snapshot"))
        except (OSError, ValueError) as exc:
            log.warning("Could not decode snapshot for %s: %s", event_id, exc)

    tmpdir = tempfile.mkdtemp(prefix="aviary-id-")
    try:
        clip_path = os.path.join(tmpdir, "clip.mp4")
        got = await _fetch(client, clip_url(frigate_url, event_id), headers, dest=clip_path)
        if got is not None and os.path.getsize(clip_path) > 0:
            framedir = os.path.join(tmpdir, "frames")
            os.makedirs(framedir, exist_ok=True)
            frames = await _extract_frames(clip_path, framedir, settings)
            for path, ts in _pick_evenly(frames, settings.sample_frames):
                try:
                    img = Image.open(path)
                    img.load()  # decode before the tmpdir disappears
                    candidates.append(
                        Candidate(image=img.convert("RGB"), origin=f"clip@{ts:.2f}s")
                    )
                except (OSError, ValueError) as exc:
                    log.debug("Could not decode frame %s: %s", path, exc)
        else:
            log.debug("No clip available for event %s.", event_id)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    log.debug("Event %s: %d candidate frames.", event_id, len(candidates))
    return candidates


def crop_box(image: Image.Image, box: tuple[float, float, float, float],
             padding: float) -> Image.Image:
    """Crop ``box`` (x1, y1, x2, y2 in pixels) with proportional padding, clamped."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    pad_x, pad_y = w * padding, h * padding
    left = max(0, int(x1 - pad_x))
    top = max(0, int(y1 - pad_y))
    right = min(image.width, int(x2 + pad_x))
    bottom = min(image.height, int(y2 + pad_y))
    # A degenerate box (detector returning a zero-area region) would make PIL raise;
    # fall back to the whole frame, which the classifier can still do something with.
    if right <= left or bottom <= top:
        return image
    return image.crop((left, top, right, bottom))
