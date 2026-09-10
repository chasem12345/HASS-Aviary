"""Recap view-model shaping: tiers, ribbons, highlights, the dawn card — plus one
round-trip through the new db queries. Run from the ``aviary`` directory:

    python -m pytest tests -q
"""

from __future__ import annotations

import time
from datetime import date, timedelta, timezone

import pytest

from app import db, recap, solar

UTC = timezone.utc
DAY = date(2024, 6, 21)
LONDON = solar.Location(51.5074, -0.1278)
SUN = solar.sun_times(DAY, LONDON, tz=UTC)          # sunrise ≈ 03:43 UTC
NOSUN = solar.sun_times(DAY, None, tz=UTC, use_default=False)
START, END = SUN.day_start, SUN.day_end


def at(hours: float) -> float:
    return START + hours * 3600


def days_ago(n: int, now: float) -> float:
    return now - n * 86400


# ------------------------------------------------------------------- attendance

def test_tier_needs_two_weeks_of_history():
    now = time.time()
    assert recap.tier(days_ago(5, now), 5, now) is None
    assert recap.tier(None, 5, now) is None
    assert recap.tier(days_ago(20, now), 0, now) is None


@pytest.mark.parametrize("active, history, key", [
    (30, 30, "daily"), (21, 30, "daily"), (20, 30, "regular"), (9, 30, "regular"),
    (4, 30, "occasional"), (2, 30, "rare"), (1, 100, "rare"),
])
def test_tier_thresholds(active, history, key):
    now = time.time()
    t = recap.tier(days_ago(history - 1, now), active, now)
    assert t is not None and t["key"] == key
    assert t["history_days"] == history


def test_back_after_gap():
    first = at(6)
    assert recap.back_after(first, first - 13 * 86400) is None
    assert recap.back_after(first, first - 14 * 86400) == 14
    assert recap.back_after(first, first - 40 * 86400) == 40
    assert recap.back_after(first, None) is None


def test_sparkline_fills_missing_days_oldest_first():
    today = date(2024, 6, 21)
    series, peak = recap.sparkline({"2024-06-21": 3, "2024-06-19": 7}, today)
    assert series == [0, 0, 0, 0, 7, 0, 3]
    assert peak == 7


# --------------------------------------------------------------------- geometry

def test_pct_is_clamped_and_uses_real_day_length():
    assert recap.pct(at(6), START, END) == 25.0
    assert recap.pct(START - 5, START, END) == 0.0
    assert recap.pct(END + 5, START, END) == 100.0
    # A 25-hour (DST) day: 06:00 is no longer a quarter of the way.
    assert recap.pct(at(6), START, END + 3600) == 24.0


def test_day_ribbon_ticks_carry_source_and_time():
    ticks = [{"t": at(6), "source": "frigate", "n": 3}, {"t": at(18.5), "source": "birdnet", "n": 1}]
    r = recap.day_ribbon(ticks, START, END)
    assert r["mode"] == "ticks"
    assert r["ticks"][0]["pct"] == 25.0 and r["ticks"][0]["cls"] == "seen"
    assert "visit of 3 clips" in r["ticks"][0]["title"]
    assert r["ticks"][1]["cls"] == "heard" and "heard" in r["ticks"][1]["title"]


def test_day_ribbon_collapses_dense_rows_to_bins():
    ticks = [{"t": at(6 + i / 60), "source": "birdnet", "n": 1} for i in range(200)]
    r = recap.day_ribbon(ticks, START, END)
    assert r["mode"] == "bins"
    # 200 minutes from 06:00 span 06:00–09:20 → 14 quarter-hour bins, all heard.
    assert 13 <= len(r["bins"]) <= 15
    assert all(b["cls"] == "heard" for b in r["bins"])
    assert max(b["a"] for b in r["bins"]) == 1.0


def test_hour_ribbon_only_emits_busy_hours():
    hours = [{"hour": h, "seen": 0, "heard": 0} for h in range(24)]
    hours[5]["heard"] = 4
    hours[17]["seen"] = 2
    hours[17]["heard"] = 2
    r = recap.hour_ribbon(hours)
    assert r["mode"] == "hours" and [b["count"] for b in r["bars"]] == [4, 4]
    assert r["bars"][0]["h"] == 100.0 and r["bars"][1]["seen_share"] == 50.0
    assert r["bars"][1]["pct"] == pytest.approx(17 / 24 * 100, abs=0.01)


def test_strip_from_ticks_matches_rows():
    ticks = {"A": [{"t": at(6.2), "source": "frigate", "n": 1}],
             "B": [{"t": at(6.9), "source": "birdnet", "n": 1}, {"t": at(19), "source": "birdnet", "n": 1}]}
    strip = recap.strip_from_ticks(ticks, START, END)
    assert len(strip) == 24
    assert strip[6]["count"] == 2 and strip[6]["seen"] == 1 and strip[6]["heard"] == 1
    assert strip[19]["count"] == 1 and strip[19]["h"] == 50.0
    assert sum(b["count"] for b in strip) == 3


# ------------------------------------------------------------------ sun overlay

def _percents(gradient: str) -> list[float]:
    toks = gradient.replace(",", " ").replace(")", " ").split()
    return [float(tok.rstrip("%")) for tok in toks if tok.endswith("%")]


def test_shading_gradient_is_monotonic_when_sun_known():
    g = recap.shading_gradient(SUN)
    assert g.startswith("linear-gradient(90deg,")
    assert "--ribbon-night" in g and "--ribbon-dawn" in g and "--ribbon-dusk" in g
    ps = _percents(g)
    assert ps == sorted(ps) and ps[0] == 0.0 and ps[-1] == 100.0


def test_shading_gradient_without_sun_only_marks_the_fallback_window():
    g = recap.shading_gradient(NOSUN)
    assert "--ribbon-night" not in g and "--ribbon-dawn" in g
    # 05:00 and 07:30 of a 24 h day.
    assert 20.83 in _percents(g) and 31.25 in _percents(g)


def test_markers_only_when_known():
    m = recap.markers(SUN)
    assert [x["kind"] for x in m] == ["sunrise", "sunset"]
    assert m[0]["pct"] == pytest.approx(recap.pct(SUN.sunrise, START, END))
    assert recap.markers(NOSUN) == []


# ------------------------------------------------------------------- page parts

def rows_fixture():
    return [
        {"common_name": "Carolina Wren", "scientific_name": "T. ludovicianus", "snapshot_ref": None,
         "count": 12, "seen": 0, "heard": 12, "first_time": at(3.3), "last_time": at(6.5),
         "is_first_ever": False, "tier": {"key": "daily", "label": "daily", "ratio": 0.9,
                                          "days_active": 90, "history_days": 100}},
        {"common_name": "Blue Jay", "scientific_name": None, "snapshot_ref": "ev1",
         "count": 5, "seen": 4, "heard": 1, "first_time": at(9), "last_time": at(20.2),
         "is_first_ever": False, "tier": {"key": "rare", "label": "rare", "ratio": 0.05,
                                          "days_active": 5, "history_days": 100}},
        {"common_name": "Indigo Bunting", "scientific_name": None, "snapshot_ref": None,
         "count": 1, "seen": 1, "heard": 0, "first_time": at(12), "last_time": at(12),
         "is_first_ever": True, "tier": None},
    ]


def test_dawn_card_uses_the_chorus_window_edges():
    ticks = {
        "Carolina Wren": [{"t": SUN.chorus_start - 1, "source": "birdnet", "n": 1},
                          {"t": SUN.chorus_start, "source": "birdnet", "n": 1},
                          {"t": SUN.chorus_end - 1, "source": "frigate", "n": 2}],
        "Blue Jay": [{"t": SUN.chorus_end, "source": "frigate", "n": 1}],
    }
    card = recap.dawn_card(rows_fixture(), ticks, SUN)
    assert [c["common_name"] for c in card] == ["Carolina Wren"]
    assert card[0]["heard"] == 1 and card[0]["seen"] == 1
    assert card[0]["first"] == SUN.chorus_start


def test_highlights_day_view():
    tiles = recap.highlights(rows_fixture(), multi=False, days=1, new_count=1)
    labels = {t["label"]: t for t in tiles}
    assert labels["Busiest"]["name"] == "Carolina Wren"
    assert labels["First bird"]["name"] == "Carolina Wren"
    assert labels["Last bird"]["name"] == "Blue Jay"
    # Rarest is the rare established bird, never the first-timer.
    assert labels["Rarest visitor"]["name"] == "Blue Jay"


def test_highlights_multi_view_and_no_rarest_among_regulars():
    rows = rows_fixture()
    for r in rows:
        r["days_active"] = 3
        if r["tier"]:
            r["tier"] = dict(r["tier"], key="daily", ratio=0.9)
    rows[0]["days_active"] = 7
    tiles = recap.highlights(rows, multi=True, days=7, new_count=2)
    labels = {t["label"]: t for t in tiles}
    assert labels["Most regular"]["value"] == "7/7"
    assert labels["New species"]["value"] == 2 and labels["New species"]["anchor"] == "new"
    assert "Rarest visitor" not in labels


def test_sort_rows():
    rows = rows_fixture()
    assert [r["common_name"] for r in recap.sort_rows(rows, "first")] == \
        ["Carolina Wren", "Blue Jay", "Indigo Bunting"]
    assert [r["common_name"] for r in recap.sort_rows(rows, "active")] == \
        ["Carolina Wren", "Blue Jay", "Indigo Bunting"]


def test_typical_hours():
    hours = [{"hour": h, "count": 0} for h in range(24)]
    for h, c in ((4, 6), (5, 10), (6, 7), (7, 2)):
        hours[h]["count"] = c
    t = recap.typical_hours(hours, SUN, {"birdnet_total": 20, "frigate_total": 5})
    assert t == {"label": "Dawn singer", "range": "04–07", "phase": "dawn", "peak_hour": 5}
    hours[5]["count"] = 3   # total 18 — still enough
    assert recap.typical_hours(hours, SUN, {}, min_total=100) is None


# -------------------------------------------------------------- db round trip

@pytest.fixture()
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "aviary.db"))
    return db


def _insert(conn, source, ref, name, start, visit_id=None):
    conn.execute(
        """INSERT INTO detections (source, source_ref, common_name, start_time, created_at,
                                   has_clip, has_snapshot, visit_id)
           VALUES (?, ?, ?, ?, ?, 0, 0, ?)""",
        (source, ref, name, start, start, visit_id))


def test_recap_ticks_and_regularity_round_trip(fresh_db):
    day = date.today() - timedelta(days=1)
    start = solar.local_midnight(day)
    with db._connect() as conn:
        # One visit tracked as two Frigate objects -> one tick standing for 2 clips.
        _insert(conn, "frigate", "e1", "Blue Jay", start + 6 * 3600, visit_id=1)
        _insert(conn, "frigate", "e2", "Blue Jay", start + 6 * 3600 + 20, visit_id=1)
        _insert(conn, "birdnet", "b1", "Blue Jay", start + 7 * 3600)
        # Species-less rows never count.
        _insert(conn, "frigate", "e3", "bird", start + 8 * 3600)
        # History for regularity: 40 days ago, well before the window.
        _insert(conn, "birdnet", "b0", "Blue Jay", start - 40 * 86400)
    ticks = db.recap_ticks(start, start + 86400)
    assert list(ticks) == ["Blue Jay"]
    assert [(t["source"], t["n"]) for t in ticks["Blue Jay"]] == [("frigate", 2), ("birdnet", 1)]

    hours = db.hourly_by_species(start, start + 86400)
    assert hours["Blue Jay"][6]["seen"] == 1 and hours["Blue Jay"][7]["heard"] == 1

    db._regularity_cache.clear()
    reg = db.species_regularity(before=start)
    r = reg["blue jay"]
    assert r["days_active"] == 2
    assert r["prev_before"] == pytest.approx(start - 40 * 86400)
    assert recap.back_after(start + 6 * 3600, r["prev_before"]) == 40

    daily = db.daily_counts_by_species(start)
    assert daily["Blue Jay"][day.isoformat()] == 2
