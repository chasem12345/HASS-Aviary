"""Cleaned BirdNET-Go playback (0.38.0) and the static-asset cache headers. No ffmpeg,
no network: the filter string, the route's fallbacks and the header plumbing only."""

from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi import FastAPI
from starlette.testclient import TestClient

from app.routes import media


def _settings(**kw):
    base = dict(birdnet_url="http://birdnet.invalid", birdnet_clean_audio=True,
                birdnet_clean_filter="highpass=f=200,afftdn=nr=20")
    base.update(kw)
    return SimpleNamespace(**base)


def test_presets_resolve_to_their_chains(monkeypatch, tmp_path):
    from app import settings as s

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BIRDNET_CLEAN_FILTER", " lowpass=f=8000 ")
    for preset in ("smooth", "balanced", "strong"):
        monkeypatch.setenv("BIRDNET_CLEAN_PRESET", preset)
        got = s.load_settings()
        assert got.birdnet_clean_audio and got.birdnet_clean_filter == s.CLEAN_PRESETS[preset]
    monkeypatch.setenv("BIRDNET_CLEAN_PRESET", "custom")
    assert s.load_settings().birdnet_clean_filter == "lowpass=f=8000"
    monkeypatch.setenv("BIRDNET_CLEAN_PRESET", "off")
    assert s.load_settings().birdnet_clean_audio is False


def test_custom_with_a_blank_box_plays_smooth_and_junk_reads_as_default():
    from app import settings as s

    assert s._clean_audio("custom", "  ") == ("custom", True, s.CLEAN_PRESETS["smooth"])
    assert s._clean_audio("bogus", "x")[2] == s.DEFAULT_CLEAN_FILTER
    assert s.DEFAULT_CLEAN_PRESET == "smooth"


def test_clean_token_changes_with_the_chain():
    from app.routes import _clean_token

    assert _clean_token("a") != _clean_token("b")
    assert _clean_token("a") == _clean_token("a")


def _client(monkeypatch, settings, cleaned):
    app = FastAPI()
    app.include_router(media.router, prefix="/media")
    app.state.settings = settings
    monkeypatch.setattr(media.db, "detection_by_id",
                        lambda i: {"id": i, "native_id": "7", "source_ref": "owl.wav"})
    calls = []

    async def fake(urls, afilter, label):
        calls.append(afilter)
        return cleaned

    monkeypatch.setattr(media, "_cleaned_audio", fake)
    return TestClient(app), calls


def test_clean_clip_is_served_as_cacheable_wav(monkeypatch, tmp_path):
    wav = tmp_path / "clean.wav"
    wav.write_bytes(b"RIFF")
    c, calls = _client(monkeypatch, _settings(), (str(wav), str(tmp_path)))
    r = c.get("/media/birdnet/1/clip?c=abc")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert "max-age" in r.headers["cache-control"]
    assert calls == ["highpass=f=200,afftdn=nr=20"]  # the configured chain, verbatim


def test_raw_and_disabled_skip_cleaning(monkeypatch):
    c, calls = _client(monkeypatch, _settings(), None)
    c.get("/media/birdnet/1/clip?raw=1")
    assert not calls
    c, calls = _client(monkeypatch, _settings(birdnet_clean_audio=False), None)
    c.get("/media/birdnet/1/clip")
    assert not calls


def test_failed_clean_falls_through_to_the_original_stream(monkeypatch):
    c, calls = _client(monkeypatch, _settings(), None)
    r = c.get("/media/birdnet/1/clip")
    # No proxy client in this bare app: reaching stream_upstream's guard proves the
    # fallthrough to the original clip.
    assert calls and r.status_code == 500 and "not initialized" in r.text


def test_static_files_are_immutable(tmp_path):
    from app.main import ImmutableStaticFiles

    (tmp_path / "a.js").write_text("x")
    app = FastAPI()
    app.mount("/static-v", ImmutableStaticFiles(directory=str(tmp_path)), name="static")
    r = TestClient(app).get("/static-v/a.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert TestClient(app).get("/static-v/missing.js").headers.get("cache-control") is None
