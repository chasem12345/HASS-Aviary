"""Life list on iNaturalist: one observation per species, from its first sighting.

The user keeps their life list on iNaturalist; Aviary's job is to get a species there
once, with evidence, and then point at it. eBird was the obvious alternative and has no
write API at all (media is drag-and-drop on a checklist), so iNaturalist — whose API
creates observations and attaches photos and sounds — is where a synced list can live.

What gets posted, per species, exactly once:

* the **first sighting** Aviary can still show — the kept "first" keepsake when there is
  one (its media survives Frigate's retention), else the oldest camera sighting with
  media, else the oldest event with a stored crop, else the oldest BirdNET-Go detection
  for a species only ever heard. The observation's date is that sighting's, so the
  evidence and the record never disagree;
* the Home Assistant location, with ``inat_geoprivacy`` (default *obscured*: it is the
  user's house) and a yard-sized positional accuracy;
* a photo (the bird's own stored crop, else Frigate's snapshot) and/or the BirdNET-Go
  clip. iNaturalist takes photos and sounds, not video, so a still stands in for the clip;
* a description that says honestly where the record came from.

**Manual by default.** An observation is published under the user's name, and iNaturalist
welcomes reviewed camera-trap records, not unattended automation — so the species page
has a *Post to iNaturalist* button, and ``inat_auto_post`` (off) only ever fires after a
species is confirmed (or first recorded, with the review gate off).

Auth is iNaturalist's OAuth password grant: an add-on has no browser for the interactive
flow, the grant yields a 24-hour JWT for the v1 API and no refresh token, so the four
credentials live in the add-on options and are traded for a fresh token when needed.
Nothing here logs or returns them. Soft-failure rule applies: nothing in this module may
break ingest or a page render.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from datetime import datetime, tzinfo
from typing import Optional

import httpx

from . import crops, db, heroes, http, proxy, solar, species_info

log = logging.getLogger("aviary.inat")

OAUTH_TOKEN_URL = "https://www.inaturalist.org/oauth/token"
API_TOKEN_URL = "https://www.inaturalist.org/users/api_token"
API_V1 = "https://api.inaturalist.org/v1"
OBS_URL = "https://www.inaturalist.org/observations/{}"
REPO_URL = "https://github.com/chasem12345/HASS-Aviary"

# A JWT with no readable exp claim is assumed to last iNaturalist's documented ~24 h.
_JWT_FALLBACK_TTL_S = 23 * 3600
# Refresh a little before expiry so a post never starts with a token about to lapse.
_JWT_REFRESH_MARGIN_S = 300
# Auto-post waits for the identifier's result and the keepsake pin to land before
# choosing the sighting to post — a first detection is still being finished then.
_AUTO_DEBOUNCE_S = 60.0

_settings = None
_http = http.register(
    "inat", timeout=httpx.Timeout(30.0), follow_redirects=True,
    headers={"User-Agent": species_info.USER_AGENT, "Accept": "application/json"},
)
_loop: Optional[asyncio.AbstractEventLoop] = None
_pending: set[str] = set()
_pending_lock = threading.Lock()
_drain_task: Optional[asyncio.Task] = None
_post_lock: Optional[asyncio.Lock] = None
_jwt: Optional[str] = None
_jwt_exp: float = 0.0


def configure(settings) -> None:
    global _settings, _jwt, _jwt_exp
    _settings = settings
    _jwt, _jwt_exp = None, 0.0


def enabled() -> bool:
    return bool(_settings and getattr(_settings, "inat_enabled", False))


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def _lock() -> asyncio.Lock:
    global _post_lock
    if _post_lock is None:
        _post_lock = asyncio.Lock()
    return _post_lock


# ------------------------------------------------------------------ pure builders

def observed_on_string(epoch: float, tz: Optional[tzinfo] = None) -> str:
    """ISO 8601 with offset, e.g. ``2026-06-15T12:00:00-05:00`` — what iNaturalist's own
    apps send. Container local time by default (the add-on runs in HA's zone)."""
    if tz is not None:
        return datetime.fromtimestamp(float(epoch), tz).isoformat(timespec="seconds")
    return datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")


def description(sighting: dict) -> str:
    """Provenance, stated plainly: a curator should know this is a camera-trap record."""
    conf = sighting.get("confidence")
    pct = f", classifier confidence {float(conf) * 100:.0f}%" if conf is not None else ""
    if sighting.get("source") == "birdnet":
        what = (f"BirdNET-Go audio detection from a backyard microphone"
                f"{' (' + sighting['location'] + ')' if sighting.get('location') else ''}{pct}.")
    else:
        zone = sighting.get("zone")
        where = f" on camera '{sighting['location']}'" if sighting.get("location") else ""
        where += f" (zone {str(zone).replace('_', ' ')})" if zone else ""
        what = f"Frigate camera-trap event{where}{pct}."
        if sighting.get("id_status") == "ok":
            what += " Species identified by the aviary-id image classifier."
    return (f"Recorded automatically by Aviary, a Home Assistant add-on for backyard bird "
            f"monitoring, and posted by its owner after review. {what} {REPO_URL}")


def build_observation(sighting: dict, loc: solar.Location, settings, taxon_id: Optional[int],
                      scientific_name: Optional[str], tz: Optional[tzinfo] = None) -> dict:
    """The ``POST /v1/observations`` body for one sighting at the home location.

    ``taxon_id`` wins when known; otherwise iNaturalist resolves ``species_guess`` (the
    scientific name if we have it, else the common name) itself.
    """
    obs: dict = {
        "observed_on_string": observed_on_string(sighting["start_time"], tz),
        "latitude": round(float(loc.lat), 6),
        "longitude": round(float(loc.lon), 6),
        "positional_accuracy": int(getattr(settings, "inat_positional_accuracy_m", 30)),
        "geoprivacy": getattr(settings, "inat_geoprivacy", "obscured"),
        "captive_flag": False,
        "description": description(sighting),
    }
    if getattr(loc, "time_zone", ""):
        obs["time_zone"] = loc.time_zone
    if taxon_id:
        obs["taxon_id"] = int(taxon_id)
    else:
        obs["species_guess"] = (scientific_name or sighting.get("scientific_name")
                                or sighting["common_name"])
    return {"observation": obs}


def media_plan(sighting: dict) -> list[tuple[str, str]]:
    """What evidence to attach, best first: ``("photo", "crop")`` (the bird's own stored
    crop), ``("photo", "snapshot")`` (Frigate's), or ``("sound", "birdnet")``."""
    plan: list[tuple[str, str]] = []
    if sighting.get("source") == "birdnet":
        if sighting.get("native_id") or sighting.get("clip_ref"):
            plan.append(("sound", "birdnet"))
        return plan
    ref = sighting.get("source_ref") or ""
    if ref and crops.path_if_exists(ref, int(sighting.get("subject_idx") or 0)):
        plan.append(("photo", "crop"))
    elif sighting.get("has_snapshot") and ref:
        plan.append(("photo", "snapshot"))
    return plan


def pick_sighting(name: str) -> Optional[dict]:
    """The sighting to post for a species: the full detections row, plus ``subject_idx``.

    Order: the kept *first* keepsake (retained, so its media is still at Frigate), the
    oldest camera sighting with media, the species' hero picture (a stored crop survives
    Frigate's expiry), then the oldest BirdNET-Go detection for a species only heard.
    """
    def full(row: Optional[dict]) -> Optional[dict]:
        if not row:
            return None
        det = db.detection_by_id(int(row["id"]))
        if not det:
            return None
        out = dict(det)
        out["subject_idx"] = int(row.get("subject_idx") or 0)
        out["common_name"] = name
        return out

    kept = db.keepsake(name, "first")
    if kept:
        found = full(db.sighting_by_detection(int(kept["detection_id"]), name))
        if found:
            return found
    found = full(db.oldest_sighting(name))
    if found:
        return found
    hero = heroes.for_species([name]).get(name)
    if hero and hero.get("id") is not None:
        found = full({"id": hero["id"], "subject_idx": hero.get("subject_idx") or hero.get("idx") or 0})
        if found:
            return found
    audio = db.oldest_detection(name, "birdnet")
    if audio:
        audio = dict(audio)
        audio["subject_idx"] = 0
        return audio
    return None


# ------------------------------------------------------------------------- HTTP

def _jwt_expiry(token: str, now: float) -> float:
    """The JWT's ``exp`` claim (unsigned decode of the payload), else ~23 h from now."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = float(data["exp"])
        if exp > now:
            return exp
    except (IndexError, ValueError, KeyError, TypeError):
        pass
    return now + _JWT_FALLBACK_TTL_S


async def _oauth_access_token() -> str:
    assert _http.client is not None and _settings is not None
    resp = await _http.client.post(OAUTH_TOKEN_URL, data={
        "client_id": _settings.inat_app_id, "client_secret": _settings.inat_app_secret,
        "grant_type": "password", "username": _settings.inat_username,
        "password": _settings.inat_password,
    })
    if resp.status_code != 200:
        raise RuntimeError(f"iNaturalist login failed ({resp.status_code}); check the four inat_* options")
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("iNaturalist login returned no access token")
    return token


async def _api_token(access_token: str) -> str:
    assert _http.client is not None
    resp = await _http.client.get(API_TOKEN_URL, headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code != 200 or not resp.json().get("api_token"):
        raise RuntimeError(f"iNaturalist API token request failed ({resp.status_code})")
    return resp.json()["api_token"]


async def jwt(force: bool = False) -> str:
    """The cached v1 API token, refreshed when near expiry (or on ``force`` after a 401)."""
    global _jwt, _jwt_exp
    if _http.client is None:
        raise RuntimeError("iNaturalist client not initialized")
    now = time.time()
    if force or not _jwt or now > _jwt_exp - _JWT_REFRESH_MARGIN_S:
        _jwt = await _api_token(await _oauth_access_token())
        _jwt_exp = _jwt_expiry(_jwt, now)
    return _jwt


async def _v1(method: str, path: str, **kwargs) -> httpx.Response:
    """One v1 API call with the JWT; a 401 refreshes the token and retries once."""
    assert _http.client is not None
    for attempt in (0, 1):
        token = await jwt(force=bool(attempt))
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        resp = await _http.client.request(method, f"{API_V1}{path}", headers=headers, **kwargs)
        if resp.status_code != 401:
            return resp
    return resp


async def _create_observation(payload: dict) -> tuple[int, str]:
    resp = await _v1("POST", "/observations", json=payload)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"iNaturalist rejected the observation ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    obs_id = data.get("id") or (data.get("results") or [{}])[0].get("id")
    if not obs_id:
        raise RuntimeError("iNaturalist returned no observation id")
    return int(obs_id), OBS_URL.format(obs_id)


async def _upload(kind: str, obs_id: int, filename: str, content: bytes, mime: str) -> Optional[str]:
    """Attach one file; returns an error string (recorded, never raised) or None."""
    field = "observation_photo" if kind == "photo" else "observation_sound"
    resp = await _v1("POST", f"/{field}s", data={f"{field}[observation_id]": str(obs_id)},
                     files={"file": (filename, content, mime)})
    if resp.status_code not in (200, 201):
        return f"{kind} upload failed ({resp.status_code}): {resp.text[:120]}"
    return None


async def _upload_photo(obs_id: int, filename: str, content: bytes, mime: str) -> Optional[str]:
    return await _upload("photo", obs_id, filename, content, mime)


async def _upload_sound(obs_id: int, filename: str, content: bytes, mime: str) -> Optional[str]:
    return await _upload("sound", obs_id, filename, content, mime)


async def _fetch_bytes(url: str, fallbacks: tuple[str, ...] = ()) -> Optional[tuple[bytes, str]]:
    return await proxy.fetch_bytes(url, fallbacks)


_AUDIO_EXT = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
              "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/flac": ".flac",
              "audio/x-flac": ".flac", "audio/mp4": ".m4a", "audio/ogg": ".ogg"}


async def _media_bytes(sighting: dict, item: tuple[str, str]) -> Optional[tuple[str, bytes, str]]:
    """(filename, bytes, mime) for one media_plan item, or None when it can't be had."""
    kind, origin = item
    slug = "".join(c if c.isalnum() else "-" for c in sighting["common_name"].lower()).strip("-")
    if origin == "crop":
        path = crops.path_if_exists(sighting["source_ref"], int(sighting.get("subject_idx") or 0))
        if not path:
            return None
        with open(path, "rb") as f:
            return f"{slug}-crop.jpg", f.read(), "image/jpeg"
    if origin == "snapshot":
        base = getattr(_settings, "frigate_url", "")
        if not base:
            return None
        got = await _fetch_bytes(proxy.frigate_snapshot_url(base, sighting["source_ref"]))
        return (f"{slug}-snapshot.jpg", got[0], got[1].split(";")[0] or "image/jpeg") if got else None
    if origin == "birdnet":
        base = getattr(_settings, "birdnet_url", "")
        urls = proxy.birdnet_audio_urls(base, sighting) if base else []
        if not urls:
            return None
        got = await _fetch_bytes(urls[0], tuple(urls[1:]))
        if not got:
            return None
        mime = got[1].split(";")[0].strip() or "audio/wav"
        return f"{slug}{_AUDIO_EXT.get(mime, '.wav')}", got[0], mime
    return None


# -------------------------------------------------------------------- orchestration

async def _taxon(name: str, scientific: Optional[str]) -> Optional[int]:
    """The cached iNaturalist taxon id — but only when it plainly belongs to this species.
    A common-name lookup can land on a look-alike, so when both we and the cache know a
    scientific name they must agree; otherwise iNaturalist gets ``species_guess``."""
    try:
        tid = await species_info.taxon_id(name, scientific)
    except Exception:  # noqa: BLE001 - a lookup failure just means species_guess
        return None
    if not tid:
        return None
    info = db.get_species_info(name) or {}
    known = (info.get("scientific_name") or "").strip().lower()
    if scientific and known and known != scientific.strip().lower():
        return None
    return int(tid)


def _location_label() -> str:
    gp = getattr(_settings, "inat_geoprivacy", "obscured")
    acc = getattr(_settings, "inat_positional_accuracy_m", 30)
    return f"Home Assistant location, {gp}, ±{acc} m"


async def preview(name: str) -> dict:
    """What a post would send, for the confirmation dialog. Also reports current state."""
    row = db.inat_get(name)
    out = {"configured": enabled(), "species": name,
           "posted": bool(row and row.get("status") == "posted"),
           "observation_url": (row or {}).get("observation_url"),
           "last_error": (row or {}).get("error")}
    if not enabled():
        out["error"] = "iNaturalist is not configured (see the add-on options)"
        return out
    if solar.location() is None:
        out["error"] = "Home Assistant's location is unknown; an observation needs coordinates"
        return out
    sighting = await asyncio.to_thread(pick_sighting, name)
    if not sighting:
        out["error"] = "no sighting with media to post"
        return out
    scientific = sighting.get("scientific_name") or await asyncio.to_thread(db.scientific_name_for, name)
    taxon = await _taxon(name, scientific)
    labels = {"crop": "photo (stored crop)", "snapshot": "photo (Frigate snapshot)",
              "birdnet": "sound (BirdNET-Go clip)"}
    out.update({
        "observed": observed_on_string(sighting["start_time"]),
        "detection_id": sighting.get("id"),
        "location": _location_label(),
        "media": [labels[o] for _, o in media_plan(sighting)],
        "taxon": f"iNaturalist taxon {taxon}" if taxon else f"by name: {scientific or name}",
        "scientific_name": scientific,
    })
    return out


async def post_species(name: str) -> dict:
    """Create the species' observation. Idempotent: a species already posted is refused
    (forget the link first to post again). Never raises; failures are recorded."""
    if not enabled():
        return {"ok": False, "error": "iNaturalist is not configured"}
    async with _lock():
        existing = await asyncio.to_thread(db.inat_get, name)
        if existing and existing.get("status") == "posted":
            return {"ok": False, "error": "already on iNaturalist",
                    "observation_url": existing.get("observation_url")}
        gated = getattr(_settings, "require_species_confirmation", False)
        if gated and not await asyncio.to_thread(db.is_species_confirmed, name):
            return {"ok": False, "error": "confirm the species first"}
        loc = solar.location()
        if loc is None:
            return {"ok": False, "error": "Home Assistant's location is unknown"}
        sighting = await asyncio.to_thread(pick_sighting, name)
        if not sighting:
            return {"ok": False, "error": "no sighting with media to post"}
        scientific = sighting.get("scientific_name") or await asyncio.to_thread(db.scientific_name_for, name)
        try:
            taxon = await _taxon(name, scientific)
            payload = build_observation(sighting, loc, _settings, taxon, scientific)
            obs_id, url = await _create_observation(payload)
        except Exception as exc:  # noqa: BLE001 - recorded for the page, never raised
            err = str(exc)[:300]
            log.warning("iNaturalist post of %s failed: %s", name, err)
            await asyncio.to_thread(db.inat_set, {"common_name": name, "status": "failed", "error": err})
            await asyncio.to_thread(db.set_pref, "inat_last_error", f"{name}: {err}")
            return {"ok": False, "error": err}
        # Record the observation BEFORE uploading media: a crash mid-upload must never
        # lead to a second observation of the same species.
        row = {"common_name": name, "observation_id": obs_id, "observation_url": url,
               "taxon_id": taxon, "detection_id": sighting.get("id"),
               "subject_idx": sighting.get("subject_idx") or 0,
               "observed_at": sighting.get("start_time"), "media": "", "status": "posted",
               "error": None, "posted_at": time.time()}
        await asyncio.to_thread(db.inat_set, row)
        attached: list[str] = []
        errors: list[str] = []
        for item in media_plan(sighting):
            try:
                got = await _media_bytes(sighting, item)
                if not got:
                    errors.append(f"{item[0]} unavailable")
                    continue
                err = await (_upload_photo if item[0] == "photo" else _upload_sound)(obs_id, *got)
                if err:
                    errors.append(err)
                else:
                    attached.append(item[0])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{item[0]}: {str(exc)[:120]}")
        row["media"] = ",".join(attached)
        row["error"] = "; ".join(errors) or None
        await asyncio.to_thread(db.inat_set, row)
        log.info("Posted %s to iNaturalist as observation %s (media: %s)", name, obs_id,
                 row["media"] or "none")
        return {"ok": True, "species": name, "observation_url": url, "media": attached,
                "error": row["error"]}


async def check_account() -> dict:
    """Whether the credentials work: the account's login, or the error. No secrets."""
    if not enabled():
        return {"ok": False, "error": "not configured"}
    try:
        resp = await _v1("GET", "/users/me")
        if resp.status_code != 200:
            return {"ok": False, "error": f"users/me returned {resp.status_code}"}
        results = resp.json().get("results") or []
        return {"ok": True, "login": (results[0] if results else {}).get("login")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


async def status() -> dict:
    """Settings-page summary. Never includes a credential."""
    out = {
        "configured": enabled(),
        "auto_post": bool(getattr(_settings, "inat_auto_post", False)),
        "geoprivacy": getattr(_settings, "inat_geoprivacy", "obscured"),
        "posted": len(await asyncio.to_thread(db.inat_all)),
        "last_error": await asyncio.to_thread(db.get_pref, "inat_last_error", ""),
        "location_known": solar.location() is not None,
    }
    if enabled():
        out["account"] = await check_account()
    return out


# ------------------------------------------------------------------------ triggers

def schedule(name: Optional[str]) -> None:
    """Queue a species for an automatic post shortly. Thread-safe; never raises."""
    if not enabled() or not name or name.strip().lower() == db.UNNAMED:
        return
    with _pending_lock:
        _pending.add(name.strip())
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(_ensure_drain)
    except RuntimeError:
        pass  # loop shutting down


def _ensure_drain() -> None:
    global _drain_task
    if _drain_task is None or _drain_task.done():
        _drain_task = asyncio.get_running_loop().create_task(_drain())


async def _drain() -> None:
    await asyncio.sleep(_AUTO_DEBOUNCE_S)
    while True:
        with _pending_lock:
            names = sorted(_pending)
            _pending.clear()
        if not names:
            return
        for name in names:
            result = await post_species(name)
            if not result.get("ok") and result.get("error") != "already on iNaturalist":
                log.info("Auto-post of %s skipped: %s", name, result.get("error"))


def on_confirmed(name: str) -> None:
    """A species just entered the registry: post it, if the user asked for that."""
    if getattr(_settings, "inat_auto_post", False):
        schedule(name)


def on_new_species(name: str) -> None:
    """First-ever live detection of a species (review gate off): post it, if asked."""
    if getattr(_settings, "inat_auto_post", False) and not getattr(
            _settings, "require_species_confirmation", False):
        schedule(name)
