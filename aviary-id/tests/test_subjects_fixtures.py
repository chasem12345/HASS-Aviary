"""Real two-bird events, re-partitioned offline with the shipped defaults.

The fixtures are ``debug.crops`` from live events at a bird bath (2026-09-13, captured with
``tools/tune_subjects.py --save``): every crop's geometry, BioCLIP embedding and own top-1.
Each expectation says which bird the primary must be (its modal top-1) and which species
must not appear in it — a cardinal crop in the wren's primary is exactly the poison this
partition exists to prevent. Same footage in two forms: ``wide`` (the detect camera, with
path anchors) and ``zoom`` (the PTZ recordings, no anchors). Re-capture with the tool
after a model change; the embeddings are only comparable under the model that made them.

The three ``zoom-17899509…/17899510…`` captures are from 2026-09-20 at dusk: the wide camera
was in infrared, Frigate's crop scored 15–36 % on some sparrow, and every zoomed frame was a
Northern Cardinal at 99 % (two of them in view). Under 0.12.1 that seed's junk vote barred
all of them from the primary; they are the regression corpus for confidence-gated votes and
zoom anchoring.
"""

from __future__ import annotations

import glob
import json
import os

import numpy as np
import pytest

from app.settings import load_settings
from app.subjects import partition
from tools.tune_subjects import decode, metas_from

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "subjects")

# fixture -> (primary species, species that must NOT be in the primary, min other birds)
# ``zoom-…6h2c7e``: min others is 0 because its only "other" under 0.12.1 was a Tufted
# Titmouse chip at 20 % on one crop — no confident species, never beside the tracked bird —
# which the gate now drops by design. ``zoom-…bqcdhd`` is the classifier-error fixture:
# the "Common Poorwill" crops are the same wren four times over (0.5–0.6 votes), and its
# ban is what pins SUBJECT_VOTE_MIN at or below 0.58.
EXPECT = {
    "wide-1789139122.196988-ic0qlt": ("House Sparrow", ["Northern Cardinal"], 1),
    "wide-1789139129.545499-zm3hxb": ("House Sparrow", ["Northern Cardinal", "House Finch"], 2),
    "wide-1789300531.848486-6h2c7e": ("Northern Cardinal", ["Tufted Titmouse", "Bewick's Wren"], 1),
    "wide-1789300535.181523-bqcdhd": ("Carolina Wren", ["Northern Cardinal", "Eastern Screech-Owl"], 1),
    "wide-1789300541.335185-fly90a": ("Carolina Wren", ["Northern Cardinal"], 1),
    "wide-1789300562.743139-q8wep1": ("Carolina Wren", ["Northern Cardinal"], 1),
    "zoom-1789139122.196988-ic0qlt": ("House Sparrow", [], 0),
    "zoom-1789139129.545499-zm3hxb": ("House Sparrow", ["Northern Cardinal", "House Finch"], 2),
    "zoom-1789300531.848486-6h2c7e": ("Northern Cardinal", ["Tufted Titmouse"], 0),
    "zoom-1789300535.181523-bqcdhd": ("Carolina Wren", ["Common Poorwill"], 1),
    "zoom-1789300541.335185-fly90a": ("Carolina Wren", ["Northern Cardinal"], 1),
    "zoom-1789300562.743139-q8wep1": ("Carolina Wren", ["Northern Cardinal"], 1),
    "zoom-1789323648.128255-l0wjid": ("Carolina Wren", ["Northern Cardinal"], 1),
    "zoom-1789324332.171108-r5zh7g": ("Carolina Wren", [], 0),
    "zoom-1789324346.170161-10il72": ("Carolina Wren", [], 0),
    "zoom-1789327867.419263-11dfza": ("Carolina Wren", [], 0),
    "zoom-1789950953.4608-vtzp8q": ("Northern Cardinal", [], 1),
    "zoom-1789951022.463391-l415kn": ("Northern Cardinal", [], 1),
    "zoom-1789951025.849643-ctnr22": ("Northern Cardinal", [], 1),
}

# Captures where the primary must hold at least this many crops: under 0.12.1 each of
# these primaries was Frigate's single infrared crop and the bird itself was "in view".
MIN_PRIMARY_CROPS = {
    "zoom-1789950953.4608-vtzp8q": 5,
    "zoom-1789951022.463391-l415kn": 5,
    "zoom-1789951025.849643-ctnr22": 4,
}


def _defaults(monkeypatch):
    for key in ("SUBJECT_SIM_MERGE", "SUBJECT_SIM_SPLIT", "SUBJECT_SIM_PRIMARY",
                "SUBJECT_SIM_SECONDARY", "SUBJECT_MIN_CROPS", "SUBJECT_SINGLE_DET", "SUBJECT_MAX",
                "SUBJECT_VOTE_MIN", "SUBJECT_SIM_CROSS"):
        monkeypatch.delenv(key, raising=False)
    return load_settings()


@pytest.mark.parametrize("name", sorted(EXPECT))
def test_real_event_partitions_cleanly(name, monkeypatch):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
        fx = json.load(f)
    s = _defaults(monkeypatch)
    crops = fx["crops"]
    feats = np.stack([decode(c["embedding"]) for c in crops])
    metas = metas_from(crops, fx.get("footage"))
    subs = partition(
        metas, feats,
        sim_merge=s.subject_sim_merge, sim_split=s.subject_sim_split,
        min_secondary_crops=s.subject_min_crops, single_crop_det=s.subject_single_det,
        max_subjects=s.subject_max, sim_primary=s.subject_sim_primary,
        sim_secondary=s.subject_sim_secondary, vote_min=s.subject_vote_min,
        sim_cross=s.subject_sim_cross,
    )
    species, banned, min_others = EXPECT[name]
    primary = subs[0]
    # The primary's species is the modal CONFIDENT vote; a junk 15 % seed does not count.
    names = [crops[i]["top1"] for i in primary.indices
             if crops[i].get("top1") and crops[i].get("top1_score", 1.0) >= s.subject_vote_min]
    assert max(set(names), key=names.count) == species, names
    assert not (set(names) & set(banned)), f"{name}: primary holds {set(names) & set(banned)}"
    assert len(subs) - 1 >= min_others, [[crops[i]["top1"] for i in x.indices] for x in subs]
    assert len(primary.indices) >= MIN_PRIMARY_CROPS.get(name, 1), primary.indices
    # No reported "other bird" rests on one crop that was never seen to be a second bird:
    # beside the tracked bird in a frame, in a frame with two boxes, or off the path.
    for other in subs[1:]:
        assert len(other.indices) >= 2 or other.co_occurring > 0 or any(
            metas[i].frame_boxes >= 2 or (metas[i].anchor_dist or 0) > 0.15
            for i in other.indices), (name, other.indices, other.notes)


def test_every_fixture_has_an_expectation():
    found = {os.path.splitext(os.path.basename(p))[0]
             for p in glob.glob(os.path.join(FIXTURES, "*.json"))}
    assert found == set(EXPECT)
