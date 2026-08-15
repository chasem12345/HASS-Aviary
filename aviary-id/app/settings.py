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
    # Frames handed to the detector at once. Lower means a lower peak.
    detector_batch: int = 2

    # --- frame extraction ---------------------------------------------------------
    # Frames pulled from the clip before filtering. Oversample: Frigate clips include
    # pre_capture/post_capture padding where the bird may not be in frame at all.
    sample_frames: int = 8
    # Frames actually classified, chosen best-first from the sampled set.
    classify_frames: int = 3
    # Minimum detector score for a box to count as a usable bird.
    detector_threshold: float = 0.5
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
        model_name=os.environ.get("MODEL_NAME", "hf-hub:imageomics/bioclip-2"),
        cache_dir=os.environ.get("CACHE_DIR", "/models"),
        cpu_only=_as_bool(os.environ.get("CPU_ONLY", "")),
        detector_cpu=_as_bool(os.environ.get("DETECTOR_CPU", "")),
        detector_batch=_as_int("DETECTOR_BATCH", 2),
        sample_frames=_as_int("SAMPLE_FRAMES", 8),
        classify_frames=_as_int("CLASSIFY_FRAMES", 3),
        detector_threshold=_as_float("DETECTOR_THRESHOLD", 0.5),
        crop_padding=_as_float("CROP_PADDING", 0.15),
        fetch_timeout=_as_float("FETCH_TIMEOUT", 30.0),
        ffmpeg_timeout=_as_float("FFMPEG_TIMEOUT", 60.0),
        ebird_api_key=os.environ.get("EBIRD_API_KEY", "").strip(),
        ebird_region=os.environ.get("EBIRD_REGION", "").strip(),
        ebird_refresh_days=_as_int("EBIRD_REFRESH_DAYS", 30),
        extra_species=_as_list("EXTRA_SPECIES"),
        exclude_species=_as_list("EXCLUDE_SPECIES"),
    )
