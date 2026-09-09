"""Turn an event's candidate images into one identification, escalating only when unsure.

Effort is spent in proportion to how hard the bird is. Most events are easy and finish on
the first rung; the uncertain ones are exactly where looking harder pays off. Three rungs,
stopping as soon as the answer is confident:

1. Classify the best crops, one per source frame where possible.
2. Still unsure -> classify crops the detector already found but we did not use. One more
   forward pass and *zero* extra I/O: no ffmpeg, no downloads.
3. Still unsure -> extract another pass of clip frames at timestamps interleaved with the
   first, so they are genuinely new views rather than neighbours of frames already seen.

The confidence thresholds come from the caller (Aviary), not from here. This service does
not decide whether an answer is good enough — it uses the caller's thresholds only to
decide whether to try harder, and still returns raw numbers for the caller to gate on. One
source of truth, and retuning the add-on retunes when it works harder with no redeploy.

Crops carry their geometry (where in the frame, how far from the tracked path, which
clip timestamp) all the way to the classifier, which uses it to tell the event's bird
from any other bird in view — see subjects.py.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from PIL import Image

from . import frames
from .settings import Settings

if TYPE_CHECKING:  # type-only: keeps this module importable (and testable) without torch
    from .detector import Detection
    from .model import Classifier

log = logging.getLogger("aviary_id.pipeline")

# Anchor matching: a detector box whose bottom-center sits within this normalized
# distance of the tracked path is "the event's bird"; anything farther is probably a
# DIFFERENT bird sharing the frame. Ranking cuts its priority (deprioritized, never
# dropped, because the path is sparse and coarse and must not discard the only bird
# found); the subject partition later bars it from the primary outright.
_ANCHOR_RADIUS = 0.15
_ANCHOR_PENALTY = 0.2


@dataclass
class Crop:
    """One candidate crop for the classifier, with the geometry that says which bird it is."""
    rank: float
    image: Image.Image
    score: float            # detector score (Frigate's own score for a pre-cropped one)
    origin: str             # "thumbnail" | "snapshot+box" | "snapshot" | "clip@1.75s" | upload
    key: str                # stable identity within one localize() pass
    pre_cropped: bool
    center: Optional[tuple[float, float]] = None   # box bottom-center, normalized to the frame
    anchor_dist: Optional[float] = None            # distance to the tracked path; None = no anchor
    t: Optional[float] = None                      # clip offset seconds; None for non-clip crops


def _clip_offset(origin: str) -> Optional[float]:
    if origin.startswith("clip@") and origin.endswith("s"):
        try:
            return float(origin[5:-1])
        except ValueError:
            return None
    return None


def _box_center(cand: "frames.Candidate", det: Detection) -> tuple[float, float]:
    """Bottom-center, normalized — the same reference point Frigate's path_data uses."""
    x1, _, x2, y2 = det.box
    return (((x1 + x2) / 2) / max(1, cand.image.width), y2 / max(1, cand.image.height))


def _anchor_distance(cand: "frames.Candidate", det: Detection) -> Optional[float]:
    """Distance from the box to the tracked bird's position, or None without an anchor."""
    if cand.anchor is None:
        return None
    bx, by = _box_center(cand, det)
    ax, ay = cand.anchor
    return ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5


def _anchor_factor(dist: Optional[float]) -> float:
    """1.0 when the box matches the tracked bird's position (or there is no anchor)."""
    if dist is None:
        return 1.0
    return 1.0 if dist <= _ANCHOR_RADIUS else _ANCHOR_PENALTY


def localize(
    candidates: list[frames.Candidate],
    detections: dict[int, list[Detection]],
    settings: Settings,
) -> list[Crop]:
    """Turn candidates into ranked crops.

    Ranked by ``score * sqrt(area)``: confidence alone would favour a tiny, perfectly
    recognised bird over a large clear one, and area alone would favour a big blurry blob.
    Clip-frame boxes are additionally weighted by whether they sit on the tracked
    object's path — with two birds in frame, the detector finds both, and the anchor is
    Frigate's own answer to which one THIS event is about.

    Pre-cropped candidates — Frigate's thumbnail, or its snapshot cropped to Frigate's own
    box — skip the detector entirely and rank on their own score. They are already the crop
    we were trying to produce, and running a COCO model over a tight crop mostly finds
    nothing.
    """
    ranked: list[Crop] = []
    for i, cand in enumerate(candidates):
        if cand.pre_cropped:
            area = cand.image.width * cand.image.height
            ranked.append(Crop(
                rank=cand.score * (area ** 0.5), image=cand.image, score=cand.score,
                origin=cand.origin, key=cand.origin, pre_cropped=True,
            ))
            continue
        for k, det in enumerate(detections.get(i, [])):
            dist = _anchor_distance(cand, det)
            ranked.append(Crop(
                rank=det.score * (det.area ** 0.5) * _anchor_factor(dist),
                image=frames.crop_box(cand.image, det.box, settings.crop_padding),
                score=det.score,
                origin=cand.origin,
                key=f"{cand.origin}#{k}",
                pre_cropped=False,
                center=_box_center(cand, det),
                anchor_dist=dist,
                t=_clip_offset(cand.origin),
            ))
    ranked.sort(key=lambda c: c.rank, reverse=True)
    return ranked


def _distinct(a: Crop, b: Crop) -> bool:
    """Two boxes from one frame far enough apart to be two birds, not one box twice."""
    if a.center is None or b.center is None:
        return False
    d = ((a.center[0] - b.center[0]) ** 2 + (a.center[1] - b.center[1]) ** 2) ** 0.5
    return d > _ANCHOR_RADIUS


def diverse(ranked: list[Crop], limit: int, already: set[str]) -> list[Crop]:
    """Up to ``limit`` crops: one per source frame first, with room kept for a second bird.

    Three boxes from the same frame is three views of one pose, which defeats the point of
    fusing frames at all — so the first pass takes the best box per frame. But a frame
    whose second-best box sits clearly APART from its best is a frame with two birds in
    it, and the other bird deserves crops of its own or it can never be classified. Up to
    two such "extras" (from different frames) are reserved per take, never at the cost of
    the primary's first two slots. Any remaining room is filled best-first.
    """
    picked: list[Crop] = []
    picked_keys: set[str] = set()

    def take(crop: Crop) -> bool:
        if crop.key in already or crop.key in picked_keys:
            return False
        picked.append(crop)
        picked_keys.add(crop.key)
        return len(picked) >= limit

    # Best per origin, and the spatially distinct extras per origin, in rank order.
    best_by_origin: dict[str, Crop] = {}
    extras: list[Crop] = []
    for crop in ranked:
        if crop.key in already:
            continue
        first = best_by_origin.get(crop.origin)
        if first is None:
            best_by_origin[crop.origin] = crop
        elif _distinct(first, crop) and not any(e.origin == crop.origin for e in extras):
            extras.append(crop)

    reserve = min(2, len(extras), max(0, limit - 2))
    for crop in best_by_origin.values():
        if len(picked) >= limit - reserve:
            break
        if take(crop):
            return picked
    for crop in extras[:reserve]:
        if take(crop):
            return picked
    for crop in ranked:
        if take(crop):
            break
    return picked


def confident(result, min_score: float, min_margin: float) -> bool:
    return bool(result) and result.score >= min_score and result.margin >= min_margin


class Pipeline:
    """Holds the models; one instance for the life of the service."""

    def __init__(self, classifier: Classifier, detector, settings: Settings):
        self.classifier = classifier
        self.detector = detector
        self.settings = settings

    async def _detect(self, candidates: list[frames.Candidate],
                      first: int) -> dict[int, list[Detection]]:
        """Detect over candidates from index ``first`` onward that actually need it."""
        todo = [(i, c) for i, c in enumerate(candidates) if i >= first and not c.pre_cropped]
        if not todo:
            return {}
        found = await asyncio.to_thread(
            self.detector.detect, [c.image for _, c in todo], self.settings.detector_batch
        )
        return {i: boxes for (i, _), boxes in zip(todo, found)}

    async def run(
        self,
        media: frames.EventMedia,
        priors: dict[str, float],
        exclude: Optional[list[str]],
        min_score: float,
        min_margin: float,
        timings: frames.Timings,
        release_vram,
    ) -> tuple[Optional[object], list[Crop], int]:
        """Classify, escalating while uncertain.

        Returns (result, crops used, rounds). ``result`` is the PRIMARY subject's answer
        (with any other birds attached as ``result.others``) and is None when nothing
        anywhere in the event looked like a bird — deliberately NOT falling back to
        classifying the whole uncropped frame, which on a 1080p frame leaves a
        feeder-distance bird about ten pixels across and only ever produced
        confidently-wrong answers. Escalation is gated on the primary alone.
        """
        loop = asyncio.get_running_loop()
        used: set[str] = set()
        crops: list[Crop] = []
        result = None
        rounds = 0
        detected_from = 0
        escalated = False

        def clip_hit(dets: dict[int, list[Detection]]) -> bool:
            """Whether any CLIP frame produced a detection.

            Clip frames only, not the boxless snapshot: with zoom active the snapshot is
            the wide camera, and a bird found there says nothing about whether the
            zoomed footage has one — which is exactly what the fallback swap gates on.
            """
            return any(boxes and media.candidates[i].origin.startswith("clip@")
                       for i, boxes in dets.items())

        detections = await self._detect(media.candidates, detected_from)
        clip_bird_seen = clip_hit(detections)
        detected_from = len(media.candidates)
        ranked = localize(media.candidates, detections, self.settings)
        release_vram()

        while True:
            # Cap the take so the ceiling is exact rather than overshot by up to a full
            # round — max_frames is a promise about GPU work per event, not a suggestion.
            room = self.settings.max_frames - len(crops)
            if room <= 0:
                log.debug("Stopping at the %d-frame ceiling for %s.",
                          self.settings.max_frames, media.event_id)
                break
            take = diverse(ranked, min(self.settings.classify_frames, room), used)
            if not take:
                # Nothing left in hand. Two bounded ways to find more: one extraction
                # pass on the current clip at timestamps interleaved with the first, so
                # the new frames are genuinely different views — and, when a ZOOMED clip
                # never contained a detectable bird at all (the PTZ was travelling,
                # blocked, or parked on another zone), one swap to the event's own clip,
                # which zoom deliberately skipped. Bounded on purpose: a bird that
                # cannot be identified should cost a few seconds, not an unbounded hunt.
                added = 0
                if not escalated and media.clip_path:
                    escalated = True
                    added = await media.add_clip_frames(
                        self.settings.sample_frames, phase=0.0, timings=timings)
                if not added and media.zoom_used and not clip_bird_seen:
                    if await media.swap_to_event_clip(timings):
                        # A fresh clip starts its sampling over and earns its own
                        # interleave pass if this one comes up short too.
                        escalated = False
                        added = await media.add_clip_frames(
                            self.settings.sample_frames, phase=0.5, timings=timings)
                if not added:
                    break
                detections = await self._detect(media.candidates, detected_from)
                clip_bird_seen = clip_bird_seen or clip_hit(detections)
                detected_from = len(media.candidates)
                ranked = localize(media.candidates, detections, self.settings)
                release_vram()
                continue

            rounds += 1
            used.update(c.key for c in take)
            crops.extend(take)

            t0 = loop.time()
            result = await asyncio.to_thread(
                self.classifier.classify, crops, priors, exclude,
            )
            timings.add(f"classify{rounds}", loop.time() - t0)

            if confident(result, min_score, min_margin):
                break

        return result, crops, rounds
