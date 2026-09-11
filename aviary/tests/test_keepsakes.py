"""Species keepsakes: the first and latest camera sighting of every species, kept at
Frigate (retain flag + exports). Drives keepsakes.reconcile against a temp database with
a fake Frigate standing in for proxy.call_upstream — no network. Run from ``aviary``:

    python -m pytest tests -q
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from types import SimpleNamespace

import pytest

from app import db, ingest, keepsakes, kept, proxy
from app.routes import api
from app.settings import Settings

# Local noon on a fixed date, so "+600 s" is the same local day and "+86400" the next.
T0 = time.mktime((2026, 6, 15, 12, 0, 0, 0, 0, -1))
CAM = "birdzone"
CARD = "Northern Cardinal"
ORIOLE = "Baltimore Oriole"


class FakeFrigate:
    """Just enough of Frigate's API: events exist unless ``gone``; retain flips a set;
    exports are queued per camera unless the camera has ``no_recordings``."""

    def __init__(self):
        self.gone: set[str] = set()
        self.retained: set[str] = set()
        self.exports: dict[str, tuple[str, str]] = {}
        self.no_recordings: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    @property
    def gets(self):
        return sum(1 for m, u in self.calls if m == "GET")

    @property
    def export_posts(self):
        return sum(1 for m, u in self.calls if m == "POST" and "/api/export/" in u)

    async def call(self, method, url, json=None, headers=None):
        self.calls.append((method, url))
        m = re.search(r"/api/events/([^/]+)(/retain)?$", url)
        if m:
            ref = m.group(1)
            if ref in self.gone:
                return 404, '{"success": false, "message": "Event not found"}'
            if m.group(2):
                (self.retained.add if method == "POST" else self.retained.discard)(ref)
            return 200, '{"success": true}'
        if "/api/export/" in url:
            camera = url.split("/api/export/")[1].split("/")[0]
            if camera in self.no_recordings:
                return 400, "No recordings found for time range"
            self._n += 1
            eid = f"exp{self._n}"
            self.exports[eid] = (camera, (json or {}).get("name", ""))
            return 202, globals()["json"].dumps({"success": True, "export_id": eid})
        if url.endswith("/api/exports/delete"):
            for eid in (json or {}).get("ids", []):
                self.exports.pop(eid, None)
            return 200, "{}"
        return 500, f"unexpected {method} {url}"


def make_settings(tmp_path, **over) -> Settings:
    base = dict(
        data_dir=str(tmp_path), db_path=str(tmp_path / "aviary.db"), mqtt_host="", mqtt_port=1883,
        mqtt_user="", mqtt_password="", frigate_url="http://frigate", birdnet_url="",
        frigate_topic="frigate/events", birdnet_topic="birdnet", frigate_review_topic="frigate/reviews",
        backfill_on_start=False, ignore_unclassified=False, ignore_cameras=(),
        require_species_confirmation=False, notify_new_species=False, xeno_canto_api_key="",
        identify_url="", identify_token="", identify_enabled=False,
        identify_min_score=0.35, identify_min_margin=0.08, identify_workers=1, identify_timeout=10,
        identify_retain_days=14, identify_use_audio_priors=False, identify_exclude_blacklisted=True,
        identify_zoom_map={}, identify_zoom_start_offset=0.0, identify_zoom_zone_priority=(),
        ha_config_dir=str(tmp_path), clip_pad_seconds=10.0, log_level="debug",
        keepsakes=True, keepsake_video=True,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "aviary.db"))
    ingest.configure(ignore_unclassified=False, ignore_cameras=())
    ingest.set_identify_hook(None)
    ingest.set_keepsake_hook(None)
    with ingest._known_lock:
        ingest._known_species.clear()
        ingest._announced_refs.clear()
        ingest._tombstones.clear()
        ingest._blacklist.clear()
    monkeypatch.setattr(ingest.notify, "enabled", lambda: False)
    frigate = FakeFrigate()
    monkeypatch.setattr(proxy, "call_upstream", frigate.call)
    keepsakes._reconcile_lock = None
    keepsakes._pending.clear()
    keepsakes._loop = None
    settings = make_settings(tmp_path)
    keepsakes.configure(settings)
    return SimpleNamespace(frigate=frigate, settings=settings, tmp_path=tmp_path)


def event(ref, start, label=CARD, end=None, camera=CAM):
    after = {
        "id": ref, "camera": camera, "label": "bird", "sub_label": label, "top_score": 0.8,
        "start_time": start, "end_time": end if end is not None else start + 5,
        "has_clip": True, "has_snapshot": True, "entered_zones": ["bath"],
    }
    ingest.handle_frigate(json.dumps({"type": "end", "before": after, "after": after}).encode())
    return db.detection_by_ref("frigate", ref)["id"]


def run(coro):
    return asyncio.run(coro)


def roles(name):
    return {k: v["detection_id"] for k, v in db.keepsakes_for_species(name).items()}


# ------------------------------------------------------------------------- pinning

def test_first_sighting_is_kept_with_its_clip_exported(world):
    a = event("a", T0)
    stats = run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {"first": a}                 # newest IS the first: no latest
    assert world.frigate.retained == {"a"}
    row = db.keepsake(CARD, "first")
    assert row["export_id"] in world.frigate.exports
    camera, name = world.frigate.exports[row["export_id"]]
    assert camera == CAM and "first sighting" in name and CARD in name
    assert row["zoom_export_id"] is None and row["export_status"] == "queued"
    assert stats["pinned"] == 1 and stats["exports"] == 1
    assert db.keepsake_roles(a) == ["first"]
    # Idempotent: nothing to do the second time, and no new Frigate calls.
    calls = len(world.frigate.calls)
    run(keepsakes.reconcile(CARD))
    assert len(world.frigate.calls) == calls


def test_latest_moves_at_most_once_a_day_and_releases_the_previous(world):
    a = event("a", T0)
    b = event("b", T0 + 600)
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {"first": a, "latest": b}
    b_export = db.keepsake(CARD, "latest")["export_id"]
    # Same local day: the latest stays put.
    event("c", T0 + 1200)
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {"first": a, "latest": b}
    # Next day: it moves, and b is released — flag off, export gone, a untouched.
    d = event("d", T0 + 86400)
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {"first": a, "latest": d}
    assert world.frigate.retained == {"a", "d"}
    assert b_export not in world.frigate.exports
    assert db.keepsake(CARD, "latest")["export_id"] in world.frigate.exports


def test_release_spares_the_users_own_pin(world):
    event("a", T0)
    b = event("b", T0 + 600)
    run(keepsakes.reconcile(CARD))
    db.set_retained(b, True)                          # the user pinned b themselves
    event("d", T0 + 86400)
    run(keepsakes.reconcile(CARD))
    assert roles(CARD)["latest"] != b
    assert "b" in world.frigate.retained             # keepsake gone, user's hold stays


def test_first_falls_back_to_the_oldest_surviving_event(world):
    ids = [event(f"e{i:02d}", T0 + i * 60) for i in range(12)]
    world.frigate.gone.update(f"e{i:02d}" for i in range(5))   # history Frigate expired
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {"first": ids[5], "latest": ids[11]}
    assert world.frigate.gets <= 6                    # binary search, not a linear walk
    # Later passes don't search again (that only happens on the deep start-up sweep).
    gets = world.frigate.gets
    run(keepsakes.reconcile(CARD))
    assert world.frigate.gets == gets
    # Everything gone: nothing kept, nothing broken.
    world.frigate.gone.update(f"e{i:02d}" for i in range(12))
    db.forget_keepsakes(CARD)
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {}


def test_deep_sweep_adopts_older_history(world):
    b = event("b", T0 + 600)
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {"first": b}
    a = event("a", T0)                                # a backfill imported an older event
    run(keepsakes.reconcile(CARD))
    assert roles(CARD)["first"] == b                  # everyday pass: leaves it alone
    run(keepsakes.reconcile(CARD, deep=True))
    assert roles(CARD) == {"first": a, "latest": b}
    assert world.frigate.retained == {"a", "b"}


def test_no_recordings_left_is_recorded_once(world):
    event("a", T0)
    world.frigate.no_recordings.add(CAM)
    run(keepsakes.reconcile(CARD))
    row = db.keepsake(CARD, "first")
    assert row["export_id"] is None and row["export_status"].startswith("failed")
    assert row["export_tried_at"] is not None
    posts = world.frigate.export_posts
    run(keepsakes.reconcile(CARD))
    assert world.frigate.export_posts == posts        # not retried every pass
    assert "a" in world.frigate.retained             # the snapshot is still kept


def test_paired_camera_gets_a_zoomed_export_too(world, tmp_path):
    keepsakes.configure(make_settings(tmp_path, identify_zoom_map={CAM: "ptz"}))
    event("a", T0)
    run(keepsakes.reconcile(CARD))
    row = db.keepsake(CARD, "first")
    assert world.frigate.exports[row["export_id"]][0] == CAM
    camera, name = world.frigate.exports[row["zoom_export_id"]]
    assert camera == "ptz" and name.endswith("zoomed")


def test_subject_only_species_keeps_the_parent_event(world):
    a = event("a", T0)
    db.replace_subjects(a, [
        {"idx": 0, "is_primary": 1, "common_name": CARD, "score": 0.9, "id_status": "ok"},
        {"idx": 1, "is_primary": 0, "common_name": ORIOLE, "scientific_name": "Icterus galbula",
         "score": 0.8, "id_status": "ok"},
    ])
    run(keepsakes.reconcile(ORIOLE))
    row = db.keepsake(ORIOLE, "first")
    assert row["detection_id"] == a and row["subject_idx"] == 1
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {"first": a}
    assert sorted(db.keepsake_roles(a)) == ["first", "first"]
    # Rejecting the oriole on that bird takes its keepsake away; the cardinal's stays.
    db.reject_subject(a, 1, ORIOLE)
    run(keepsakes.reconcile(ORIOLE))
    assert roles(ORIOLE) == {}
    assert "a" in world.frigate.retained


def test_confirmation_gate(world, tmp_path):
    keepsakes.configure(make_settings(tmp_path, require_species_confirmation=True))
    event("a", T0)
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {} and not world.frigate.calls
    db.confirm_species(CARD)
    run(keepsakes.reconcile(CARD))
    assert "first" in roles(CARD)
    db.unconfirm_species(CARD)
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {} and world.frigate.retained == set()


def test_deleting_the_first_repicks_and_releases(world):
    a = event("a", T0)
    b = event("b", T0 + 86400)
    run(keepsakes.reconcile(CARD))
    a_export = db.keepsake(CARD, "first")["export_id"]
    deleted = db.delete_detection(a)
    assert [k["role"] for k in deleted["keepsakes"]] == ["first"]
    run(keepsakes.on_detection_deleted(deleted))
    assert a_export not in world.frigate.exports and "a" not in world.frigate.retained
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {"first": b}                # b is now both first and newest
    assert "b" in world.frigate.retained


def test_forgetting_a_species_releases_everything(world):
    event("a", T0)
    event("b", T0 + 86400)
    run(keepsakes.reconcile(CARD))
    assert len(world.frigate.exports) == 2
    run(keepsakes.forget(CARD))
    assert roles(CARD) == {}
    assert world.frigate.exports == {} and world.frigate.retained == set()


def test_disabled_does_nothing(world, tmp_path):
    keepsakes.configure(make_settings(tmp_path, keepsakes=False))
    event("a", T0)
    run(keepsakes.reconcile(CARD))
    assert roles(CARD) == {} and not world.frigate.calls


# --------------------------------------------------------------- the 📌 route

def test_user_unpin_leaves_a_keepsake_hold_in_place(world):
    a = event("a", T0)
    run(keepsakes.reconcile(CARD))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=world.settings)))
    res = run(api.retain_detection(a, request, keep=1))
    assert res["ok"] and db.detection_by_id(a)["retained_at"]
    res = run(api.retain_detection(a, request, keep=0))
    assert res["ok"] and res.get("held_by") == "keepsake"
    assert db.detection_by_id(a)["retained_at"] is None
    assert "a" in world.frigate.retained             # no DELETE went to Frigate
    assert not any(m == "DELETE" for m, _ in world.frigate.calls)


def test_user_unpin_releases_when_nothing_else_holds(world):
    a = event("a", T0)                                # no keepsake pass ran
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=world.settings)))
    run(api.retain_detection(a, request, keep=1))
    res = run(api.retain_detection(a, request, keep=0))
    assert res["ok"] and "held_by" not in res
    assert world.frigate.retained == set()
    # Pinning an event Frigate no longer has is a clear error, not a silent success.
    world.frigate.gone.add("a")
    res = run(api.retain_detection(a, request, keep=1))
    assert not res["ok"] and "no longer" in res["error"]


def test_kept_helpers_report_gone(world):
    world.frigate.gone.add("x")
    assert run(kept.set_frigate_retain(world.settings, "x", True)) == kept.GONE
    assert run(kept.frigate_has_event(world.settings, "x")) is False
    assert run(kept.frigate_has_event(world.settings, "y")) is True
