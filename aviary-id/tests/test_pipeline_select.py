"""Crop selection (pipeline.localize / diverse) without a GPU: fake detections, tiny images."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from app import frames, pipeline
from app.settings import Settings


@dataclass
class Det:
    box: tuple[float, float, float, float]
    score: float

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.box
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def settings() -> Settings:
    return Settings(frigate_url="http://frigate")


def frame(origin: str, anchor=None) -> frames.Candidate:
    return frames.Candidate(image=Image.new("RGB", (1000, 1000)), origin=origin, anchor=anchor)


def test_localize_carries_geometry_and_penalizes_off_path():
    cands = [frames.Candidate(image=Image.new("RGB", (160, 160)), origin="thumbnail",
                              pre_cropped=True, score=0.9),
             frame("clip@1.00s", anchor=(0.3, 0.6))]
    dets = {1: [Det((250, 400, 350, 600), 0.8),     # bottom-center (0.3, 0.6): on path
                Det((650, 400, 750, 600), 0.8)]}    # (0.7, 0.6): off path
    ranked = pipeline.localize(cands, dets, settings())
    keys = [c.key for c in ranked]
    assert keys[0] == "thumbnail" or keys[0] == "clip@1.00s#0"
    on = next(c for c in ranked if c.key == "clip@1.00s#0")
    off = next(c for c in ranked if c.key == "clip@1.00s#1")
    assert on.anchor_dist == 0.0 and off.anchor_dist > 0.15
    assert off.rank == on.rank * pipeline._ANCHOR_PENALTY
    assert on.center == (0.3, 0.6) and on.t == 1.0 and not on.pre_cropped
    thumb = next(c for c in ranked if c.pre_cropped)
    assert thumb.center is None and thumb.anchor_dist is None and thumb.t is None


def test_diverse_reserves_room_for_a_second_bird():
    cands = [frame("clip@1.00s", anchor=(0.3, 0.6)), frame("clip@2.00s", anchor=(0.3, 0.6)),
             frame("clip@3.00s", anchor=(0.3, 0.6)), frame("clip@4.00s", anchor=(0.3, 0.6))]
    dets = {i: [Det((250, 400, 350, 600), 0.9), Det((650, 400, 750, 600), 0.7)]
            for i in range(4)}
    ranked = pipeline.localize(cands, dets, settings())
    take = pipeline.diverse(ranked, 4, set())
    keys = sorted(c.key for c in take)
    # Two best-per-frame primaries plus two off-path extras from different frames —
    # not four views of the primary and none of the other bird.
    primaries = [k for k in keys if k.endswith("#0")]
    extras = [k for k in keys if k.endswith("#1")]
    assert len(primaries) == 2 and len(extras) == 2
    assert len({k.split("#")[0] for k in extras}) == 2


def test_diverse_without_extras_takes_one_per_frame_then_fills():
    cands = [frame("clip@1.00s"), frame("clip@2.00s")]
    dets = {0: [Det((250, 400, 350, 600), 0.9), Det((260, 405, 355, 610), 0.85)],  # same bird twice
            1: [Det((250, 400, 350, 600), 0.8)]}
    ranked = pipeline.localize(cands, dets, settings())
    take = pipeline.diverse(ranked, 3, set())
    assert [c.key for c in take] == ["clip@1.00s#0", "clip@2.00s#0", "clip@1.00s#1"]
    # Already-used keys are skipped and nothing is returned when everything is used.
    assert pipeline.diverse(ranked, 3, {c.key for c in take}) == []


def test_diverse_never_starves_primary_when_limit_is_tiny():
    cands = [frame("clip@1.00s")]
    dets = {0: [Det((250, 400, 350, 600), 0.9), Det((650, 400, 750, 600), 0.7)]}
    ranked = pipeline.localize(cands, dets, settings())
    assert [c.key for c in pipeline.diverse(ranked, 2, set())] == ["clip@1.00s#0", "clip@1.00s#1"]
    assert [c.key for c in pipeline.diverse(ranked, 1, set())] == ["clip@1.00s#0"]
