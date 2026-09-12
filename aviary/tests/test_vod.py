"""HLS playback of recordings windows: the URL helper and the validators the media
routes gate on. Pure — no Frigate, no network."""

from __future__ import annotations

import pytest

from app import kept, proxy


def test_vod_url_mirrors_frigates_path_shape():
    url = proxy.frigate_vod_url("http://f:5000", "birdzone", 1757600000.0, 1757600030.5, "master.m3u8")
    assert url == "http://f:5000/vod/birdzone/start/1757600000.000/end/1757600030.500/master.m3u8"


@pytest.mark.parametrize("name", [
    "master.m3u8", "index-v1-a1.m3u8", "index.m3u8", "init-v1-a1.mp4",
    "seg-12-v1-a1.m4s", "seg-1-v1.ts",
])
def test_vod_filenames_nginx_vod_emits_are_accepted(name):
    assert kept.vod_filename_ok(name)


@pytest.mark.parametrize("name", [
    "clip.mp4", "MASTER.M3U8", "seg-1.exe", "../master.m3u8", "master.m3u8?x", "",
    "init.mp4x", "seg-v1-a1.m4s", "master.m3u8/", "master.m3u8 ",
])
def test_anything_else_is_rejected(name):
    assert not kept.vod_filename_ok(name)


@pytest.mark.parametrize("max_s", [kept.RECORDING_MAX_S, kept.PLAYBACK_MAX_S])
def test_recording_window_honours_its_cap(max_s):
    assert kept.recording_window("cam", 0.0, max_s, max_s) == ("cam", 0.0, max_s)
    assert kept.recording_window("cam", 0.0, max_s + 0.5, max_s) is None


def test_recording_window_validates_camera_and_order():
    assert kept.recording_window("BirdZone", 10.0, 20.0, 900) == ("birdzone", 10.0, 20.0)
    assert kept.recording_window("Bird Zone", 10.0, 20.0, 900) is None
    assert kept.recording_window("../x", 10.0, 20.0, 900) is None
    assert kept.recording_window("", 10.0, 20.0, 900) is None
    assert kept.recording_window("cam", 20.0, 20.0, 900) is None
    assert kept.recording_window("cam", 20.0, 10.0, 900) is None
    assert kept.recording_window("cam", "nan?", 10.0, 900) is None


def test_playback_cap_is_the_looser_one():
    # Streaming assembles nothing up front, so it may exceed what exports pull whole.
    assert kept.PLAYBACK_MAX_S > kept.RECORDING_MAX_S


def test_media_routes_validate_before_proxying():
    """The route rejects bad input before touching Frigate: 400s with no client at all."""
    from types import SimpleNamespace
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from app.routes import media

    app = FastAPI()
    app.include_router(media.router, prefix="/media")
    app.state.settings = SimpleNamespace(frigate_url="http://frigate.invalid:5000")
    c = TestClient(app)
    base = "/media/frigate/vod/birdzone/start/100.0/end/200.0/"
    assert c.get(base + "clip.mp4").status_code == 400
    assert c.get("/media/frigate/vod/birdzone/start/100.0/end/9000.0/master.m3u8").status_code == 400
    # Valid request, no proxy client initialised in this bare app: the guard fires, not Frigate.
    r = c.get(base + "master.m3u8")
    assert r.status_code == 500 and "not initialized" in r.text
    r = c.get("/media/frigate/recordings/birdzone/clip.mp4?start=100&end=9000")
    assert r.status_code == 400
