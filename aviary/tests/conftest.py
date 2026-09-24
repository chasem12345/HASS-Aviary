"""Shared fixtures: a temp database with identification configured, and toy embeddings."""

from __future__ import annotations

import base64

import numpy as np
import pytest

from app import crops, db, identify, ingest, probe
from app.settings import Settings

T0 = 1_788_904_615.0
MODEL = "hf-hub:imageomics/bioclip-2"
DIM = 64


def unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def encode(v: np.ndarray) -> str:
    return base64.b64encode((v / np.linalg.norm(v)).astype(np.float16).tobytes()).decode("ascii")


def emb(seed: int, base: np.ndarray | None = None, noise: float = 0.0) -> str:
    """A random unit vector; or ``base`` nudged by ``noise`` (0.05 -> cosine ~0.93)."""
    v = unit(seed)
    if base is not None:
        v = base + noise * v
    return encode(v)


@pytest.fixture(autouse=True)
def _fresh_sighting_debounce():
    """The unlinked-fragment debounce is module state keyed by camera + species; tests
    reuse both (and T0), so one test's announcement must not silence the next's."""
    ingest._recent_sightings.clear()  # noqa: SLF001
    yield
    ingest._recent_sightings.clear()  # noqa: SLF001


def make_settings(tmp_path, **override) -> Settings:
    kw = dict(
        data_dir=str(tmp_path), db_path=str(tmp_path / "aviary.db"), mqtt_host="", mqtt_port=1883,
        mqtt_user="", mqtt_password="", frigate_url="http://frigate", birdnet_url="",
        frigate_topic="frigate/events", birdnet_topic="birdnet", frigate_review_topic="frigate/reviews",
        backfill_on_start=False, ignore_unclassified=False, ignore_cameras=(),
        require_species_confirmation=True, notify_new_species=False, xeno_canto_api_key="",
        identify_url="http://id", identify_token="", identify_enabled=True,
        identify_min_score=0.35, identify_min_margin=0.08, identify_workers=1, identify_timeout=10,
        identify_retain_days=14, identify_use_audio_priors=False, identify_exclude_blacklisted=True,
        identify_zoom_map={}, identify_zoom_start_offset=0.0, identify_zoom_zone_priority=(),
        ha_config_dir=str(tmp_path), clip_pad_seconds=10.0, log_level="debug",
    )
    kw.update(override)
    return Settings(**kw)


def frigate_row(ref: str, start: float = T0, camera: str = "birdzone") -> dict:
    return {
        "source": "frigate", "source_ref": ref, "common_name": "bird", "scientific_name": None,
        "species_code": None, "confidence": 0.8, "location": camera, "zone": "bath",
        "start_time": start, "end_time": start + 5, "has_clip": 1, "has_snapshot": 1,
        "clip_ref": ref, "snapshot_ref": ref, "native_id": ref, "raw_json": None,
        "created_at": start,
    }


@pytest.fixture()
def env(tmp_path):
    db.init_db(str(tmp_path / "aviary.db"))
    crops.configure(str(tmp_path))
    ingest.configure(ignore_unclassified=False, ignore_cameras=())
    identify.configure(make_settings(tmp_path))
    probe.configure(False)
    # An empty probe abstains from every blend, so the service's numbers stand.
    probe.rebuild(MODEL)
    row = frigate_row("ev1")
    db.upsert_detection(row)
    row["id"] = db.detection_by_ref("frigate", "ev1")["id"]
    return row
