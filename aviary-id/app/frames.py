"""Fetch a Frigate event's media and turn it into candidate crops for classification.

The guiding principle here is that **Frigate already found the bird**. Locating objects is
its entire job, and it hands us the answer twice over: `thumbnail.jpg` is cropped to the
object once an event has ended, and the event API reports the snapshot's bounding box in
pixels. Earlier versions ignored both, downloaded the full snapshot, and re-derived the box
with a COCO detector that is worse at this than Frigate is — then classified the whole
uncropped frame when that failed, which on a 1080p frame leaves a feeder-distance bird about
ten pixels across.

So: take Frigate's crops when it has them, and use our own detector only for clip frames,
where Frigate cannot give us a box for an arbitrary timestamp.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import httpx
from PIL import Image

from .settings import Settings

log = logging.getLogger("aviary_id.frames")

# Frigate clips are usually 1080p or the detect-stream resolution. Cap the long edge so a
# 4K substream can't blow up memory, but stay high enough that a small distant bird still
# has pixels left after cropping.
_MAX_WIDTH = 1920

# Concurrent ffmpeg seeks. Each is cheap, but a feeder in full swing shouldn't be able to
# fork a dozen decoders at once on a box that is also running a GPU workload.
_SEEK_CONCURRENCY = 4

# How far (seconds) a clip frame may sit outside the tracked path's time span and still
# borrow its nearest endpoint as an anchor. Paths are sparse (a few points across a
# whole event), so this is deliberately generous — but a frame in the pre/post-capture
# padding, long before the bird arrived, must not inherit an anchor it has no claim to.
_ANCHOR_TOLERANCE = 2.0


@dataclass
class Candidate:
    """One image pulled from the event."""
    image: Image.Image
    # Where it came from, for the per-frame debug output: "thumbnail", "snapshot",
    # "snapshot+box", or "clip@1.75s".
    origin: str
    # True when the image is already a crop of the bird (Frigate's thumbnail, or the
    # snapshot cropped to Frigate's own box). Such candidates skip the detector entirely —
    # running a COCO model over an existing tight crop mostly finds nothing.
    pre_cropped: bool = False
    # Detector-equivalent confidence for a pre-cropped candidate, used when fusing frames.
    # Frigate's own score where we have it.
    score: float = 0.9
    # Where the TRACKED bird sits in this frame, normalized (x, y), interpolated from the
    # event's path_data. Clip frames only. This is Frigate's own answer to "which bird is
    # this event about" — with two birds in frame, the detector finds both, and without
    # the anchor the pipeline would happily classify whichever is more photogenic.
    anchor: Optional[tuple[float, float]] = None


@dataclass
class Timings:
    """Per-stage wall-clock, so a slow event says which stage was slow."""
    stages: dict = field(default_factory=dict)

    def add(self, name: str, seconds: float) -> None:
        self.stages[name] = round(seconds * 1000)

    def summary(self) -> str:
        return " ".join(f"{k}={v}ms" for k, v in self.stages.items())


def clip_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/clip.mp4"


def snapshot_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/snapshot.jpg"


def thumbnail_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/thumbnail.jpg"


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


def _decode(data: bytes, what: str) -> Optional[Image.Image]:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except (OSError, ValueError) as exc:
        log.warning("Could not decode %s: %s", what, exc)
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


async def probe_duration(path: str, timeout: float) -> Optional[float]:
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


async def _grab_frame(clip_path: str, outdir: str, offset: float, index: int,
                      settings: Settings, sem: asyncio.Semaphore) -> Optional[tuple[str, float]]:
    """Extract exactly one frame at ``offset`` seconds.

    ``-ss`` goes BEFORE ``-i`` on purpose. That makes ffmpeg seek to the nearest keyframe
    and decode from there, instead of decoding the clip from the start. The previous
    approach used a single `-vf fps=N/duration` pass, which forces a full decode of the
    entire clip to emit its handful of frames — measured at 34 seconds on one event.
    """
    out = os.path.join(outdir, f"f_{index:03d}.jpg")
    async with sem:
        code, _ = await _run([
            "ffmpeg", "-nostdin", "-y",
            "-ss", f"{offset:.3f}",
            "-i", clip_path,
            "-frames:v", "1",
            "-vf", f"scale='min({_MAX_WIDTH},iw)':-2",
            "-q:v", "2",
            out,
        ], settings.ffmpeg_timeout)
    if code != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        return None
    return out, offset


def sample_offsets(duration: float, count: int, phase: float = 0.5) -> list[float]:
    """Evenly spaced timestamps inside the clip.

    ``phase`` positions them within each slice: 0.5 centres them, and a second pass at 0.0
    or 1.0 lands between the first pass's frames rather than next to them — which is what
    makes escalation produce genuinely new views instead of near-duplicates.
    """
    if count <= 0 or duration <= 0:
        return []
    step = duration / count
    return [min(duration - 0.05, max(0.0, (i + phase) * step)) for i in range(count)]


async def extract_frames(clip_path: str, outdir: str, offsets: list[float],
                         settings: Settings) -> list[tuple[Image.Image, float]]:
    """Decode the given timestamps concurrently. Returns [(image, offset)]."""
    if not offsets:
        return []
    sem = asyncio.Semaphore(_SEEK_CONCURRENCY)
    results = await asyncio.gather(*[
        _grab_frame(clip_path, outdir, off, i, settings, sem)
        for i, off in enumerate(offsets)
    ])
    frames: list[tuple[Image.Image, float]] = []
    for item in results:
        if item is None:
            continue
        path, offset = item
        img = _decode_file(path)
        if img is not None:
            frames.append((img, offset))
    return frames


def _decode_file(path: str) -> Optional[Image.Image]:
    try:
        img = Image.open(path)
        img.load()  # decode before the tmpdir disappears
        return img.convert("RGB")
    except (OSError, ValueError) as exc:
        log.debug("Could not decode frame %s: %s", path, exc)
        return None


def _path_from_event(event: dict) -> list[tuple[float, float, float]]:
    """The tracked object's path as [(wall_time, x, y)], normalized, time-sorted.

    Frigate stores it as ``data.path_data = [[[x, y], timestamp], ...]`` — sparse
    normalized points (bottom-center of the tracked box) with absolute wall-clock
    timestamps. Anything malformed is skipped rather than guessed at.
    """
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    raw = data.get("path_data")
    if not isinstance(raw, list):
        return []
    points: list[tuple[float, float, float]] = []
    for entry in raw:
        try:
            (x, y), t = entry
            x, y, t = float(x), float(y), float(t)
        except (TypeError, ValueError):
            continue
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            points.append((t, x, y))
    points.sort()
    return points


def _as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _box_from_event(event: dict) -> Optional[tuple[float, float, float, float]]:
    """Frigate's own bounding box for the snapshot frame, in absolute pixels.

    Frigate reports this in a couple of shapes depending on version: a top-level
    ``snapshot.box``, or ``data.box``. Some builds normalise ``data.box`` to 0-1, which is
    detected and rejected here rather than guessed at — a normalised box applied as pixels
    would crop the top-left corner of the frame and quietly ruin every identification.
    """
    for path in (("snapshot", "box"), ("data", "box")):
        node = event
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if not (isinstance(node, (list, tuple)) and len(node) == 4):
            continue
        try:
            box = tuple(float(v) for v in node)
        except (TypeError, ValueError):
            continue
        if max(box) <= 1.0:
            log.debug("Ignoring a normalised box from %s; expected pixels.", "/".join(path))
            continue
        x1, y1, x2, y2 = box
        if x2 > x1 and y2 > y1:
            return (x1, y1, x2, y2)
    return None


class EventMedia:
    """Everything fetched for one event, so escalation can reuse it without re-downloading."""

    def __init__(self, event_id: str, base: str, settings: Settings):
        self.event_id = event_id
        self.base = base
        self.settings = settings
        self.candidates: list[Candidate] = []
        self.clip_path: Optional[str] = None
        self.duration: Optional[float] = None
        # Tracked-object path and event times, for anchoring clip frames to the bird
        # this event is actually about (see Candidate.anchor).
        self.path: list[tuple[float, float, float]] = []
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._tmpdir: Optional[str] = None
        self._used_offsets: set[int] = set()

    async def close(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    async def gather(self, client: httpx.AsyncClient, timings: Timings) -> None:
        """Fetch Frigate's own crops plus the first pass of clip frames."""
        headers = self.settings.frigate_headers
        loop = asyncio.get_running_loop()
        started = loop.time()

        # Frigate's crops first — these are the ones that do not depend on our detector.
        thumb_bytes, snap_bytes, event_json = await asyncio.gather(
            _fetch(client, thumbnail_url(self.base, self.event_id), headers),
            _fetch(client, snapshot_url(self.base, self.event_id), headers),
            _fetch(client, event_url(self.base, self.event_id), headers),
        )
        timings.add("fetch", loop.time() - started)

        if thumb_bytes and self.settings.use_thumbnail:
            img = _decode(thumb_bytes, f"thumbnail for {self.event_id}")
            # On an ended event this is Frigate's crop of the object. While an event is
            # still in progress it is the full frame instead — size is the tell, and a
            # thumbnail as large as the snapshot is not a crop.
            if img is not None:
                self.candidates.append(Candidate(image=img, origin="thumbnail",
                                                 pre_cropped=True))

        event: dict = {}
        if event_json:
            try:
                parsed = json.loads(event_json)
                if isinstance(parsed, dict):
                    event = parsed
            except (ValueError, TypeError):
                log.debug("Could not parse the event JSON for %s.", self.event_id)
        self.path = _path_from_event(event)
        self.start_time = _as_float(event.get("start_time"))
        self.end_time = _as_float(event.get("end_time"))

        snapshot = _decode(snap_bytes, f"snapshot for {self.event_id}") if snap_bytes else None
        if snapshot is not None:
            box = _box_from_event(event) if (event and self.settings.use_event_box) else None
            if box:
                self.candidates.append(Candidate(
                    image=crop_box(snapshot, box, self.settings.crop_padding),
                    origin="snapshot+box", pre_cropped=True,
                ))
            else:
                # No box from Frigate: keep the full snapshot and let the detector try.
                self.candidates.append(Candidate(image=snapshot, origin="snapshot"))

        await self._fetch_clip(client, timings)

    async def _fetch_clip(self, client: httpx.AsyncClient, timings: Timings) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        self._tmpdir = tempfile.mkdtemp(prefix="aviary-id-")
        clip_path = os.path.join(self._tmpdir, "clip.mp4")
        got = await _fetch(client, clip_url(self.base, self.event_id),
                           self.settings.frigate_headers, dest=clip_path)
        if got is None or not os.path.exists(clip_path) or os.path.getsize(clip_path) == 0:
            log.debug("No clip available for event %s.", self.event_id)
            return
        self.clip_path = clip_path
        timings.add("clip", loop.time() - started)

        started = loop.time()
        self.duration = await probe_duration(clip_path, self.settings.ffmpeg_timeout)
        if not self.duration:
            log.debug("Could not probe clip duration for %s; skipping clip frames.",
                      self.event_id)
            return
        await self.add_clip_frames(self.settings.sample_frames, phase=0.5, timings=timings)

    async def add_clip_frames(self, count: int, phase: float, timings: Timings) -> int:
        """Extract another pass of clip frames. Returns how many were added.

        Escalation calls this a second time with a different ``phase`` so the new frames
        fall between the ones already seen.
        """
        if not (self.clip_path and self.duration):
            return 0
        loop = asyncio.get_running_loop()
        started = loop.time()
        offsets = [
            off for off in sample_offsets(self.duration, count, phase)
            # Guard against a second pass landing on a frame we already have.
            if int(off * 10) not in self._used_offsets
        ]
        if not offsets:
            return 0
        frames = await extract_frames(self.clip_path, self._tmpdir, offsets, self.settings)
        clip_t0 = self._clip_start()
        for img, offset in frames:
            self._used_offsets.add(int(offset * 10))
            anchor = (self._anchor_at(clip_t0 + offset)
                      if clip_t0 is not None else None)
            self.candidates.append(
                Candidate(image=img, origin=f"clip@{offset:.2f}s", anchor=anchor))
        timings.add(f"ffmpeg{'' if phase == 0.5 else '2'}", loop.time() - started)
        return len(frames)

    def _clip_start(self) -> Optional[float]:
        """Estimated wall-clock time of the clip's first frame.

        The clip is the event plus Frigate's pre/post-capture padding, whose exact split
        Frigate does not report — assume symmetric. Anchor matching is tolerance-based,
        so being a second off is survivable; having no estimate at all is not.
        """
        if self.start_time is None or not self.duration:
            return None
        event_span = max(0.0, (self.end_time or self.start_time) - self.start_time)
        return self.start_time - max(0.0, (self.duration - event_span) / 2)

    def _anchor_at(self, wall: float) -> Optional[tuple[float, float]]:
        """The tracked bird's normalized position at ``wall`` time, or None.

        Linear interpolation between the sparse path points; outside the path's span the
        nearest endpoint serves, but only within _ANCHOR_TOLERANCE — a frame from the
        padding before the bird arrived gets no anchor rather than a fabricated one.
        """
        if not self.path:
            return None
        first, last = self.path[0], self.path[-1]
        if wall <= first[0]:
            return (first[1], first[2]) if first[0] - wall <= _ANCHOR_TOLERANCE else None
        if wall >= last[0]:
            return (last[1], last[2]) if wall - last[0] <= _ANCHOR_TOLERANCE else None
        for (t0, x0, y0), (t1, x1, y1) in zip(self.path, self.path[1:]):
            if t0 <= wall <= t1:
                span = t1 - t0
                f = (wall - t0) / span if span > 0 else 0.0
                return (x0 + f * (x1 - x0), y0 + f * (y1 - y0))
        return None


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
