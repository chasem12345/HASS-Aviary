"""Locate birds in a frame so the classifier sees a crop instead of a wide shot.

This step matters more than it looks. BioCLIP resizes its input to 224x224; a sparrow
occupying 3% of a 1080p frame becomes about forty usable pixels, and the classifier is
then guessing from the background. Cropping to the bird is most of the difference between
"works" and "doesn't".

torchvision's COCO-pretrained Faster R-CNN is used rather than a YOLO model purely for
licensing: torchvision is BSD-3 and already a hard dependency, whereas Ultralytics is
AGPL-3.0, which is a real consideration for code published in a public repo. YOLO11n is
somewhat better at small objects — if AGPL is acceptable for your use, this is the one
class to replace, and nothing outside this module needs to change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import torch
from PIL import Image
from torchvision.models.detection import (
    FasterRCNN_MobileNet_V3_Large_320_FPN_Weights,
    fasterrcnn_mobilenet_v3_large_320_fpn,
)

log = logging.getLogger("aviary_id.detector")


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in source pixels
    score: float

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.box
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)


class BirdDetector:
    def __init__(self, device: torch.device, threshold: float):
        self.device = device
        self.threshold = threshold
        weights = FasterRCNN_MobileNet_V3_Large_320_FPN_Weights.DEFAULT
        self.model = fasterrcnn_mobilenet_v3_large_320_fpn(weights=weights)
        self.model.eval().to(device)
        self._preprocess = weights.transforms()

        # Read the label index off the weights metadata rather than hard-coding COCO's
        # "bird is 16". The index has shifted between torchvision releases and a silently
        # wrong constant would mean detecting the wrong class forever.
        categories = weights.meta["categories"]
        try:
            self.bird_index = categories.index("bird")
        except ValueError as exc:
            raise RuntimeError(
                "The detection weights have no 'bird' category; cannot localize birds."
            ) from exc
        log.info("Detector ready on %s (bird class index %d).", device, self.bird_index)

    @torch.inference_mode()
    def detect(self, images: list[Image.Image]) -> list[list[Detection]]:
        """Bird boxes per input image, ordered best-scoring first."""
        if not images:
            return []
        batch = [self._preprocess(img).to(self.device) for img in images]
        outputs = self.model(batch)

        results: list[list[Detection]] = []
        for output in outputs:
            keep = (output["labels"] == self.bird_index) & (output["scores"] >= self.threshold)
            boxes = output["boxes"][keep].tolist()
            scores = output["scores"][keep].tolist()
            found = [
                Detection(box=(b[0], b[1], b[2], b[3]), score=float(s))
                for b, s in zip(boxes, scores)
            ]
            found.sort(key=lambda d: d.score, reverse=True)
            results.append(found)
        return results
