"""aviary-id: a stateless bird identification service for Frigate events.

Takes a Frigate event id, pulls that event's media from Frigate itself, finds the bird,
and returns a species. It holds no database and knows nothing about Home Assistant — the
Aviary add-on owns queueing, thresholds, retries and storage. This service only has to
answer one question well.

Deliberately, it does NOT decide whether a result is good enough to act on: it returns the
score and the top-1/top-2 margin and lets the caller apply its own thresholds. Splitting
the judgement from the measurement means you can retune Aviary's gates without
redeploying the GPU container.
"""

from __future__ import annotations

import asyncio
import hmac
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import torch
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel, Field

from . import frames
from .detector import BirdDetector, Detection
from .model import Classifier
from .settings import Settings, load_settings
from .species import load_species

log = logging.getLogger("aviary_id")

settings: Settings = load_settings()

# One lock around all GPU work. The card has 4 GB and one job at a time saturates it;
# concurrency is the caller's queue, not ours. Keeping the lock here rather than relying
# on the caller behaving means a stray parallel request degrades to waiting, not OOM.
_gpu_lock = asyncio.Lock()

_classifier: Optional[Classifier] = None
_detector: Optional[BirdDetector] = None
_client: Optional[httpx.AsyncClient] = None
_species_source = "not loaded"
_ready = False


# ------------------------------------------------------------------------------ models

class IdentifyRequest(BaseModel):
    event_id: str
    # Per-request override, so one service can serve more than one Frigate instance.
    frigate_url: Optional[str] = None
    # {species name: likelihood multiplier}. Aviary populates this from BirdNET-Go audio
    # detections near the same timestamp; 3.0 means "treat as 3x more likely a priori".
    priors: dict[str, float] = Field(default_factory=dict)
    # Species to rule out entirely for this call — answers the user has already rejected
    # for this detection, and any species they never want suggested. Common or scientific
    # names, matched case-insensitively.
    exclude: list[str] = Field(default_factory=list)


class FrameOut(BaseModel):
    origin: str
    det_score: float
    top1: str
    top1_score: float
    top2: str
    top2_score: float


class IdentifyResponse(BaseModel):
    status: str  # ok | no_media | no_bird | not_ready
    common_name: Optional[str] = None
    scientific_name: Optional[str] = None
    species_code: Optional[str] = None
    score: Optional[float] = None
    margin: Optional[float] = None
    runner_up: Optional[str] = None
    # False when the detector found no bird and we classified the frame uncropped —
    # the result is usable but materially less trustworthy, and the caller should know.
    localized: bool = True
    frames_used: int = 0
    # How many species were ruled out for this call (rejected answers, or species the
    # user never wants suggested). Lets the caller tell a fresh answer from a reroll.
    excluded: int = 0
    per_frame: list[FrameOut] = Field(default_factory=list)
    embedding: Optional[str] = None
    model_version: Optional[str] = None
    elapsed_ms: int = 0


# -------------------------------------------------------------------------------- auth

async def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """Bearer-token check. A blank AVIARY_ID_TOKEN disables it entirely."""
    if not settings.auth_token:
        return
    expected = f"Bearer {settings.auth_token}"
    # Constant-time compare: this endpoint is reachable from another host, and a naive
    # == on a secret is a bad habit to leave in code even at low stakes.
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


# ---------------------------------------------------------------------------- pipeline

def _select_crops(
    candidates: list[frames.Candidate],
    detections: list[list[Detection]],
) -> tuple[list[Image.Image], list[float], list[str], bool]:
    """Pick the best crops across all candidate frames.

    Ranked by ``det_score * sqrt(area)``: confidence alone would favour a tiny, perfectly
    recognised bird over a large clear one, and area alone would favour a big blurry blob.
    """
    scored = []
    for candidate, found in zip(candidates, detections):
        for det in found:
            scored.append((det.score * (det.area ** 0.5), candidate, det))
    scored.sort(key=lambda item: item[0], reverse=True)

    if scored:
        chosen = scored[:settings.classify_frames]
        crops = [frames.crop_box(c.image, d.box, settings.crop_padding) for _, c, d in chosen]
        return crops, [d.score for _, _, d in chosen], [c.origin for _, c, _ in chosen], True

    # Frigate already decided there is a bird here; our detector just couldn't find it
    # (too small, occluded, or an odd pose). Classifying the uncropped frame is worse than
    # a good crop but much better than returning nothing, so long as we say so.
    log.debug("No bird localized; falling back to uncropped frames.")
    fallback = candidates[:settings.classify_frames]
    return (
        [c.image for c in fallback],
        [0.1] * len(fallback),
        [f"{c.origin} (uncropped)" for c in fallback],
        False,
    )


def _run_inference(crops, det_scores, origins, priors, exclude):
    """Detector + classifier work, run off the event loop by the caller."""
    return _classifier.classify(crops, det_scores, origins, priors, exclude)


async def _identify_images(
    candidates: list[frames.Candidate],
    priors: dict[str, float],
    started: float,
    exclude: Optional[list[str]] = None,
) -> IdentifyResponse:
    if not candidates:
        return IdentifyResponse(status="no_media", elapsed_ms=_ms(started))

    async with _gpu_lock:
        # to_thread because torch inference is blocking C code — without it a long
        # classification would stall /healthz and every other request on the loop.
        detections = await asyncio.to_thread(
            _detector.detect, [c.image for c in candidates]
        )
        crops, det_scores, origins, localized = _select_crops(candidates, detections)
        result = await asyncio.to_thread(
            _run_inference, crops, det_scores, origins, priors, exclude
        )

    if result is None:
        return IdentifyResponse(status="no_bird", elapsed_ms=_ms(started))

    return IdentifyResponse(
        status="ok",
        common_name=result.species.com_name,
        scientific_name=result.species.sci_name,
        species_code=result.species.species_code,
        score=round(result.score, 4),
        margin=round(result.margin, 4),
        runner_up=result.runner_up.com_name if result.runner_up else None,
        localized=localized,
        frames_used=len(crops),
        excluded=result.excluded,
        per_frame=[FrameOut(**vars(f)) for f in result.per_frame],
        embedding=result.embedding,
        model_version=_classifier.model_version,
        elapsed_ms=_ms(started),
    )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# ------------------------------------------------------------------------------ routes

@asynccontextmanager
async def lifespan(_: FastAPI):
    global _classifier, _detector, _client, _species_source, _ready

    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    os.makedirs(settings.cache_dir, exist_ok=True)

    _client = httpx.AsyncClient(timeout=settings.fetch_timeout, follow_redirects=True)

    species, _species_source = await load_species(settings)

    def build():
        classifier = Classifier(settings)
        classifier.set_species(species)
        return classifier, BirdDetector(classifier.device, settings.detector_threshold)

    # Blocking: weights load plus (on a cold cache) tens of thousands of text encodes.
    # Off the loop so /healthz answers "ok: false" during startup instead of hanging,
    # which is what lets Aviary show "still loading" rather than "unreachable".
    _classifier, _detector = await asyncio.to_thread(build)
    _ready = True

    if not settings.frigate_url:
        log.warning(
            "FRIGATE_URL is not set; every /identify call must supply frigate_url itself."
        )
    log.info("aviary-id ready.")
    try:
        yield
    finally:
        _ready = False
        if _client is not None:
            await _client.aclose()


app = FastAPI(title="aviary-id", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness plus the facts you actually need when something looks wrong.

    Intentionally unauthenticated so a container healthcheck and Aviary's status pill work
    without provisioning the token; it exposes no secrets.
    """
    return {
        "ok": _ready,
        "cuda": torch.cuda.is_available(),
        "device": _classifier.device_name() if _classifier else "not loaded",
        "cpu_only": settings.cpu_only,
        "model": settings.model_name,
        "model_version": _classifier.model_version if _classifier else None,
        "species_count": len(_classifier.species) if _classifier else 0,
        "species_source": _species_source,
        "frigate_url": settings.frigate_url or None,
    }


@app.get("/species", dependencies=[Depends(require_auth)])
async def species_list() -> dict:
    if not _ready:
        raise HTTPException(status_code=503, detail="model still loading")
    return {
        "source": _species_source,
        "count": len(_classifier.species),
        "species": [s.as_dict() for s in _classifier.species],
    }


@app.post("/identify", response_model=IdentifyResponse, dependencies=[Depends(require_auth)])
async def identify(req: IdentifyRequest) -> IdentifyResponse:
    started = time.monotonic()
    if not _ready:
        raise HTTPException(status_code=503, detail="model still loading")

    base = (req.frigate_url or settings.frigate_url).rstrip("/")
    if not base:
        raise HTTPException(
            status_code=400,
            detail="no Frigate URL: set FRIGATE_URL or pass frigate_url in the request",
        )

    candidates = await frames.gather_candidates(_client, req.event_id, base, settings)
    response = await _identify_images(candidates, req.priors, started, req.exclude)
    log.info(
        "identify %s -> %s %s (score=%s margin=%s, %d frames, %dms)",
        req.event_id, response.status, response.common_name or "-",
        response.score, response.margin, response.frames_used, response.elapsed_ms,
    )
    return response


@app.post("/identify/image", response_model=IdentifyResponse,
          dependencies=[Depends(require_auth)])
async def identify_image(file: UploadFile = File(...)) -> IdentifyResponse:
    """Identify a single uploaded image.

    Two jobs: it backs Aviary's "re-identify from this still" action, and it is how you
    sanity-check the model against reference photos without needing a Frigate event.
    """
    started = time.monotonic()
    if not _ready:
        raise HTTPException(status_code=503, detail="model still loading")

    data = await file.read()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        image = image.convert("RGB")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"could not decode image: {exc}") from exc

    candidate = frames.Candidate(image=image, origin=file.filename or "upload")
    return await _identify_images([candidate], {}, started)  # no exclusions on ad-hoc uploads
