"""Confirm mode gathers Frigate's crops only: no clip, no ffmpeg, no boxless snapshot.

``frames._fetch`` is replaced with an in-memory Frigate; ``_fetch_clip`` is armed to fail
so any attempt to download footage is a test failure, not a network call.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from PIL import Image

from app import frames
from app.settings import Settings


def jpeg(size=(120, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(buf, format="JPEG")
    return buf.getvalue()


BASE = "http://frigate"
EID = "1700000000.1-abcdef"


def fake_frigate(monkeypatch, *, thumbnail=True, snapshot=True, box=True):
    urls = {}
    if thumbnail:
        urls[frames.thumbnail_url(BASE, EID)] = jpeg((96, 96))
    if snapshot:
        urls[frames.snapshot_url(BASE, EID)] = jpeg((640, 360))
    event = {"id": EID, "start_time": 1700000000.0, "end_time": 1700000006.0}
    if box:
        event["snapshot"] = {"box": [100, 50, 300, 250]}
    urls[frames.event_url(BASE, EID)] = json.dumps(event).encode()
    fetched: list[str] = []

    async def _fetch(client, url, headers, dest=None):
        fetched.append(url)
        return urls.get(url)

    async def _no_clip(self, client, timings):
        raise AssertionError("confirm mode must never fetch the clip")

    monkeypatch.setattr(frames, "_fetch", _fetch)
    monkeypatch.setattr(frames.EventMedia, "_fetch_clip", _no_clip)
    return fetched


def gather(confirm=True, **settings_kw) -> tuple[frames.EventMedia, frames.Timings]:
    media = frames.EventMedia(EID, BASE, Settings(frigate_url=BASE, **settings_kw),
                              confirm=confirm)
    timings = frames.Timings()
    asyncio.run(media.gather(None, timings))
    return media, timings


def test_confirm_keeps_exactly_frigates_crops(monkeypatch):
    fetched = fake_frigate(monkeypatch)
    media, timings = gather()
    assert [c.origin for c in media.candidates] == ["thumbnail", "snapshot+box"]
    assert all(c.pre_cropped for c in media.candidates)
    # The snapshot crop is Frigate's box plus padding, never the whole frame.
    crop = media.candidates[1].image
    assert crop.width < 640 and crop.height < 360
    assert media.clip_path is None and media._tmpdir is None  # noqa: SLF001
    assert frames.clip_url(BASE, EID) not in fetched
    assert timings.stages.get("clip") == 0


def test_confirm_drops_a_boxless_snapshot(monkeypatch):
    fake_frigate(monkeypatch, box=False)
    media, _ = gather()
    # Without Frigate's box the full frame would need the detector to find a bird — and
    # with two birds in view that need not be the one Frigate named. Out it goes.
    assert [c.origin for c in media.candidates] == ["thumbnail"]


def test_confirm_with_nothing_from_frigate_is_empty(monkeypatch):
    fake_frigate(monkeypatch, thumbnail=False, snapshot=False)
    media, _ = gather()
    assert media.candidates == []


def test_full_mode_still_keeps_the_boxless_snapshot(monkeypatch):
    fake_frigate(monkeypatch, box=False)

    async def _no_clip(self, client, timings):
        return None  # full mode may fetch a clip; here there is none

    monkeypatch.setattr(frames.EventMedia, "_fetch_clip", _no_clip)
    media, _ = gather(confirm=False)
    assert [c.origin for c in media.candidates] == ["thumbnail", "snapshot"]
    assert not media.candidates[1].pre_cropped


@pytest.mark.parametrize("confirm", [True, False])
def test_add_clip_frames_is_inert_without_a_clip(monkeypatch, confirm):
    fake_frigate(monkeypatch)
    if not confirm:
        async def _no_clip(self, client, timings):
            return None
        monkeypatch.setattr(frames.EventMedia, "_fetch_clip", _no_clip)
    media, timings = gather(confirm=confirm)
    # The pipeline's escalation calls these later; with no clip they must do nothing.
    assert asyncio.run(media.add_clip_frames(4, 0.0, timings)) == 0
    assert asyncio.run(media.swap_to_event_clip(timings)) is False
