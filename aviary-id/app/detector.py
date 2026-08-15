"""Locate birds in a frame so the classifier sees a crop instead of a wide shot.

This step matters more than it looks. BioCLIP resizes its input to 224x224; a sparrow
occupying 3% of a 1080p frame becomes about forty usable pixels, and the classifier is
then guessing from the background. Cropping to the bird is most of the difference between
"works" and "doesn't".

Two backends, selected by DETECTOR_BACKEND:

* ``yolo`` (default) — Ultralytics YOLO11n. Measurably better at small, shaded and
  partially-occluded birds, which is exactly what clip frames contain; it runs at 640px
  where the Faster R-CNN alternative runs at 320. Ultralytics is AGPL-3.0 — acceptable
  here and called out in the README; if that license doesn't work for your deployment,
  set DETECTOR_BACKEND=frcnn and nothing else changes.
* ``frcnn`` — torchvision's COCO Faster R-CNN (BSD-3, zero extra dependencies). The
  original backend, kept as the permissively-licensed fallback.

Both expose the same ``detect()`` contract; nothing outside this module knows which one
is running.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import torch
from PIL import Image

from .settings import Settings

log = logging.getLogger("aviary_id.detector")


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in source pixels
    score: float

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.box
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)


class YoloBirdDetector:
    """YOLO11n, filtered to the COCO bird class."""

    def __init__(self, device: torch.device, threshold: float, cache_dir: str):
        # Imported here rather than at module top so the frcnn backend works even if the
        # (AGPL) ultralytics package were removed from the image.
        from ultralytics import YOLO

        self.threshold = threshold
        # Ultralytics takes a device string per predict() call rather than moving a
        # module; str() of a torch.device ("cpu", "cuda") is a form it accepts.
        self.device = device
        self._device_str = str(device)

        # Keep the weights on the model volume so a container rebuild doesn't
        # re-download them, same as the CLIP weights and the text-embedding cache.
        path = os.path.join(cache_dir, "yolo11n.pt")
        if not os.path.exists(path):
            log.info("Downloading YOLO11n weights to %s ...", path)
            try:
                # Internal ultralytics util — the only way to control WHERE the asset
                # lands. Guarded because it is not public API and could move.
                from ultralytics.utils.downloads import attempt_download_asset
                attempt_download_asset(path)
            except (ImportError, AttributeError, TypeError) as exc:
                log.warning(
                    "Could not download to the cache dir (%s); falling back to "
                    "ultralytics' default location — the weights will re-download "
                    "after a container rebuild.", exc,
                )
                path = "yolo11n.pt"
        self.model = YOLO(path)

        # Read the class index off the model rather than hard-coding COCO's "bird is 14":
        # a silently wrong constant would mean detecting the wrong class forever.
        names = self.model.names
        try:
            self.bird_index = next(i for i, n in names.items() if n == "bird")
        except StopIteration as exc:
            raise RuntimeError(
                "The YOLO weights have no 'bird' class; cannot localize birds."
            ) from exc
        log.info("YOLO11n detector ready on %s (bird class index %d).",
                 self._device_str, self.bird_index)

    @torch.inference_mode()
    def detect(self, images: list[Image.Image], batch_size: int = 2) -> list[list[Detection]]:
        """Bird boxes per input image, in ORIGINAL image coordinates, best-scoring first.

        Chunked for the same reason as the frcnn backend: the peak, not the total, is what
        OOMs a shared 4 GB card.
        """
        if not images:
            return []

        results: list[list[Detection]] = []
        for start in range(0, len(images), max(1, batch_size)):
            chunk = images[start:start + max(1, batch_size)]
            outputs = self.model.predict(
                chunk,
                conf=self.threshold,
                classes=[self.bird_index],
                device=self._device_str,
                verbose=False,
            )
            for out in outputs:
                found = [
                    Detection(box=(b[0], b[1], b[2], b[3]), score=float(s))
                    for b, s in zip(out.boxes.xyxy.tolist(), out.boxes.conf.tolist())
                ]
                found.sort(key=lambda d: d.score, reverse=True)
                results.append(found)
            if self.device.type == "cuda":
                # Hand cached blocks back between chunks; the card is likely shared.
                torch.cuda.empty_cache()
        return results


class FrcnnBirdDetector:
    """torchvision COCO Faster R-CNN — the permissively-licensed (BSD-3) fallback."""

    def __init__(self, device: torch.device, threshold: float):
        from torchvision.models.detection import (
            FasterRCNN_MobileNet_V3_Large_320_FPN_Weights,
            fasterrcnn_mobilenet_v3_large_320_fpn,
        )

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
        log.info("Faster R-CNN detector ready on %s (bird class index %d).",
                 device, self.bird_index)

    def _resized(self, image: Image.Image) -> tuple[Image.Image, float]:
        """Shrink to the size the model would resize to anyway. Returns (image, scale).

        This matters far more than it looks. torchvision's detection transform normalizes
        the image tensor at its ORIGINAL resolution and only then resizes — so handing it a
        1920x1080 frame allocates ~24 MB per frame for the input, doubles it in normalize,
        and does it for every frame in the batch, all to produce a 569x320 tensor. On a 4 GB
        card shared with other workloads that is the difference between working and OOM.

        Doing the resize first costs nothing in accuracy: the model was going to perform
        exactly this scaling internally. Boxes come back in resized coordinates and are
        scaled up by the caller.
        """
        min_size = self.model.transform.min_size[0]
        max_size = self.model.transform.max_size
        w, h = image.size
        scale = min(min_size / min(w, h), max_size / max(w, h))
        if scale >= 1.0:
            return image, 1.0
        return image.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                            Image.BILINEAR), scale

    @torch.inference_mode()
    def detect(self, images: list[Image.Image], batch_size: int = 2) -> list[list[Detection]]:
        """Bird boxes per input image, in ORIGINAL image coordinates, best-scoring first.

        Chunked rather than one big batch: the whole candidate set at once was the other
        half of the memory problem, and the detector is fast enough that sequential costs
        milliseconds.
        """
        if not images:
            return []

        results: list[list[Detection]] = []
        for start in range(0, len(images), max(1, batch_size)):
            chunk = images[start:start + max(1, batch_size)]
            prepared = [self._resized(img) for img in chunk]
            batch = [self._preprocess(img).to(self.device) for img, _ in prepared]
            try:
                outputs = self.model(batch)
            finally:
                del batch
            results.extend(
                self._extract(output, scale) for output, (_, scale) in zip(outputs, prepared)
            )
            del outputs
            if self.device.type == "cuda":
                # Hand the block back between chunks. The card is likely shared, and
                # holding a chunk's activations while decoding the next one is exactly the
                # peak we are trying to avoid.
                torch.cuda.empty_cache()
        return results

    def _extract(self, output: dict, scale: float) -> list[Detection]:
        keep = (output["labels"] == self.bird_index) & (output["scores"] >= self.threshold)
        # Undo the pre-resize so boxes address the full-resolution frame the caller crops.
        inv = 1.0 / scale if scale else 1.0
        boxes = (output["boxes"][keep] * inv).tolist()
        scores = output["scores"][keep].tolist()
        found = [
            Detection(box=(b[0], b[1], b[2], b[3]), score=float(s))
            for b, s in zip(boxes, scores)
        ]
        found.sort(key=lambda d: d.score, reverse=True)
        return found


def make_detector(device: torch.device, settings: Settings):
    """Build the configured backend. Both share the detect() contract."""
    if settings.detector_backend == "frcnn":
        return FrcnnBirdDetector(device, settings.detector_threshold)
    return YoloBirdDetector(device, settings.detector_threshold, settings.cache_dir)
