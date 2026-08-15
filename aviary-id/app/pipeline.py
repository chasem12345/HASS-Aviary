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
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from PIL import Image

from . import frames
from .detector import Detection
from .model import Classifier
from .settings import Settings

log = logging.getLogger("aviary_id.pipeline")

# One ranked crop: (rank, image, score, origin).
Ranked = tuple[float, Image.Image, float, str]


def localize(
    candidates: list[frames.Candidate],
    detections: dict[int, list[Detection]],
    settings: Settings,
) -> list[Ranked]:
    """Turn candidates into ranked crops.

    Ranked by ``score * sqrt(area)``: confidence alone would favour a tiny, perfectly
    recognised bird over a large clear one, and area alone would favour a big blurry blob.

    Pre-cropped candidates — Frigate's thumbnail, or its snapshot cropped to Frigate's own
    box — skip the detector entirely and rank on their own score. They are already the crop
    we were trying to produce, and running a COCO model over a tight crop mostly finds
    nothing.
    """
    ranked: list[Ranked] = []
    for i, cand in enumerate(candidates):
        if cand.pre_cropped:
            area = cand.image.width * cand.image.height
            ranked.append((cand.score * (area ** 0.5), cand.image, cand.score, cand.origin))
            continue
        for det in detections.get(i, []):
            ranked.append((
                det.score * (det.area ** 0.5),
                frames.crop_box(cand.image, det.box, settings.crop_padding),
                det.score,
                cand.origin,
            ))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def diverse(ranked: list[Ranked], limit: int, already: set[str]) -> list[Ranked]:
    """Up to ``limit`` crops, preferring one per source frame.

    Three boxes from the same frame is three views of one pose, which defeats the point of
    fusing frames at all. Take the best per origin first, then fill any remaining slots.
    """
    picked: list[Ranked] = []
    seen: set[str] = set()
    for item in ranked:
        if item[3] in already or item[3] in seen:
            continue
        seen.add(item[3])
        picked.append(item)
        if len(picked) >= limit:
            return picked
    for item in ranked:
        if item[3] in already or any(item is p for p in picked):
            continue
        picked.append(item)
        if len(picked) >= limit:
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
    ) -> tuple[Optional[object], list[Ranked], int]:
        """Classify, escalating while uncertain.

        Returns (result, crops used, rounds). ``result`` is None when nothing anywhere in
        the event looked like a bird — deliberately NOT falling back to classifying the
        whole uncropped frame, which on a 1080p frame leaves a feeder-distance bird about
        ten pixels across and only ever produced confidently-wrong answers.
        """
        loop = asyncio.get_running_loop()
        used: set[str] = set()
        crops: list[Ranked] = []
        result = None
        rounds = 0
        detected_from = 0
        escalated = False

        detections = await self._detect(media.candidates, detected_from)
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
                # Nothing left in hand. Spend one extraction pass, at timestamps
                # interleaved with the first so the new frames are genuinely different
                # views — then stop. Bounded on purpose: a bird that cannot be identified
                # should cost a few seconds, not an unbounded hunt.
                if escalated or not media.clip_path:
                    break
                escalated = True
                added = await media.add_clip_frames(
                    self.settings.sample_frames, phase=0.0, timings=timings)
                if not added:
                    break
                detections = await self._detect(media.candidates, detected_from)
                detected_from = len(media.candidates)
                ranked = localize(media.candidates, detections, self.settings)
                release_vram()
                continue

            rounds += 1
            used.update(item[3] for item in take)
            crops.extend(take)

            t0 = loop.time()
            result = await asyncio.to_thread(
                self.classifier.classify,
                [c[1] for c in crops], [c[2] for c in crops], [c[3] for c in crops],
                priors, exclude,
            )
            timings.add(f"classify{rounds}", loop.time() - t0)

            if confident(result, min_score, min_margin):
                break

        return result, crops, rounds
