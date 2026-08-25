"""Runtime configuration for aviary-id.

Everything comes from environment variables — this service runs as a plain Docker
container on the GPU host, not as a Home Assistant add-on, so there is no options.json
equivalent. Defaults are chosen so that `docker compose up` with only FRIGATE_URL set
produces a working (if unregionalized) service.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return default


def _as_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return default


def _as_list(key: str) -> tuple[str, ...]:
    """Comma-separated env var -> tuple of trimmed non-empty values."""
    raw = os.environ.get(key, "")
    return tuple(v for v in (p.strip() for p in raw.split(",")) if v)


@dataclass(frozen=True)
class Settings:
    # --- upstream -----------------------------------------------------------------
    # Default Frigate base URL. A request may override it per-call, which is what makes
    # the service usable against more than one Frigate instance.
    frigate_url: str
    frigate_headers: dict[str, str] = field(default_factory=dict)

    # --- auth ---------------------------------------------------------------------
    # Shared secret expected as `Authorization: Bearer <token>`. Blank disables the
    # check entirely (fine on a trusted LAN segment; set it before exposing the port
    # across subnets).
    auth_token: str = ""

    # --- model --------------------------------------------------------------------
    # Supervised classifier that carries the primary vote: "aiy" (Google's iNaturalist
    # bird MobileNet — the model behind Frigate's native classification; CPU, ~5ms),
    # "inat21" (a timm iNat21 fine-tune on the GPU — far stronger, ~1.2 GB VRAM, see
    # TRAINED_MODEL), "auto" (inat21 when CUDA is available, aiy otherwise) or "none"
    # for zero-shot only. Zero-shot alone is measurably worse on common regional birds;
    # "none" exists for A/B measurement, not as a recommendation.
    trained_classifier: str = "aiy"
    # HF Hub id of the timm checkpoint behind TRAINED_CLASSIFIER=inat21. The default is
    # EVA02-L/14 @336 fine-tuned on iNaturalist 2021 (10,000 species, 92% top-1 there).
    # Checkpoint license is cc-by-nc-4.0 — see the README licensing note.
    trained_model: str = "timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
    # Crops per forward pass for the inat21 backend. The peak, not the total, is what
    # OOMs a shared card — same reasoning as DETECTOR_BATCH.
    trained_batch: int = 4
    # The supervised model's share of the per-frame probability mix, for species it was
    # trained on. The remainder is BioCLIP zero-shot, which alone covers species the
    # trained model has never seen.
    trained_weight: float = 0.75
    # If the supervised model is at least this sure on its BEST frame, its verdict wins
    # outright instead of being averaged away by frames where it saw nothing. A bird
    # confidently recognized once WAS recognized — a feeder event's other frames are
    # routinely occlusion and motion blur, and their zero-shot noise must not outvote
    # the one clean look. (WhosAtMyFeeder gates this same model at 0.7, single frame.)
    trained_accept: float = 0.65
    model_name: str = "hf-hub:imageomics/bioclip-2"
    # Where model weights, the species list, and the text-embedding cache live. Mount
    # this as a volume so a container rebuild doesn't re-download ~2 GB.
    cache_dir: str = "/models"
    # Force CPU even when CUDA is present. Mostly for testing the whole pipeline on a
    # machine without the GPU — ~5s/event instead of ~0.3s, which is fine for this
    # workload.
    cpu_only: bool = False
    # Run the bird detector on the CPU while the classifier stays on the GPU. The detector
    # is small and its activations are the spikiest part of the pipeline, so on a card
    # shared with other workloads this buys headroom for a few hundred ms per event.
    detector_cpu: bool = False
    # Frames handed to the detector at once. Lower means a lower peak; drop back to 2
    # on a 4 GB card or when running a big DETECTOR_MODEL at DETECTOR_IMGSZ 1280.
    detector_batch: int = 4

    # --- frame extraction ---------------------------------------------------------
    # Frames pulled from the clip before filtering. Oversample: Frigate clips include
    # pre_capture/post_capture padding where the bird may not be in frame at all.
    # Deliberately NOT raised with GPU headroom: each sampled frame costs an ffmpeg
    # seek+decode (the actual per-event bottleneck), and moment-collapsing in the
    # consensus dedupes near-duplicate frames anyway.
    sample_frames: int = 8
    # Frames actually classified in the first pass, chosen best-first. Pure GPU cost —
    # the candidates are already extracted and detected — so this is cheap headroom to
    # spend: more frames means better fusion and more honest consensus votes.
    classify_frames: int = 4
    # Hard ceiling on frames classified across all escalation rounds.
    max_frames: int = 10
    # Use Frigate's own crops instead of re-deriving them. thumbnail.jpg is cropped to the
    # object on an ended event; the event API reports the snapshot's box in pixels. Both
    # are more reliable than our COCO detector at finding a small bird, because locating
    # the object is what Frigate was doing in the first place.
    use_thumbnail: bool = True
    use_event_box: bool = True
    # How each species is described to the text encoder: common | binomial |
    # binomial_common | taxonomy. See Species.label — this is the single biggest lever on
    # whether the model can tell apart species within a family, and it is worth A/B-ing
    # against birds you can name. Changing it re-encodes the vocabulary on next start.
    label_format: str = "common"
    # Average each species over the 80 OpenAI prompt templates. Right for short labels
    # (pybioclip's CustomLabelsClassifier does this); wrong for full taxonomic strings,
    # which produce text like "a tattoo of a Animalia Chordata Aves ...".
    prompt_ensemble: bool = True
    # Which bird localizer to run over clip frames: "yolo" (default — better on the
    # small, shaded, partly-hidden birds clip frames contain, but AGPL-3.0) or "frcnn"
    # (torchvision Faster R-CNN, BSD-3, no extra dependency).
    detector_backend: str = "yolo"
    # Which official Ultralytics weights the yolo backend runs: yolo11n.pt (default),
    # yolo11s.pt or yolo11m.pt. Bigger models find more of the small, far birds; on an
    # 8 GB card yolo11s is a comfortable step up. Ignored by frcnn.
    detector_model: str = "yolo11n.pt"
    # Inference resolution for the yolo backend. 640 is the model's native default;
    # 960/1280 materially helps feeder-distance birds that are tens of pixels across in
    # a 1080p+ frame, at a VRAM/latency cost. Ignored by frcnn.
    detector_imgsz: int = 640
    # Minimum detector score for a box to count as a usable bird. Deliberately permissive:
    # downstream ranking (score * sqrt(area)), detector-score-weighted fusion and the
    # per-frame consensus vote all suppress junk boxes, whereas a box never proposed is a
    # bird never classified.
    detector_threshold: float = 0.3
    # Fraction of the box's size added as padding on each side before cropping. Birds
    # get cropped tight by the detector; a little context helps the classifier.
    crop_padding: float = 0.15
    # Give up on a clip download / ffmpeg run after this long.
    fetch_timeout: float = 30.0
    ffmpeg_timeout: float = 60.0

    # --- species vocabulary -------------------------------------------------------
    # Free key from https://ebird.org/api/keygen. Without it the service falls back to
    # the bundled common-North-American-yard-birds list.
    ebird_api_key: str = ""
    # e.g. "US-CO-013" (county), "US-CO" (state), "US" (country). County is best.
    ebird_region: str = ""
    ebird_refresh_days: int = 30
    # Manual overrides, comma-separated common OR scientific names.
    extra_species: tuple[str, ...] = ()
    exclude_species: tuple[str, ...] = ()

    @property
    def ebird_enabled(self) -> bool:
        return bool(self.ebird_api_key and self.ebird_region)


def _frigate_headers() -> dict[str, str]:
    """Extra headers for every Frigate call, e.g. an auth or proxy header.

    Format: ``FRIGATE_HEADERS="X-API-Key: abc, X-Proxy-User: aviary"``. Values may not
    contain a comma; that limitation is documented in the README and has not mattered
    for any real Frigate auth scheme.
    """
    headers: dict[str, str] = {}
    for part in _as_list("FRIGATE_HEADERS"):
        name, sep, value = part.partition(":")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    return headers


def load_settings() -> Settings:
    return Settings(
        frigate_url=os.environ.get("FRIGATE_URL", "").rstrip("/"),
        frigate_headers=_frigate_headers(),
        auth_token=os.environ.get("AVIARY_ID_TOKEN", "").strip(),
        trained_classifier=os.environ.get("TRAINED_CLASSIFIER", "aiy").strip().lower(),
        trained_model=os.environ.get(
            "TRAINED_MODEL", "timm/eva02_large_patch14_clip_336.merged2b_ft_inat21"
        ).strip(),
        trained_batch=max(1, _as_int("TRAINED_BATCH", 4)),
        trained_weight=min(1.0, max(0.0, _as_float("TRAINED_WEIGHT", 0.75))),
        trained_accept=min(1.0, max(0.0, _as_float("TRAINED_ACCEPT", 0.65))),
        model_name=os.environ.get("MODEL_NAME", "hf-hub:imageomics/bioclip-2"),
        cache_dir=os.environ.get("CACHE_DIR", "/models"),
        cpu_only=_as_bool(os.environ.get("CPU_ONLY", "")),
        detector_cpu=_as_bool(os.environ.get("DETECTOR_CPU", "")),
        detector_batch=_as_int("DETECTOR_BATCH", 4),
        sample_frames=_as_int("SAMPLE_FRAMES", 8),
        classify_frames=_as_int("CLASSIFY_FRAMES", 4),
        max_frames=_as_int("MAX_FRAMES", 10),
        use_thumbnail=not _as_bool(os.environ.get("NO_THUMBNAIL", "")),
        use_event_box=not _as_bool(os.environ.get("NO_EVENT_BOX", "")),
        label_format=os.environ.get("LABEL_FORMAT", "common").strip().lower(),
        prompt_ensemble=not _as_bool(os.environ.get("NO_PROMPT_ENSEMBLE", "")),
        detector_backend=os.environ.get("DETECTOR_BACKEND", "yolo").strip().lower(),
        detector_model=os.environ.get("DETECTOR_MODEL", "yolo11n.pt").strip(),
        detector_imgsz=max(320, _as_int("DETECTOR_IMGSZ", 640)),
        detector_threshold=_as_float("DETECTOR_THRESHOLD", 0.3),
        crop_padding=_as_float("CROP_PADDING", 0.15),
        fetch_timeout=_as_float("FETCH_TIMEOUT", 30.0),
        ffmpeg_timeout=_as_float("FFMPEG_TIMEOUT", 60.0),
        ebird_api_key=os.environ.get("EBIRD_API_KEY", "").strip(),
        ebird_region=os.environ.get("EBIRD_REGION", "").strip(),
        ebird_refresh_days=_as_int("EBIRD_REFRESH_DAYS", 30),
        extra_species=_as_list("EXTRA_SPECIES"),
        exclude_species=_as_list("EXCLUDE_SPECIES"),
    )
