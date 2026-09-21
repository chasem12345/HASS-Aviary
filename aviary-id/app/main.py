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
import base64
import hmac
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Literal, Optional

import httpx
import torch
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel, Field

from . import frames
from .detector import make_detector
from .model import Classifier
from .trained import make_trained
from .pipeline import Pipeline
from .settings import Settings, load_settings
from .species import load_species

log = logging.getLogger("aviary_id")

settings: Settings = load_settings()

# One lock around all GPU work; concurrency is the caller's queue, not ours. Kept even
# on cards with headroom: media gathering (the slow, I/O-bound part) already overlaps
# outside this lock, the TFLite interpreter and the YOLO predictor are not safe to share
# across threads as single instances, and a stray parallel request degrades to waiting
# rather than OOM.
_gpu_lock = asyncio.Lock()

_classifier: Optional[Classifier] = None
_detector = None
_pipeline: Optional[Pipeline] = None
_client: Optional[httpx.AsyncClient] = None
_species_source = "not loaded"
_ready = False


# ------------------------------------------------------------------------------ models

class ZoomWindow(BaseModel):
    """A second camera's recordings window that replaces the event clip.

    For setups where a wide camera runs detection (and owns the event) while a
    record-only PTZ camera holds the zoomed view: the event id still provides the wide
    camera's thumbnail/snapshot (Frigate's crops, and the fallback when the PTZ missed);
    this window names the footage actually worth classifying.
    """
    camera: str
    start: float  # epoch seconds
    end: float


class IdentifyRequest(BaseModel):
    event_id: str
    # Per-request override, so one service can serve more than one Frigate instance.
    frigate_url: Optional[str] = None
    # Optional zoomed-footage source (see ZoomWindow). Ignored when malformed.
    zoom: Optional[ZoomWindow] = None
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
    # Return per-crop diagnostics (geometry, embedding, top-1) under ``debug`` so the
    # subject partition can be re-run and tuned offline (tools/tune_subjects.py).
    debug: bool = False
    # Which species the returned ``embedding``/``best_crop`` should be chosen FOR (common
    # or scientific name, as in /species), on subject ``target_subject`` (0 = primary).
    # A human relabelling a card names the bird; this makes the example Aviary keeps a
    # frame of THAT bird, not the frame that looked most like the classifier's wrong
    # guess. Never changes the species answer. ``embedding_target`` on the response says
    # whether it was honoured (null: unknown name, excluded, or no such subject).
    target: Optional[str] = None
    target_subject: int = Field(default=0, ge=0)
    # "confirm" (0.12.0+): Frigate's own classifier has already named this bird and the
    # caller wants a second opinion, fast. Only Frigate's crops of the tracked object
    # (thumbnail, snapshot cut to its box) are classified — no clip download, no ffmpeg,
    # no detector, no zoom — so the answer is about THAT bird and arrives in a fraction
    # of the time. Escalation has nothing to escalate into; priors/exclude/target keep
    # their meaning. "full" is every earlier behaviour, unchanged.
    mode: Literal["full", "confirm"] = "full"
    # Frigate's label and its classification score, echoed back on the response with
    # ``frigate_agrees``. Provenance only: deliberately NOT applied as a prior, because
    # the default supervised backend here (AIY) is the very model behind Frigate's
    # classifier — feeding its answer back in would count one observation twice and
    # inflate exactly the species the caller is trying to check.
    frigate_label: Optional[str] = None
    frigate_label_score: Optional[float] = None


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
    # The supervised classifier's own verdict for this frame, before mixing. Empty/0.0
    # means it saw essentially nothing it recognized (background-heavy output).
    trained_top1: str = ""
    trained_score: float = 0.0


class SubjectOut(BaseModel):
    """One bird in the event, classified on its own crops.

    ``idx`` 0 is the primary — the tracked object this event is about, which is also what
    every top-level field of the response describes. Others are birds that shared the
    view: they have their own species, crop and embedding so a label on one of them can
    never train the model on a picture of the other.
    """
    idx: int
    primary: bool
    # False when Frigate's crop/path could not pin the primary down and the service fell
    # back to the biggest cluster (zoomed footage without a path, a boxless snapshot).
    anchored: bool
    common_name: str
    scientific_name: Optional[str] = None
    species_code: Optional[str] = None
    score: float
    margin: float
    runner_up: Optional[str] = None
    candidates: list[SpeciesGuess] = Field(default_factory=list)
    consensus: Optional[dict] = None
    trained: bool = False
    n_frames: int = 0
    origins: list[str] = Field(default_factory=list)
    embedding: Optional[str] = None
    best_crop: Optional[str] = None
    per_frame: list[FrameOut] = Field(default_factory=list)
    # Which species ``embedding``/``best_crop`` were chosen for (request ``target``), and
    # its probability on that frame. Null when chosen for the fused winner as usual.
    embedding_target: Optional[str] = None
    embedding_target_score: Optional[float] = None
    # Share of this bird's crops whose own top-1 is its answer (null with one crop) and
    # the minimum cosine among them. Low purity on the primary means two birds were
    # fused into one answer — flag it rather than trust the score.
    purity: Optional[float] = None
    cohesion: Optional[float] = None
    # Origin of the crop behind ``best_crop``/``embedding`` (e.g. "clip@2.50s").
    best_origin: Optional[str] = None
    # Frames this bird shared with the primary (0 for the primary itself). A secondary
    # with co_occurring > 0 was seen BESIDE the tracked bird — a second bird for certain.
    # 0 means it was kept on other evidence (off the tracked path, or several crops
    # confidently naming another species) and never appeared in the same frame.
    co_occurring: int = 0
    # Clip crops of this bird that were the only detector box in their frame.
    solo_frames: int = 0


class DebugCrop(BaseModel):
    """Per-crop facts for offline tuning of the subject partition."""
    origin: str
    det_score: float
    rank: float
    pre_cropped: bool
    center: Optional[tuple[float, float]] = None
    anchor_dist: Optional[float] = None
    t: Optional[float] = None
    subject: Optional[int] = None   # which subjects[] entry took it; None = dropped
    note: str = ""
    best_for: Optional[int] = None  # the subjects[] entry whose best_crop/embedding this is
    top1: str = ""
    top1_score: float = 0.0
    zoomed: bool = False            # from the zoomed PTZ recordings
    frame_boxes: int = 1            # detector boxes in this crop's frame (de-duplicated)
    embedding: str = ""             # base64 float16, same encoding as the response's


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
    # Frame agreement about the winner: {votes, supporting, fraction, agreed, score}.
    # None when fewer than two frames could vote — the caller must treat "no data" and
    # "frames disagreed" differently.
    consensus: Optional[dict] = None
    # Whether the supervised (trained) classifier contributed to this answer. False
    # means zero-shot only: TRAINED_CLASSIFIER=none, or no regional species mapped.
    trained: bool = False
    embedding: Optional[str] = None
    model_version: Optional[str] = None
    # What the embedding is keyed by: the model name alone. Image embeddings survive
    # vocabulary and label-format changes; model_version deliberately does not.
    embedding_key: Optional[str] = None
    # Small JPEG (base64) of the crop that best backed the winner — the same frame the
    # embedding comes from. Aviary stores it and shows it as the card thumbnail, which
    # matters most when the classified footage (e.g. a zoomed PTZ recording) is not the
    # event's own media.
    best_crop: Optional[str] = None
    # See SubjectOut. Mirrored here for the primary: ``embedding_target`` is the species
    # the embedding/best_crop were chosen for when the request named a ``target`` (null
    # otherwise — and null on purpose when ``target_subject`` was not 0, since the
    # targeted frame then lives on that subjects[] entry). A pre-0.11 service omits the
    # key entirely; a caller should treat both as "not chosen for my label".
    embedding_target: Optional[str] = None
    embedding_target_score: Optional[float] = None
    purity: Optional[float] = None
    cohesion: Optional[float] = None
    best_origin: Optional[str] = None
    # Every bird in the event, primary first. The top-level fields above are exactly
    # subjects[0] — including embedding_target; older callers can ignore this list.
    subjects: list[SubjectOut] = Field(default_factory=list)
    # Crops the classifier looked at that belong to no reported bird: a cluster that
    # failed the evidence gate (never seen beside the tracked bird, or too few crops /
    # no confident species — SUBJECT_MIN_CROPS, SUBJECT_SINGLE_DET, SUBJECT_VOTE_MIN) or
    # the SUBJECT_MAX cap. Their per-frame verdicts, so "what was that third thing?" has
    # an answer without pretending it was a bird worth naming.
    unassigned: list[FrameOut] = Field(default_factory=list)
    # Only with ``debug: true`` on the request.
    debug: Optional[dict] = None
    elapsed_ms: int = 0
    # Which request ``mode`` produced this answer, and whether the clip was skipped. A
    # pre-0.12 service omits both; a caller should read that as a full run.
    mode: str = "full"
    clip_skipped: bool = False
    # The request's Frigate label echoed back, and whether this answer names the same
    # species (common or scientific name, case-insensitive). None without a label. The
    # caller decides what disagreement means; this only reports it.
    frigate_label: Optional[str] = None
    frigate_label_score: Optional[float] = None
    frigate_agrees: Optional[bool] = None


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


# Long edge of the best-crop JPEG returned to the caller. A card thumbnail, not an
# archive — tens of KB, never the full frame.
_CROP_MAX_EDGE = 480


def _encode_crop(image: Image.Image) -> Optional[str]:
    """Base64 JPEG of a crop, downscaled for use as a card thumbnail."""
    try:
        img = image
        scale = _CROP_MAX_EDGE / max(img.width, img.height)
        if scale < 1.0:
            img = img.resize((max(1, round(img.width * scale)),
                              max(1, round(img.height * scale))), Image.BICUBIC)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except (OSError, ValueError) as exc:
        log.debug("Could not encode the best crop: %s", exc)
        return None


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
        # The supervised primary. Built after the vocabulary exists so its label
        # mapping is over the same regional species list, and after the classifier so
        # the GPU backend (and "auto") can follow its device choice.
        trained = make_trained(settings, classifier.device)
        if trained is not None:
            trained.set_species(species)
            classifier.trained = trained
        import torch as _t
        det_device = _t.device("cpu") if settings.detector_cpu else classifier.device
        detector = make_detector(det_device, settings)
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


app = FastAPI(title="aviary-id", version="0.13.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness plus the facts you actually need when something looks wrong.

    Intentionally unauthenticated so a container healthcheck and Aviary's status pill work
    without provisioning the token; it exposes no secrets.
    """
    return {
        "ok": _ready,
        # The service version, so a caller can tell which request fields (``target``,
        # 0.11.0+; ``mode: confirm``, 0.12.0+) this instance honours without probing.
        "version": app.version,
        "cuda": torch.cuda.is_available(),
        "device": _classifier.device_name() if _classifier else "not loaded",
        "cpu_only": settings.cpu_only,
        "model": settings.model_name,
        "model_version": _classifier.model_version if _classifier else None,
        "embedding_key": _classifier.embedding_key if _classifier else None,
        "trained_classifier": settings.trained_classifier,
        # The class actually serving as the supervised backend — this is what makes
        # TRAINED_CLASSIFIER=auto diagnosable from the outside.
        "trained_backend": (type(_classifier.trained).__name__
                            if _classifier and _classifier.trained else None),
        # How many regional species the supervised model actually covers; the rest are
        # zero-shot only. 0 with a backend enabled means the label mapping failed.
        "trained_coverage": (_classifier.trained.coverage
                             if _classifier and _classifier.trained else 0),
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

    confirm = req.mode == "confirm"
    zoom = None
    if req.zoom and req.zoom.camera.strip() and req.zoom.end > req.zoom.start:
        if confirm:
            # Zoom means "classify the PTZ recordings instead of the clip" — footage,
            # which confirm mode never fetches. Frigate's crops come from the detect
            # camera regardless, so the answer is still about the tracked bird.
            log.info("Event %s: zoom ignored in confirm mode (no footage is fetched).",
                     req.event_id)
        else:
            zoom = frames.Zoom(camera=req.zoom.camera.strip(),
                               start=req.zoom.start, end=req.zoom.end)
    timings = frames.Timings()
    media = frames.EventMedia(req.event_id, base, settings, zoom=zoom, confirm=confirm)
    try:
        await media.gather(_client, timings)
        response = await _run_pipeline(media, req, timings, started)
    finally:
        # The temp dir holds the downloaded clip and its extracted frames; every decoded
        # image is already in memory by now.
        await media.close()

    trained_note = "off"
    if response.trained:
        # Summarize what the supervised model saw across frames: its strongest
        # per-frame verdict, or "saw nothing" when every frame was background-heavy.
        best = max(response.per_frame, key=lambda f: f.trained_score, default=None)
        trained_note = (f"{best.trained_top1} {best.trained_score:.2f}"
                        if best and best.trained_score >= 0.05 else "saw nothing")
    others = ""
    if len(response.subjects) > 1:
        others = " +" + ", ".join(
            f"{s.common_name} {s.score:.2f} ({s.n_frames} crop{'s' if s.n_frames != 1 else ''})"
            for s in response.subjects[1:]) + " also in view"
    frigate_note = ""
    if confirm:
        frigate_note = " mode=confirm"
        if response.frigate_label:
            frigate_note += (f" frigate={response.frigate_label!r}"
                             f" agrees={response.frigate_agrees}")
    log.info(
        "identify %s -> %s %s (score=%s margin=%s, %d/%d frames, %d round(s), "
        "localized=%s, trained[%s], %dms)%s%s [%s]",
        req.event_id, response.status, response.common_name or "-",
        response.score, response.margin, response.frames_used, response.images,
        response.rounds, response.localized, trained_note, response.elapsed_ms,
        others, frigate_note, timings.summary(),
    )
    return response


def _frigate_agrees(label: Optional[str], result) -> Optional[bool]:
    """Whether the answer names the species Frigate did (common or scientific name)."""
    label = (label or "").strip().lower()
    if not label or result is None:
        return None
    return label in {(result.species.com_name or "").lower(),
                     (result.species.sci_name or "").lower()}


def _with_request_echo(response: IdentifyResponse, req: IdentifyRequest, media,
                       result=None) -> IdentifyResponse:
    """Stamp the mode and Frigate-label provenance onto any response, ok or not.

    The label is the request's when the caller sent one, else whatever Frigate's event
    JSON carried when the media was fetched — which is how a classification Frigate
    applied AFTER the event's end message (never published on the events topic) still
    reaches the caller.
    """
    label, score = req.frigate_label, req.frigate_label_score
    if not label and media is not None and getattr(media, "sub_label", None):
        label, score = media.sub_label, media.sub_label_score
    response.mode = req.mode
    response.clip_skipped = req.mode == "confirm"
    response.frigate_label = label
    response.frigate_label_score = score
    response.frigate_agrees = _frigate_agrees(label, result)
    return response


async def _run_pipeline(media, req, timings, started) -> IdentifyResponse:
    """Shared tail: run the pipeline, map failures onto a status, build the response."""
    async with _gpu_lock:
        try:
            result, crops, rounds = await _pipeline.run(
                media, req.priors, req.exclude, req.min_score, req.min_margin,
                timings, _release_vram, req.target, req.target_subject,
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
            return _with_request_echo(IdentifyResponse(
                status="out_of_memory", elapsed_ms=_ms(started),
                images=len(media.candidates), timings=timings.stages), req, media)
        except RuntimeError as exc:
            # cuDNN failures on a full card arrive as a plain RuntimeError rather than
            # OutOfMemoryError (CUDNN_STATUS_INTERNAL_ERROR is the usual one), so they need
            # the same treatment or they become a 500 too.
            _release_vram()
            log.error("Inference failed: %s. %s", exc, _classifier.memory_summary())
            return _with_request_echo(IdentifyResponse(
                status="error", elapsed_ms=_ms(started),
                images=len(media.candidates), timings=timings.stages), req, media)

    if not media.candidates:
        return _with_request_echo(IdentifyResponse(
            status="no_media", elapsed_ms=_ms(started), timings=timings.stages), req, media)
    if result is None:
        # Nothing anywhere in the event looked like a bird, even after escalating.
        # Deliberately NOT falling back to classifying the whole uncropped frame: on a
        # 1080p frame that leaves a feeder-distance bird about ten pixels across, and it
        # only ever produced confidently-wrong answers the caller then rejected anyway.
        return _with_request_echo(IdentifyResponse(
            status="no_bird", elapsed_ms=_ms(started),
            images=len(media.candidates), timings=timings.stages), req, media)

    return _with_request_echo(IdentifyResponse(
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
        consensus=result.consensus,
        trained=result.trained,
        embedding=result.embedding,
        model_version=_classifier.model_version,
        embedding_key=_classifier.embedding_key,
        best_crop=(_encode_crop(crops[result.best_frame].image)
                   if 0 <= result.best_frame < len(crops) else None),
        embedding_target=result.embedding_target,
        embedding_target_score=result.embedding_target_score,
        purity=result.purity,
        cohesion=result.cohesion,
        best_origin=_origin_of(result, crops),
        subjects=[_subject_out(i, r, crops) for i, r in enumerate([result, *result.others])],
        unassigned=[FrameOut(**vars(f)) for f in result.unassigned],
        debug=_debug_out(result, crops) if req.debug else None,
        elapsed_ms=_ms(started),
        timings=timings.stages,
    ), req, media, result)


def _origin_of(r, crops) -> Optional[str]:
    return crops[r.best_frame].origin if 0 <= r.best_frame < len(crops) else None


def _subject_out(idx: int, r, crops) -> SubjectOut:
    return SubjectOut(
        idx=idx,
        primary=r.primary,
        anchored=r.anchored,
        common_name=r.species.com_name,
        scientific_name=r.species.sci_name,
        species_code=r.species.species_code,
        score=round(r.score, 4),
        margin=round(r.margin, 4),
        runner_up=r.runner_up.com_name if r.runner_up else None,
        candidates=[
            SpeciesGuess(common_name=sp.com_name, scientific_name=sp.sci_name,
                         species_code=sp.species_code, score=round(score, 4))
            for sp, score in r.candidates
        ],
        consensus=r.consensus,
        trained=r.trained,
        n_frames=len(r.indices),
        origins=[crops[i].origin for i in r.indices if 0 <= i < len(crops)],
        embedding=r.embedding,
        best_crop=(_encode_crop(crops[r.best_frame].image)
                   if 0 <= r.best_frame < len(crops) else None),
        per_frame=[FrameOut(**vars(f)) for f in r.per_frame],
        embedding_target=r.embedding_target,
        embedding_target_score=r.embedding_target_score,
        purity=r.purity,
        cohesion=r.cohesion,
        best_origin=_origin_of(r, crops),
        co_occurring=int(getattr(r, "co_occurring", 0) or 0),
        solo_frames=int(getattr(r, "solo_frames", 0) or 0),
    )


def _debug_out(result, crops) -> dict:
    """Per-crop geometry + embeddings, so tools/tune_subjects.py can re-partition offline."""
    owner: dict[int, int] = {}
    notes: dict[int, str] = {}
    best_for: dict[int, int] = {}
    for s_idx, r in enumerate([result, *result.others]):
        best_for[r.best_frame] = s_idx
        for k, i in enumerate(r.indices):
            owner[i] = s_idx
            if k < len(r.notes):
                notes[i] = r.notes[k]
    per_frame = {}
    for r in [result, *result.others]:
        for i, f in zip(r.indices, r.per_frame):
            per_frame[i] = f
    # Unassigned crops were classified too; their verdicts ride along with subject=None.
    # Keyed by crop index (two unassigned boxes from one frame must not share a verdict);
    # falls back to the origin for a result that did not record the indices.
    unassigned_idx = getattr(result, "unassigned_indices", None) or []
    if len(unassigned_idx) == len(result.unassigned):
        for i, f in zip(unassigned_idx, result.unassigned):
            per_frame.setdefault(i, f)
    else:
        by_origin = {f.origin: f for f in result.unassigned}
        for i, c in enumerate(crops):
            if i not in owner and c.origin in by_origin:
                per_frame.setdefault(i, by_origin[c.origin])
    out = []
    for i, c in enumerate(crops):
        f = per_frame.get(i)
        out.append(DebugCrop(
            origin=c.origin, det_score=float(c.score), rank=float(c.rank),
            pre_cropped=bool(c.pre_cropped), center=c.center, anchor_dist=c.anchor_dist,
            t=c.t, subject=owner.get(i), note=notes.get(i, ""), best_for=best_for.get(i),
            top1=f.top1 if f else "", top1_score=f.top1_score if f else 0.0,
            zoomed=bool(getattr(c, "zoomed", False)),
            frame_boxes=int(getattr(c, "frame_boxes", 1) or 1),
            embedding=_classifier.crop_embedding(i) if _classifier else "",
        ).model_dump())
    return {"crops": out, "settings": {
        "sim_merge": settings.subject_sim_merge, "sim_split": settings.subject_sim_split,
        "min_crops": settings.subject_min_crops, "single_det": settings.subject_single_det,
        "max_subjects": settings.subject_max,
        "sim_primary": settings.subject_sim_primary,
        "sim_secondary": settings.subject_sim_secondary,
        "vote_min": settings.subject_vote_min,
        "sim_cross": settings.subject_sim_cross,
    }}


@app.post("/identify/image", response_model=IdentifyResponse,
          dependencies=[Depends(require_auth)])
async def identify_image(file: UploadFile = File(...),
                         target: Optional[str] = Form(default=None)) -> IdentifyResponse:
    """Identify a single uploaded image.

    Two jobs: it backs Aviary's "re-identify from this still" action, and it is how you
    sanity-check the model against reference photos without needing a Frigate event.
    ``target`` (form field) works as on /identify; with one crop it cannot change which
    frame is used, but ``embedding_target`` then tells the caller whether the name it is
    about to file the embedding under exists in this vocabulary.
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
    req = IdentifyRequest(event_id="upload", target=target)
    return await _run_pipeline(media, req, frames.Timings(), started)
