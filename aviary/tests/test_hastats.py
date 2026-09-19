"""Bird statistics to Home Assistant (0.34.0): the queries, the payloads, the publish
discipline and the anti-fling rules for the PTZ target — against a temp database and a
fake MQTT client. No broker, no network.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app import db, hastats, ingest
from app.mqtt_client import MqttIngestor
from conftest import frigate_row, make_settings

# Local noon on a fixed date: "+3600" is the same local day, "-86400" is yesterday.
T0 = time.mktime((2026, 6, 15, 12, 0, 0, 0, 0, -1))
DAY = 86400.0
CARD = "Northern Cardinal"
FINCH = "House Finch"
HUMMER = "Ruby-throated Hummingbird"
WARBLER = "Prothonotary Warbler"

DEVICE_TOPIC = "homeassistant/device/aviary/config"


class FakeIngestor:
    def __init__(self):
        self.published: list[tuple[str, str, bool]] = []
        self.on_connect_hooks = []
        self.subscriptions = []
        self.status_topic = None
        self.connected = True

    def enable_status(self, topic):
        self.status_topic = topic

    def add_subscription(self, topic, handler):
        self.subscriptions.append((topic, handler))

    def publish(self, topic, payload, retain=False, qos=0):
        if not self.connected:
            return False
        self.published.append((topic, payload, retain))
        return True

    def topics(self):
        return [t for t, _, _ in self.published]

    def last(self, topic):
        for t, p, _ in reversed(self.published):
            if t == topic:
                return json.loads(p) if p else None
        return None


def reset_module():
    hastats._sent.clear()  # noqa: SLF001
    hastats._pending.clear()  # noqa: SLF001
    hastats._drains.clear()  # noqa: SLF001
    hastats._rarity.clear()  # noqa: SLF001
    hastats._baselines.clear()  # noqa: SLF001
    hastats._slugs.clear()  # noqa: SLF001
    hastats._loop = None  # noqa: SLF001
    hastats._expiry = None  # noqa: SLF001
    hastats._ingestor = None  # noqa: SLF001
    hastats._refresh_lock = None  # noqa: SLF001


@pytest.fixture()
def world(tmp_path, monkeypatch):
    db.init_db(str(tmp_path / "aviary.db"))
    ingest.configure(ignore_unclassified=False, ignore_cameras=())
    ingest.set_identify_hook(None)
    ingest.set_stats_hook(None)
    reset_module()
    settings = make_settings(tmp_path, mqtt_host="broker", require_species_confirmation=True)
    hastats.configure(settings)
    fake = FakeIngestor()
    hastats.attach(fake)
    monkeypatch.setattr(hastats, "time", SimpleNamespace(time=lambda: T0, monotonic=time.monotonic))
    yield SimpleNamespace(fake=fake, settings=settings, tmp_path=tmp_path)
    reset_module()


# ------------------------------------------------------------------------- seeding

def seen(ref: str, species: str, start: float, *, visit: int | None = None, zone: str = "bath",
         end: float | None = "auto") -> int:
    row = frigate_row(ref, start)
    row["common_name"] = species
    row["zone"] = zone
    row["end_time"] = None if end is None else (start + 5 if end == "auto" else end)
    db.upsert_detection(row)
    det_id = db.detection_by_ref("frigate", ref)["id"]
    if visit is not None:
        with db._connect() as conn:  # noqa: SLF001
            conn.execute("UPDATE detections SET visit_id = ? WHERE id = ?", (visit, det_id))
    return det_id


def heard(ref: str, species: str, start: float) -> None:
    db.upsert_detection({
        "source": "birdnet", "source_ref": ref, "common_name": species, "scientific_name": None,
        "species_code": None, "confidence": 0.9, "location": "BirdNET-Go", "start_time": start,
        "end_time": None, "has_clip": 0, "has_snapshot": 0, "clip_ref": None,
        "snapshot_ref": None, "native_id": ref, "raw_json": None, "created_at": start,
    })


def visit(vid: int, start: float, end: float | None, zones: str | None,
          announced: str | None = None) -> None:
    with db._connect() as conn:  # noqa: SLF001
        conn.execute(
            """INSERT INTO visits (id, review_id, camera, start_time, end_time, review_start,
               review_end, severity, zones, raw_json, created_at, updated_at)
               VALUES (?, ?, 'birdzone', ?, ?, ?, ?, 'detection', ?, NULL, ?, ?)""",
            (vid, f"r{vid}", start, end, start, end, zones, start, end or start),
        )
    if announced:
        db.claim_visit_announcement(vid, announced, None)


def a_cardinal_history():
    """Four visits (one of them three fragments) plus two heard rows; last 30 days = 4 active
    days (three with a sighting, one with only a BirdNET hearing — heard counts as present)."""
    for i, off in enumerate((3600, 3700, 3800)):
        seen(f"c{i}", CARD, T0 - off, visit=7)          # today, one visit
    seen("c3", CARD, T0 - 3 * DAY, visit=8)             # recent
    seen("c4", CARD, T0 - 10 * DAY, visit=9)            # in window, not recent
    seen("c5", CARD, T0 - 40 * DAY, visit=10)           # outside the window
    heard("h1", CARD, T0 - 7200)
    heard("h2", CARD, T0 - 20 * DAY)
    db.confirm_species(CARD)


# ---------------------------------------------------------------------- pure rules

def test_slug_rules_and_collisions():
    assert hastats.slug("Bewick's Wren") == "bewicks_wren"
    assert hastats.slug("Black-capped Chickadee") == "black_capped_chickadee"
    assert hastats.slug("Ruby-throated Hummingbird") == "ruby_throated_hummingbird"
    assert hastats.slug("Zoë's Bird  ") == "zoes_bird"
    slugs = hastats.slugs_for(["Black-capped Chickadee", "Black capped Chickadee"])
    assert len(set(slugs.values())) == 2
    # The alphabetically later name (a space sorts before a hyphen) carries the suffix.
    assert slugs["Black capped Chickadee"] == "black_capped_chickadee"
    later = slugs["Black-capped Chickadee"]
    assert later.startswith("black_capped_chickadee_") and len(later) == len("black_capped_chickadee_") + 4
    assert hastats.slugs_for(["Black capped Chickadee", "Black-capped Chickadee"]) == slugs  # stable


@pytest.mark.parametrize("days,window,score,tier", [
    (30, 30, 0, "daily"), (25, 30, 17, "daily"), (9, 30, 70, "regular"),
    (3, 30, 90, "occasional"), (0, 30, 100, "rare"), (45, 30, 0, "daily"),
])
def test_rarity_table(days, window, score, tier):
    assert hastats.rarity(days, window) == (score, tier)


def test_baseline_is_the_visit_weighted_median():
    rar = {HUMMER.lower(): 3, WARBLER.lower(): 95, CARD.lower(): 20}
    rows = [{"zone": "hummer", "common_name": HUMMER, "visits": 20},
            {"zone": "hummer", "common_name": WARBLER, "visits": 1},
            {"zone": "bath, platform", "common_name": CARD, "visits": 2},
            {"zone": "bath", "common_name": WARBLER, "visits": 2},
            {"zone": "pecker", "common_name": "Unknown Thing", "visits": 9}]
    base = hastats.baseline(rows, rar)
    assert base["hummer"] == 3                       # the regulars, not the one-off warbler
    assert base["bath"] == 20                        # even split: lower-median rule
    assert base["platform"] == 20
    assert "pecker" not in base                      # a species with no score contributes nothing


def test_zones_payload_rules():
    rar = {HUMMER.lower(): 3, CARD.lower(): 20, WARBLER.lower(): 95}
    base = {"hummer": 3, "bath": 35}
    occupants = [
        {"zone": "hummer", "species": HUMMER, "since": T0 - 30, "until": None, "source": "frigate"},
        {"zone": "hummer", "species": None, "since": T0 - 10, "until": None, "source": "frigate"},
        {"zone": "bath", "species": None, "since": T0 - 20, "until": None, "source": "frigate"},
        {"zone": "finch", "species": "Not Confirmed", "since": T0 - 5, "until": None, "source": "frigate"},
        {"zone": "pecker", "species": WARBLER, "since": T0 - 40, "until": T0 + 50, "source": "memory"},
        {"zone": "pecker", "species": CARD, "since": T0 - 35, "until": None, "source": "visit"},
    ]
    out = hastats.zones_payload(occupants, rar, base, ["bath", "finch", "hummer", "pecker", "platform"],
                                T0, memory_s=90, window_days=30)
    s = out["zone_scores"]
    assert s == {"hummer": 3, "bath": 35, "finch": 50, "pecker": 95, "platform": 50}
    assert out["zones"]["hummer"]["species"] == HUMMER and out["zones"]["hummer"]["source"] == "frigate"
    assert out["zones"]["bath"]["source"] == "baseline" and out["zones"]["bath"]["species"] is None
    assert out["zones"]["finch"]["source"] == "unconfirmed"      # never raises a zone
    assert out["zones"]["pecker"]["species"] == WARBLER          # rarest present wins the zone
    assert out["target"] == "pecker"
    assert out["zones"]["hummer"]["since"] is not None
    empty = hastats.zones_payload([], rar, base, ["bath"], T0, memory_s=90, window_days=30)
    assert empty["target"] == "none" and empty["zone_scores"] == {"bath": 35}


# ------------------------------------------------------------------------ queries

def test_species_frequency_counts(world):
    a_cardinal_history()
    seen("f1", FINCH, T0 - 3600)                        # unconfirmed
    window, recent = hastats.window_starts(T0, 30, 7)
    rows = {r["common_name"]: r for r in db.species_frequency(window, recent, only_confirmed=True)}
    assert list(rows) == [CARD]
    c = rows[CARD]
    assert (c["seen_all_time"], c["heard_all_time"]) == (4, 2)   # three fragments = one visit
    assert (c["seen_recent"], c["heard_recent"]) == (2, 1)
    assert c["days_active"] == 4
    assert c["confirmed"] == 1
    both = db.species_frequency(window, recent, only_confirmed=False)
    assert {r["common_name"] for r in both} == {CARD, FINCH}
    assert next(r for r in both if r["common_name"] == FINCH)["confirmed"] == 0


def test_window_starts_are_local_midnights():
    window, recent = hastats.window_starts(T0, 30, 7)
    assert time.localtime(window).tm_hour == 0 and time.localtime(recent).tm_hour == 0
    assert (T0 - window) / DAY < 30 and (T0 - window) / DAY > 29
    assert (T0 - recent) / DAY < 7 and (T0 - recent) / DAY > 6


def test_zone_baselines_rows(world):
    seen("z1", HUMMER, T0 - DAY, visit=1, zone="hummer")
    seen("z2", HUMMER, T0 - 2 * DAY, visit=2, zone="hummer")
    seen("z3", WARBLER, T0 - DAY, visit=3, zone="hummer, bath")
    seen("z4", WARBLER, T0 - 60 * DAY, visit=4, zone="hummer")   # outside the window
    db.confirm_species(HUMMER)
    db.confirm_species(WARBLER)
    window, _ = hastats.window_starts(T0, 30, 7)
    rows = {(r["zone"], r["common_name"]): r["visits"] for r in db.zone_baselines(window, True)}
    assert rows == {("hummer", HUMMER): 2, ("hummer, bath", WARBLER): 1}


def test_active_zone_occupants(world):
    seen("o1", CARD, T0 - 30, zone="bath", end=None)                    # live, named
    seen("o2", "bird", T0 - 10, zone="hummer", end=None, visit=20)       # live, unnamed fragment
    visit(20, T0 - 300, None, "hummer", announced=HUMMER)               # open visit knows the bird
    visit(21, T0 - 400, T0 - 60, "finch", announced=FINCH)              # closed 60 s ago: memory
    visit(22, T0 - 900, T0 - 120, "pecker", announced=WARBLER)          # closed 120 s ago: gone
    seen("o3", CARD, T0 - 3 * 3600, zone="platform", end=None)          # never closed: stale
    seen("o4", FINCH, T0 - 200, zone="platformtop", end=T0 - 50)        # ended 50 s ago: memory
    out = db.active_zone_occupants(T0, memory_s=90, stale_s=7200)
    by_zone = {}
    for o in out:
        by_zone.setdefault(o["zone"], []).append(o)
    assert [o["source"] for o in by_zone["bath"]] == ["frigate"]
    assert by_zone["bath"][0]["species"] == CARD and by_zone["bath"][0]["until"] is None
    hummer = sorted(by_zone["hummer"], key=lambda o: o["source"])
    assert [(o["source"], o["species"]) for o in hummer] == [("frigate", None), ("visit", HUMMER)]
    assert by_zone["finch"][0]["source"] == "memory"
    assert by_zone["finch"][0]["until"] == pytest.approx(T0 - 60 + 90)
    assert "pecker" not in by_zone and "platform" not in by_zone
    assert by_zone["platformtop"][0]["source"] == "memory"


# ---------------------------------------------------------------------- publishing

def test_refresh_publishes_discovery_states_and_zones_once(world):
    a_cardinal_history()
    n = asyncio.run(hastats.refresh_species())
    fake = world.fake
    assert n >= 3
    device = fake.last(DEVICE_TOPIC)
    comps = device["components"]
    assert set(comps) == {"species_northern_cardinal", "ptz_target"}
    sp = comps["species_northern_cardinal"]
    assert sp["default_entity_id"] == "sensor.aviary_northern_cardinal"
    assert sp["state_topic"] == sp["json_attributes_topic"] == "aviary/species/northern_cardinal/state"
    assert sp["value_template"] == "{{ value_json.rarity }}" and sp["platform"] == "sensor"
    assert device["availability_topic"] == "aviary/status"
    assert device["device"]["identifiers"] == ["aviary"]
    state = fake.last("aviary/species/northern_cardinal/state")
    assert state["rarity"] == 87 and state["tier"] == "occasional"
    assert (state["seen_all_time"], state["seen_7d"], state["heard_7d"], state["days_active_30d"]) == (4, 2, 1, 4)
    assert state["first_seen"].startswith("2026-05-06") and state["confirmed"] is True
    zones = fake.last("aviary/zones/state")
    assert zones["zone_scores"] == {"bath": 87}          # every known zone, scored by baseline
    assert zones["zones"]["bath"]["source"] == "baseline"
    assert all(retain for _, _, retain in fake.published)
    assert json.loads(db.get_pref("hastats_slugs")) == ["northern_cardinal"]
    # Nothing changed: nothing published.
    before = len(fake.published)
    assert asyncio.run(hastats.refresh_species()) == 0
    assert len(fake.published) == before
    # A new sighting republishes that species and the zones map only.
    seen("c9", CARD, T0 - 600, visit=11, zone="finch")
    asyncio.run(hastats.refresh_species())
    new = fake.topics()[before:]
    assert "aviary/species/northern_cardinal/state" in new and DEVICE_TOPIC not in new
    assert fake.last("aviary/species/northern_cardinal/state")["seen_all_time"] == 5
    assert fake.last("aviary/zones/state")["zone_scores"] == {"bath": 87, "finch": 87}


def test_live_named_bird_scores_its_zone(world):
    a_cardinal_history()
    seen("h0", HUMMER, T0 - 5 * DAY, visit=30, zone="hummer")
    for d in range(28):
        seen(f"hh{d}", HUMMER, T0 - d * DAY - 1000, visit=100 + d, zone="hummer")
    db.confirm_species(HUMMER)
    asyncio.run(hastats.refresh_species())
    z = world.fake.last("aviary/zones/state")
    assert z["zone_scores"]["hummer"] == hastats.rarity(28, 30)[0] == 7   # baseline: its regulars
    # A cardinal shows up at the hummingbird feeder and Frigate names it mid-event.
    seen("live", CARD, T0 - 20, zone="hummer", end=None)
    asyncio.run(hastats.refresh_zones())
    z = world.fake.last("aviary/zones/state")
    assert z["zone_scores"]["hummer"] == 87 and z["zones"]["hummer"]["species"] == CARD
    assert z["target"] == "hummer"


def test_removed_species_is_removed_from_home_assistant(world):
    a_cardinal_history()
    asyncio.run(hastats.refresh_species())
    world.fake.published.clear()
    db.delete_species(CARD)
    asyncio.run(hastats.refresh_species())
    topics = world.fake.topics()
    assert topics[0] == DEVICE_TOPIC
    stub = json.loads(world.fake.published[0][1])
    assert stub["components"]["species_northern_cardinal"] == {"platform": "sensor"}
    assert ("aviary/species/northern_cardinal/state", "", True) in world.fake.published
    clean = world.fake.last(DEVICE_TOPIC)
    assert set(clean["components"]) == {"ptz_target"}
    assert json.loads(db.get_pref("hastats_slugs")) == []


def test_connect_hook_republishes_everything(world):
    a_cardinal_history()
    asyncio.run(hastats.refresh_species())
    world.fake.published.clear()
    assert asyncio.run(hastats.refresh_species()) == 0          # cached: quiet
    hooks = world.fake.on_connect_hooks
    assert len(hooks) == 1 and world.fake.status_topic == "aviary/status"
    hooks[0]()                                                   # broker reconnect
    assert world.fake.published[0] == ("aviary/status", "online", True)
    assert asyncio.run(hastats.refresh_species()) >= 3          # cache cleared: full republish
    assert [t for t, _ in world.fake.subscriptions] == ["homeassistant/status"]


def test_debounce_coalesces_nudges(world, monkeypatch):
    calls = []

    async def fake_refresh():
        calls.append(1)
        return 1
    monkeypatch.setattr(hastats, "refresh_species", fake_refresh)
    monkeypatch.setattr(hastats, "_SPECIES_DEBOUNCE_S", 0.01)

    async def scenario():
        hastats.set_event_loop(asyncio.get_running_loop())
        for _ in range(3):
            hastats.on_hook("species")
        await asyncio.sleep(0.1)
    asyncio.run(scenario())
    assert calls == [1]


def test_disabled_without_broker_or_option(tmp_path):
    reset_module()
    for over in ({"mqtt_host": ""}, {"mqtt_host": "broker", "ha_stats": False}):
        hastats.configure(make_settings(tmp_path, **over))
        assert not hastats.enabled()
        fake = FakeIngestor()
        hastats.attach(fake)
        assert fake.status_topic is None and not fake.on_connect_hooks
        hastats.schedule_species()
        assert not hastats._pending  # noqa: SLF001
        assert asyncio.run(hastats.refresh_species()) == 0
    reset_module()


def test_ingest_nudges_the_publisher(world):
    kinds = []
    ingest.set_stats_hook(kinds.append)
    try:
        row = frigate_row("n1", T0 - 30)
        row["common_name"] = CARD
        ingest.store_row(row, live=True, announce=False)
        assert kinds == ["zones"]
        ingest.store_row(dict(row), live=False, announce=False)   # backfill: no nudge
        assert kinds == ["zones"]
    finally:
        ingest.set_stats_hook(None)


def test_mqtt_publish_is_best_effort(tmp_path):
    m = MqttIngestor(make_settings(tmp_path, mqtt_host="broker"))
    assert m.publish("t", "p") is False                          # no client yet

    class Boom:
        def publish(self, *a, **k):
            raise RuntimeError("socket")

    class Ok:
        def publish(self, topic, payload, qos=0, retain=False):
            return SimpleNamespace(rc=0)
    m._client, m.connected = Boom(), True  # noqa: SLF001
    assert m.publish("t", "p") is False
    m._client = Ok()  # noqa: SLF001
    assert m.publish("t", "p", retain=True) is True
    m.connected = False
    assert m.publish("t", "p") is False
