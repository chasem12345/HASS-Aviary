"""Life list on iNaturalist: payload building, sighting choice, idempotency and failure
handling — with every HTTP call replaced, so no network and no account."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from app import crops, db, inat, ingest, solar, species_info

from tests.test_keepsakes import make_settings  # noqa: E402 - shared fixture helper

CAM = "birdzone"
CARD = "Northern Cardinal"
WREN = "Carolina Wren"
T0 = 1_788_904_615.0
LOC = solar.Location(41.0, -87.0, 0.0, "America/Chicago")


def event(ref, start, label=CARD, end=None):
    after = {
        "id": ref, "camera": CAM, "label": "bird", "sub_label": label, "top_score": 0.87,
        "start_time": start, "end_time": end if end is not None else start + 5,
        "has_clip": True, "has_snapshot": True, "entered_zones": ["bath"],
    }
    ingest.handle_frigate(json.dumps({"type": "end", "before": after, "after": after}).encode())


def birdnet(ref, start, name=WREN):
    db.upsert_detection({
        "source": "birdnet", "source_ref": ref, "common_name": name,
        "scientific_name": "Thryothorus ludovicianus", "species_code": "carwre", "confidence": 0.91,
        "location": "mic", "start_time": start, "end_time": start + 3, "has_clip": 1,
        "has_snapshot": 0, "clip_ref": f"{ref}.wav", "snapshot_ref": None, "native_id": ref,
        "raw_json": None, "created_at": start,
    })


class Fake:
    """Stand-in for the HTTP layer: records what would have been sent."""

    def __init__(self):
        self.created: list[dict] = []
        self.uploads: list[tuple] = []
        self.fail_create = False
        self.fail_upload = False
        self.next_id = 100

    async def create(self, payload):
        if self.fail_create:
            raise RuntimeError("iNaturalist rejected the observation (422): boom")
        self.created.append(payload)
        self.next_id += 1
        return self.next_id, inat.OBS_URL.format(self.next_id)

    async def upload(self, obs_id, filename, content, mime):
        self.uploads.append((obs_id, filename, mime, len(content)))
        return "photo upload failed (422): nope" if self.fail_upload else None

    async def fetch(self, url, fallbacks=()):
        if "snapshot" in url:
            return b"\xff\xd8jpegbytes", "image/jpeg"
        if "/audio/" in url:
            return b"RIFFwavbytes", "audio/wav"
        return None


@pytest.fixture()
def world(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "aviary.db"))
    crops.configure(str(tmp_path))
    ingest.configure(ignore_unclassified=False, ignore_cameras=())
    ingest.set_identify_hook(None)
    ingest.set_keepsake_hook(None)
    ingest.set_new_species_hook(None)
    with ingest._known_lock:
        ingest._known_species.clear()
        ingest._announced_refs.clear()
        ingest._tombstones.clear()
        ingest._blacklist.clear()
    monkeypatch.setattr(ingest.notify, "enabled", lambda: False)
    settings = make_settings(
        tmp_path, inat_app_id="app", inat_app_secret="my-secret", inat_username="chase",
        inat_password="pw", frigate_url="http://frigate", birdnet_url="http://birdnet",
    )
    inat.configure(settings)
    inat._post_lock = None
    inat._pending.clear()
    inat._loop = None
    solar.set_location(LOC)
    fake = Fake()
    monkeypatch.setattr(inat, "_create_observation", fake.create)
    monkeypatch.setattr(inat, "_upload_photo", fake.upload)
    monkeypatch.setattr(inat, "_upload_sound", fake.upload)
    monkeypatch.setattr(inat, "_fetch_bytes", fake.fetch)

    async def _taxon(name, sci=None):
        return 9721 if name == CARD else None
    monkeypatch.setattr(species_info, "taxon_id", _taxon)
    yield SimpleNamespace(fake=fake, settings=settings, tmp_path=tmp_path)
    solar.set_location(None)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ pure builders

def test_observed_on_string_carries_the_offset():
    tz = timezone(timedelta(hours=-5))
    assert inat.observed_on_string(1_750_000_000.0, tz) == "2025-06-15T10:06:40-05:00"


def test_build_observation_prefers_taxon_and_states_provenance():
    sighting = {"source": "frigate", "source_ref": "e1", "common_name": CARD,
                "scientific_name": "Cardinalis cardinalis", "start_time": T0, "location": CAM,
                "zone": "bath", "confidence": 0.87, "id_status": "ok"}
    settings = SimpleNamespace(inat_geoprivacy="obscured", inat_positional_accuracy_m=30)
    body = inat.build_observation(sighting, LOC, settings, 9721, "Cardinalis cardinalis", timezone.utc)
    obs = body["observation"]
    assert obs["taxon_id"] == 9721 and "species_guess" not in obs
    assert obs["latitude"] == 41.0 and obs["longitude"] == -87.0
    assert obs["positional_accuracy"] == 30 and obs["geoprivacy"] == "obscured"
    assert obs["captive_flag"] is False and obs["time_zone"] == "America/Chicago"
    assert obs["observed_on_string"].endswith("+00:00")
    assert "birdzone" in obs["description"] and "zone bath" in obs["description"]
    assert "87%" in obs["description"] and "aviary-id" in obs["description"]

    # No taxon → species_guess falls back to the scientific name, then the common name.
    body = inat.build_observation(sighting, LOC, settings, None, None)
    assert body["observation"]["species_guess"] == "Cardinalis cardinalis"
    sighting["scientific_name"] = None
    body = inat.build_observation(sighting, solar.Location(1.0, 2.0), settings, None, None)
    assert body["observation"]["species_guess"] == CARD
    assert "time_zone" not in body["observation"]


def test_description_for_audio_names_birdnet():
    text = inat.description({"source": "birdnet", "location": "mic", "confidence": 0.91})
    assert "BirdNET-Go" in text and "91%" in text and "Frigate" not in text


def test_media_plan_order():
    assert inat.media_plan({"source": "frigate", "source_ref": "e1", "has_snapshot": 1}) == [("photo", "snapshot")]
    assert inat.media_plan({"source": "frigate", "source_ref": "e1", "has_snapshot": 0}) == []
    assert inat.media_plan({"source": "birdnet", "native_id": "7"}) == [("sound", "birdnet")]
    assert inat.media_plan({"source": "birdnet"}) == []


def test_jwt_expiry_reads_exp_and_falls_back():
    now = 1_000_000.0
    payload = base64.urlsafe_b64encode(json.dumps({"exp": now + 500}).encode()).decode().rstrip("=")
    token = f"hdr.{payload}.sig"
    assert inat._jwt_expiry(token, now) == now + 500
    assert inat._jwt_expiry("garbage", now) == now + inat._JWT_FALLBACK_TTL_S
    expired = base64.urlsafe_b64encode(json.dumps({"exp": now - 5}).encode()).decode()
    assert inat._jwt_expiry(f"h.{expired}.s", now) == now + inat._JWT_FALLBACK_TTL_S


# ------------------------------------------------------------------- sightings

def test_pick_sighting_prefers_kept_first_then_oldest_then_audio(world):
    event("e2", T0 + 100)
    event("e1", T0)          # older, arrives second
    assert inat.pick_sighting(CARD)["source_ref"] == "e1"
    # A kept "first" keepsake wins even when an older row exists (its media survives).
    e2 = db.detection_by_ref("frigate", "e2")
    db.set_keepsake(CARD, "first", e2["id"], 0, T0 + 100)
    assert inat.pick_sighting(CARD)["source_ref"] == "e2"
    # Heard-only species fall back to the oldest BirdNET-Go row.
    birdnet("w2", T0 + 50)
    birdnet("w1", T0 + 10)
    picked = inat.pick_sighting(WREN)
    assert picked["source"] == "birdnet" and picked["source_ref"] == "w1"
    assert inat.pick_sighting("Blue Jay") is None


def test_crop_beats_snapshot(world):
    event("e1", T0)
    crops.save("e1", base64.b64encode(b"\xff\xd8crop").decode())
    sighting = inat.pick_sighting(CARD)
    assert inat.media_plan(sighting) == [("photo", "crop")]


# ------------------------------------------------------------------- posting

def test_post_is_idempotent_and_attaches_media(world):
    event("e1", T0)
    result = run(inat.post_species(CARD))
    assert result["ok"] and result["observation_url"].endswith("/101")
    assert result["media"] == ["photo"] and result["error"] is None
    assert world.fake.created[0]["observation"]["taxon_id"] == 9721
    assert world.fake.uploads[0][1].startswith("northern-cardinal") and world.fake.uploads[0][2] == "image/jpeg"
    row = db.inat_get(CARD)
    assert row["status"] == "posted" and row["media"] == "photo" and row["observed_at"] == T0

    again = run(inat.post_species(CARD))
    assert again["ok"] is False and again["error"] == "already on iNaturalist"
    assert again["observation_url"] == result["observation_url"]
    assert len(world.fake.created) == 1

    assert db.inat_forget(CARD)["observation_id"] == 101
    third = run(inat.post_species(CARD))
    assert third["ok"] and len(world.fake.created) == 2
    assert set(db.inat_all()) == {CARD.lower()}


def test_audio_only_species_posts_a_sound(world):
    birdnet("w1", T0 + 10)
    result = run(inat.post_species(WREN))
    assert result["ok"] and result["media"] == ["sound"]
    assert world.fake.uploads[0][1] == "carolina-wren.wav" and world.fake.uploads[0][2] == "audio/wav"
    assert world.fake.created[0]["observation"]["species_guess"] == "Thryothorus ludovicianus"


def test_failed_create_is_recorded_not_raised(world):
    event("e1", T0)
    world.fake.fail_create = True
    result = run(inat.post_species(CARD))
    assert result["ok"] is False and "422" in result["error"]
    row = db.inat_get(CARD)
    assert row["status"] == "failed" and "422" in row["error"] and row["observation_id"] is None
    assert CARD in db.get_pref("inat_last_error")
    assert db.inat_all() == {}
    # The next attempt simply tries again.
    world.fake.fail_create = False
    assert run(inat.post_species(CARD))["ok"]


def test_failed_upload_keeps_the_observation(world):
    event("e1", T0)
    world.fake.fail_upload = True
    result = run(inat.post_species(CARD))
    assert result["ok"] and result["media"] == [] and "upload failed" in result["error"]
    row = db.inat_get(CARD)
    assert row["status"] == "posted" and row["media"] == "" and "upload failed" in row["error"]


def test_gate_and_location_and_config_guards(world, monkeypatch):
    event("e1", T0)
    inat.configure(make_settings(world.tmp_path, inat_app_id="a", inat_app_secret="s",
                                 inat_username="u", inat_password="p",
                                 require_species_confirmation=True))
    assert run(inat.post_species(CARD))["error"] == "confirm the species first"
    db.confirm_species(CARD)
    solar.set_location(None)
    assert "location" in run(inat.post_species(CARD))["error"]
    solar.set_location(LOC)
    assert run(inat.post_species(CARD))["ok"]
    inat.configure(make_settings(world.tmp_path))  # credentials blank → off
    assert inat.enabled() is False
    assert run(inat.post_species(WREN))["error"] == "iNaturalist is not configured"
    assert run(inat.preview(WREN))["configured"] is False


def test_preview_describes_what_would_be_sent(world):
    event("e1", T0)
    p = run(inat.preview(CARD))
    assert p["configured"] and p["posted"] is False
    assert p["media"] == ["photo (Frigate snapshot)"] and p["taxon"] == "iNaturalist taxon 9721"
    assert "obscured" in p["location"] and p["observed"].startswith("2026-")


def test_settings_inat_enabled_needs_all_four(tmp_path):
    assert make_settings(tmp_path).inat_enabled is False
    assert make_settings(tmp_path, inat_app_id="a", inat_app_secret="s",
                         inat_username="u", inat_password="p").inat_enabled is True
    assert make_settings(tmp_path, inat_app_id="a", inat_app_secret="s",
                         inat_username="u").inat_enabled is False


def test_auto_post_hooks_respect_the_settings(world):
    inat.configure(make_settings(world.tmp_path, inat_app_id="a", inat_app_secret="s",
                                 inat_username="u", inat_password="p", inat_auto_post=True,
                                 require_species_confirmation=True))
    inat.on_new_species(CARD)       # gate on: first detection is not the moment
    assert inat._pending == set()
    inat.on_confirmed(CARD)         # confirmation is
    assert inat._pending == {CARD}
    inat._pending.clear()
    inat.configure(make_settings(world.tmp_path, inat_app_id="a", inat_app_secret="s",
                                 inat_username="u", inat_password="p", inat_auto_post=False))
    inat.on_confirmed(CARD)
    assert inat._pending == set()
