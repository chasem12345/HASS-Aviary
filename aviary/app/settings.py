"""Runtime configuration for Aviary.

Configuration comes from environment variables (exported by ``run.sh`` inside the
add-on). As a fallback we also read ``/data/options.json`` directly so the app can run
with a bare ``uvicorn`` invocation. Environment variables always win when set.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def _options_file(data_dir: str) -> dict:
    """Read the add-on options file if present (Supervisor writes it to /data)."""
    path = Path(data_dir) / "options.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _pick(env_key: str, opts: dict, opt_key: str, default: str = "") -> str:
    val = os.environ.get(env_key)
    if val is not None and val != "":
        return val
    opt_val = opts.get(opt_key)
    if opt_val is not None and opt_val != "":
        return str(opt_val)
    return default


def _pick_float(env_key: str, opts: dict, opt_key: str, default: float) -> float:
    try:
        return float(_pick(env_key, opts, opt_key, str(default)))
    except (TypeError, ValueError):
        return default


def _pick_int(env_key: str, opts: dict, opt_key: str, default: int) -> int:
    try:
        return int(_pick(env_key, opts, opt_key, str(default)))
    except (TypeError, ValueError):
        return default


def _pick_list(env_key: str, opts: dict, opt_key: str) -> tuple[str, ...]:
    """List option, lowercased. Env override is comma-separated; options.json is a list.

    Kept separate from ``_pick`` because that stringifies its value, which would turn a
    JSON list into its repr.
    """
    raw = os.environ.get(env_key)
    if raw:
        values = raw.split(",")
    else:
        opt_val = opts.get(opt_key)
        values = opt_val if isinstance(opt_val, list) else []
    return tuple(v for v in (str(x).strip().lower() for x in values) if v)


@dataclass(frozen=True)
class Settings:
    data_dir: str
    db_path: str

    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_password: str

    frigate_url: str
    birdnet_url: str
    frigate_topic: str
    birdnet_topic: str

    backfill_on_start: bool
    ignore_unclassified: bool
    # Frigate camera names (lowercased) whose detections are never recorded.
    ignore_cameras: tuple[str, ...]
    # New species queue for review instead of entering the registry automatically.
    require_species_confirmation: bool
    notify_new_species: bool

    # Optional xeno-canto API key (https://xeno-canto.org/account). Unlocks the curated
    # song/call reference recordings; blank falls back to iNaturalist observation sounds.
    # A credential — never log it or return it from a route.
    xeno_canto_api_key: str

    # --- External identification (aviary-id) --------------------------------------
    # Base URL of the companion GPU service. Blank disables identification entirely
    # regardless of identify_enabled.
    identify_url: str
    # Shared secret sent as a bearer token. A credential — never log it or return it
    # from a route.
    identify_token: str
    identify_enabled: bool
    identify_min_score: float
    identify_min_margin: float
    identify_workers: int
    identify_timeout: int
    identify_retain_days: int
    identify_use_audio_priors: bool
    identify_exclude_blacklisted: bool

    # HA config folder mount (map: homeassistant_config). Missing on bare metal —
    # the blueprint install and notification images degrade gracefully then.
    ha_config_dir: str

    log_level: str

    @property
    def mqtt_enabled(self) -> bool:
        return bool(self.mqtt_host)

    @property
    def identify_active(self) -> bool:
        """Whether to route Frigate detections through the identification service.

        Both the toggle and a URL are required: enabling the feature without pointing it
        anywhere would park every Frigate detection in 'pending' forever, since the
        unclassified gate is bypassed for rows awaiting identification.
        """
        return self.identify_enabled and bool(self.identify_url)


def load_settings() -> Settings:
    data_dir = os.environ.get("DATA_DIR", "/data")
    opts = _options_file(data_dir)

    mqtt_port_raw = _pick("MQTT_PORT", opts, "mqtt_port", "1883")
    try:
        mqtt_port = int(mqtt_port_raw)
    except (TypeError, ValueError):
        mqtt_port = 1883

    return Settings(
        data_dir=data_dir,
        db_path=os.path.join(data_dir, "aviary.db"),
        mqtt_host=_pick("MQTT_HOST", opts, "mqtt_host", ""),
        mqtt_port=mqtt_port,
        mqtt_user=_pick("MQTT_USER", opts, "mqtt_user", ""),
        mqtt_password=_pick("MQTT_PASSWORD", opts, "mqtt_password", ""),
        frigate_url=_pick("FRIGATE_URL", opts, "frigate_url", "").rstrip("/"),
        birdnet_url=_pick("BIRDNET_URL", opts, "birdnet_url", "").rstrip("/"),
        frigate_topic=_pick("FRIGATE_TOPIC", opts, "frigate_topic", "frigate/events"),
        birdnet_topic=_pick("BIRDNET_TOPIC", opts, "birdnet_topic", "birdnet"),
        backfill_on_start=_as_bool(_pick("BACKFILL_ON_START", opts, "backfill_on_start", "true")),
        ignore_unclassified=_as_bool(_pick("IGNORE_UNCLASSIFIED", opts, "ignore_unclassified", "true")),
        ignore_cameras=_pick_list("IGNORE_CAMERAS", opts, "ignore_cameras"),
        require_species_confirmation=_as_bool(
            _pick("REQUIRE_SPECIES_CONFIRMATION", opts, "require_species_confirmation", "true")),
        notify_new_species=_as_bool(_pick("NOTIFY_NEW_SPECIES", opts, "notify_new_species", "true")),
        xeno_canto_api_key=_pick("XENO_CANTO_API_KEY", opts, "xeno_canto_api_key", "").strip(),
        identify_url=_pick("IDENTIFY_URL", opts, "identify_url", "").rstrip("/"),
        identify_token=_pick("IDENTIFY_TOKEN", opts, "identify_token", "").strip(),
        identify_enabled=_as_bool(_pick("IDENTIFY_ENABLED", opts, "identify_enabled", "false")),
        identify_min_score=_pick_float("IDENTIFY_MIN_SCORE", opts, "identify_min_score", 0.35),
        identify_min_margin=_pick_float("IDENTIFY_MIN_MARGIN", opts, "identify_min_margin", 0.08),
        identify_workers=_pick_int("IDENTIFY_WORKERS", opts, "identify_workers", 2),
        identify_timeout=_pick_int("IDENTIFY_TIMEOUT", opts, "identify_timeout", 60),
        identify_retain_days=_pick_int("IDENTIFY_RETAIN_DAYS", opts, "identify_retain_days", 14),
        identify_use_audio_priors=_as_bool(
            _pick("IDENTIFY_USE_AUDIO_PRIORS", opts, "identify_use_audio_priors", "true")),
        identify_exclude_blacklisted=_as_bool(
            _pick("IDENTIFY_EXCLUDE_BLACKLISTED", opts, "identify_exclude_blacklisted", "true")),
        # Matches the explicit `path:` on the homeassistant_config map entry.
        ha_config_dir=os.environ.get("HA_CONFIG_DIR", "/homeassistant"),
        log_level=_pick("LOG_LEVEL", opts, "log_level", "info").lower(),
    )
