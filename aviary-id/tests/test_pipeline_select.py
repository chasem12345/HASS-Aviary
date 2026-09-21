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
    assert on.frame_boxes == 2 and off.frame_boxes == 2 and thumb.frame_boxes == 1


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
    cands = [frame("clip@1.00s"), frame("clip@2.00s"), frame("clip@3.00s")]
    dets = {0: [Det((250, 400, 350, 600), 0.9), Det((500, 400, 600, 600), 0.85)],  # two boxes, near
            1: [Det((250, 400, 350, 600), 0.8)],
            2: [Det((250, 400, 350, 600), 0.7)]}
    ranked = pipeline.localize(cands, dets, settings())
    take = pipeline.diverse(ranked, 3, set())
    # (0.55, 0.6) is within the anchor radius of (0.3, 0.6)? No: 0.25 apart — a distinct
    # extra, reserved after the two best-per-frame slots at limit 3 (reserve = 1).
    assert [c.key for c in take] == ["clip@1.00s#0", "clip@2.00s#0", "clip@1.00s#1"]
    # Already-used keys are skipped and nothing is returned when everything is used.
    assert pipeline.diverse(ranked, 4, {c.key for c in ranked}) == []


def test_localize_dedupes_one_bird_boxed_twice_and_counts_frame_boxes():
    cands = [frame("clip@1.00s"), frame("clip@2.00s")]
    dets = {0: [Det((250, 400, 350, 600), 0.9), Det((260, 405, 355, 610), 0.85),  # same bird twice
                Det((650, 400, 750, 600), 0.7)],                                   # another bird
            1: [Det((250, 400, 350, 600), 0.8), Det((280, 470, 340, 590), 0.6)]}  # whole bird + head
    ranked = pipeline.localize(cands, dets, settings())
    keys = sorted(c.key for c in ranked)
    assert keys == ["clip@1.00s#0", "clip@1.00s#1", "clip@2.00s#0"]
    by_key = {c.key: c for c in ranked}
    assert by_key["clip@1.00s#0"].frame_boxes == 2 and by_key["clip@1.00s#1"].frame_boxes == 2
    assert by_key["clip@1.00s#1"].center == (0.7, 0.6)      # the survivor is the other bird
    assert by_key["clip@2.00s#0"].frame_boxes == 1 and by_key["clip@2.00s#0"].score == 0.8
    assert all(not c.zoomed for c in ranked)


def test_localize_carries_zoomed_flag():
    cand = frames.Candidate(image=Image.new("RGB", (1000, 1000)), origin="clip@1.00s",
                            zoomed=True)
    ranked = pipeline.localize([cand], {0: [Det((250, 400, 350, 600), 0.9)]}, settings())
    assert ranked[0].zoomed and ranked[0].frame_boxes == 1 and ranked[0].anchor_dist is None


def test_diverse_takes_frigate_crops_first_whatever_their_rank():
    """The 175 px thumbnail ranks below every clip crop on area alone; it is the seed
    the partition anchors the tracked bird on and must reach the classifier."""
    cands = [frames.Candidate(image=Image.new("RGB", (175, 175)), origin="thumbnail",
                              pre_cropped=True, score=0.9)]
    cands += [frame(f"clip@{k}.00s") for k in range(1, 7)]
    dets = {i: [Det((100, 100, 900, 900), 0.95)] for i in range(1, 7)}
    ranked = pipeline.localize(cands, dets, settings())
    assert ranked[-1].origin == "thumbnail"                 # last by rank...
    take = pipeline.diverse(ranked, 4, set())
    assert take[0].origin == "thumbnail" and len(take) == 4  # ...first into the take


def test_pre_cropped_only_needs_no_detector():
    """Confirm mode: Frigate's two crops flow through selection with the detector never
    consulted — ``_detect`` has nothing to do, ``localize`` ranks by area, ``diverse``
    takes both."""
    import asyncio

    class Boom:
        def detect(self, *a, **k):
            raise AssertionError("detector must not run on pre-cropped candidates")

    cands = [frames.Candidate(image=Image.new("RGB", (96, 96)), origin="thumbnail",
                              pre_cropped=True, score=0.9),
             frames.Candidate(image=Image.new("RGB", (220, 200)), origin="snapshot+box",
                              pre_cropped=True, score=0.9)]
    p = pipeline.Pipeline(classifier=None, detector=Boom(), settings=settings())
    assert asyncio.run(p._detect(cands, 0)) == {}  # noqa: SLF001
    ranked = pipeline.localize(cands, {}, settings())
    assert [c.origin for c in ranked] == ["snapshot+box", "thumbnail"]  # bigger first
    assert all(c.pre_cropped and c.anchor_dist is None for c in ranked)
    assert sorted(c.key for c in pipeline.diverse(ranked, 4, set())) == \
        sorted(c.key for c in ranked)


def test_diverse_never_starves_primary_when_limit_is_tiny():
    cands = [frame("clip@1.00s")]
    dets = {0: [Det((250, 400, 350, 600), 0.9), Det((650, 400, 750, 600), 0.7)]}
    ranked = pipeline.localize(cands, dets, settings())
    assert [c.key for c in pipeline.diverse(ranked, 2, set())] == ["clip@1.00s#0", "clip@1.00s#1"]
    assert [c.key for c in pipeline.diverse(ranked, 1, set())] == ["clip@1.00s#0"]
