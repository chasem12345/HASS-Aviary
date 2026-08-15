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
from .detector import BirdDetector
from .model import Classifier
from .pipeline import Pipeline
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
_pipeline: Optional[Pipeline] = None
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
    # The caller's confidence thresholds. Used ONLY to decide whether to escalate —
    # this service never gates on them, it just tries harder below them. Keeping them
    # on the request means one source of truth: retuning Aviary's thresholds retunes
    # when the GPU works harder, with no redeploy here.
    min_score: float = 0.35
    min_margin: float = 0.08


class SpeciesGuess(BaseModel):
    common_name: str
    scientific_name: Optional[str] = None
    species_code: Optional[str] = None
    score: float


class FrameOut(BaseModel):
    origin: str
    det_score: float
    top1: str
    top1_score: float
    top2: str
    top2_score: float


class IdentifyResponse(BaseModel):
    status: str  # ok | no_media | no_bird | not_ready | out_of_memory | error
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
    # Images gathered before localization, and how many classification rounds it took.
    # rounds > 1 means the answer was uncertain and the service looked harder.
    images: int = 0
    rounds: int = 0
    # The best few species considered, best first. Populated even when the top answer is
    # below the caller's thresholds — that is exactly when it is worth showing, because it
    # tells the user the model tried and often has the right bird at number two.
    candidates: list[SpeciesGuess] = Field(default_factory=list)
    # Per-stage wall-clock in ms, so a slow event says WHICH stage was slow.
    timings: dict = Field(default_factory=dict)
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

def _release_vram() -> None:
    """Return cached blocks to the driver so other processes on this GPU can use them."""
    if _classifier is not None and _classifier.device.type == "cuda":
        torch.cuda.empty_cache()


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# ------------------------------------------------------------------------------ routes

@asynccontextmanager
async def lifespan(_: FastAPI):
    global _classifier, _detector, _pipeline, _client, _species_source, _ready

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
        import torch as _t
        det_device = _t.device("cpu") if settings.detector_cpu else classifier.device
        detector = BirdDetector(det_device, settings.detector_threshold)
        log.info("Startup complete. %s", classifier.memory_summary())
        return classifier, detector

    # Blocking: weights load plus (on a cold cache) tens of thousands of text encodes.
    # Off the loop so /healthz answers "ok: false" during startup instead of hanging,
    # which is what lets Aviary show "still loading" rather than "unreachable".
    _classifier, _detector = await asyncio.to_thread(build)
    _pipeline = Pipeline(_classifier, _detector, settings)
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


app = FastAPI(title="aviary-id", version="0.3.0", lifespan=lifespan)


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
        **(_classifier.memory() if _classifier else {}),
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

    timings = frames.Timings()
    media = frames.EventMedia(req.event_id, base, settings)
    try:
        await media.gather(_client, timings)
        response = await _run_pipeline(media, req, timings, started)
    finally:
        # The temp dir holds the downloaded clip and its extracted frames; every decoded
        # image is already in memory by now.
        await media.close()

    log.info(
        "identify %s -> %s %s (score=%s margin=%s, %d/%d frames, %d round(s), "
        "localized=%s, %dms) [%s]",
        req.event_id, response.status, response.common_name or "-",
        response.score, response.margin, response.frames_used, response.images,
        response.rounds, response.localized, response.elapsed_ms, timings.summary(),
    )
    return response


async def _run_pipeline(media, req, timings, started) -> IdentifyResponse:
    """Shared tail: run the pipeline, map failures onto a status, build the response."""
    async with _gpu_lock:
        try:
            result, crops, rounds = await _pipeline.run(
                media, req.priors, req.exclude, req.min_score, req.min_margin,
                timings, _release_vram,
            )
        except torch.cuda.OutOfMemoryError:
            # Never let this surface as a 500. The caller can only record that as a generic
            # failure, and the actual cause — a full GPU — is the one thing worth saying
            # out loud, because it is usually another process on the same card.
            _release_vram()
            log.error(
                "Out of GPU memory. %s. Another process on this GPU is the usual cause; "
                "otherwise lower DETECTOR_BATCH/CLASSIFY_FRAMES, set DETECTOR_CPU=1, or "
                "use a smaller MODEL_NAME.",
                _classifier.memory_summary(),
            )
            return IdentifyResponse(status="out_of_memory", elapsed_ms=_ms(started),
                                    images=len(media.candidates), timings=timings.stages)
        except RuntimeError as exc:
            # cuDNN failures on a full card arrive as a plain RuntimeError rather than
            # OutOfMemoryError (CUDNN_STATUS_INTERNAL_ERROR is the usual one), so they need
            # the same treatment or they become a 500 too.
            _release_vram()
            log.error("Inference failed: %s. %s", exc, _classifier.memory_summary())
            return IdentifyResponse(status="error", elapsed_ms=_ms(started),
                                    images=len(media.candidates), timings=timings.stages)

    if not media.candidates:
        return IdentifyResponse(status="no_media", elapsed_ms=_ms(started),
                                timings=timings.stages)
    if result is None:
        # Nothing anywhere in the event looked like a bird, even after escalating.
        # Deliberately NOT falling back to classifying the whole uncropped frame: on a
        # 1080p frame that leaves a feeder-distance bird about ten pixels across, and it
        # only ever produced confidently-wrong answers the caller then rejected anyway.
        return IdentifyResponse(status="no_bird", elapsed_ms=_ms(started),
                                images=len(media.candidates), timings=timings.stages)

    return IdentifyResponse(
        status="ok",
        common_name=result.species.com_name,
        scientific_name=result.species.sci_name,
        species_code=result.species.species_code,
        score=round(result.score, 4),
        margin=round(result.margin, 4),
        runner_up=result.runner_up.com_name if result.runner_up else None,
        localized=True,
        frames_used=len(crops),
        images=len(media.candidates),
        rounds=rounds,
        candidates=[
            SpeciesGuess(common_name=sp.com_name, scientific_name=sp.sci_name,
                         species_code=sp.species_code, score=round(score, 4))
            for sp, score in result.candidates
        ],
        excluded=result.excluded,
        per_frame=[FrameOut(**vars(f)) for f in result.per_frame],
        embedding=result.embedding,
        model_version=_classifier.model_version,
        elapsed_ms=_ms(started),
        timings=timings.stages,
    )


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

    # Treated as pre-cropped: an uploaded reference photo is already a picture OF the
    # bird, so re-detecting inside it would usually find nothing and discard the image.
    media = frames.EventMedia("upload", "", settings)
    media.candidates = [frames.Candidate(image=image, origin=file.filename or "upload",
                                         pre_cropped=True)]
    req = IdentifyRequest(event_id="upload")
    return await _run_pipeline(media, req, frames.Timings(), started)
