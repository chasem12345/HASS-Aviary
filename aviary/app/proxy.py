"""Range-aware streaming proxy for Frigate/BirdNET-Go media.

Clips and snapshots are fetched live from the source and streamed back to the browser.
The incoming ``Range`` header is forwarded and the source's ``Content-Range`` /
``Accept-Ranges`` / ``206`` response is relayed, so ``<video>`` seeking works.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import httpx

from . import http
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("aviary.proxy")

# Headers worth relaying from the upstream response to the client.
_PASS_RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "cache-control",
    "last-modified",
    "etag",
)


def _reset_csrf() -> None:
    # The token belongs to the closed client's cookie jar; keeping it would pair a live
    # header with a cookie the next client doesn't have.
    global _csrf_token_cache
    _csrf_token_cache = None


_http = http.register(
    "proxy", timeout=httpx.Timeout(30.0, read=None), follow_redirects=True, on_close=_reset_csrf,
)


async def stream_upstream(
    request: Request,
    url: str,
    fallbacks: tuple[str, ...] = (),
    headers: Optional[dict[str, str]] = None,
):
    """Proxy ``url``, forwarding Range and relaying media headers as a streaming response.

    ``fallbacks`` are tried in order when a URL errors or returns >= 400 (used for
    BirdNET-Go, where the working audio endpoint differs across versions). ``headers``
    adds request headers for the upstream call — the client sets none by default, and
    public APIs (e.g. iNaturalist) ask to be sent a descriptive User-Agent.
    """
    if _http.client is None:
        return JSONResponse({"error": "proxy client not initialized"}, status_code=500)

    fwd_headers = dict(headers or {})
    if "range" in request.headers:
        fwd_headers["Range"] = request.headers["range"]

    urls = (url, *fallbacks)
    resp = None
    for i, candidate in enumerate(urls):
        upstream = _http.client.build_request("GET", candidate, headers=fwd_headers)
        try:
            attempt = await _http.client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            log.warning("Upstream fetch failed for %s: %s", candidate, exc)
            continue
        if attempt.status_code >= 400 and i < len(urls) - 1:
            log.debug("Upstream %s returned %s; trying fallback", candidate, attempt.status_code)
            await attempt.aclose()
            continue
        resp = attempt
        break
    if resp is None:
        return JSONResponse({"error": "upstream unreachable"}, status_code=502)

    out_headers = {
        k: v for k, v in resp.headers.items() if k.lower() in _PASS_RESPONSE_HEADERS
    }

    async def body_iter():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=resp.status_code,
        headers=out_headers,
        media_type=resp.headers.get("content-type"),
    )


async def fetch_to_file(url: str, dest: str) -> bool:
    """Stream an upstream URL into a local file. Returns False on any failure."""
    if _http.client is None:
        return False
    try:
        async with _http.client.stream("GET", url) as resp:
            if resp.status_code != 200:
                return False
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
        return True
    except (httpx.HTTPError, OSError) as exc:
        log.warning("Download from %s failed: %s", url, exc)
        return False


# Largest media body fetch_bytes will hold in memory — a Frigate snapshot is ~100 KB, a
# BirdNET-Go clip a few MB; anything bigger is not something we want to re-upload anyway.
_FETCH_BYTES_MAX = 15 * 1024 * 1024


async def fetch_bytes(url: str, fallbacks: tuple[str, ...] = ()
                      ) -> Optional[tuple[bytes, str]]:
    """GET a media file whole: (body, content-type), or None if every candidate failed.

    Same fallback semantics as ``stream_upstream`` — used where the bytes are needed in
    hand rather than relayed (re-uploading a snapshot or an audio clip elsewhere).
    """
    if _http.client is None:
        return None
    for candidate in (url, *fallbacks):
        try:
            async with _http.client.stream("GET", candidate) as resp:
                if resp.status_code >= 400:
                    continue
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > _FETCH_BYTES_MAX:
                        log.warning("Media at %s exceeds %d bytes; skipped", candidate, _FETCH_BYTES_MAX)
                        break
                    chunks.append(chunk)
                else:
                    return b"".join(chunks), resp.headers.get("content-type", "application/octet-stream")
        except httpx.HTTPError as exc:
            log.warning("Fetch of %s failed: %s", candidate, exc)
    return None


async def call_upstream(method: str, url: str, json: Optional[dict] = None,
                        headers: Optional[dict[str, str]] = None) -> tuple[int, str]:
    """One-off upstream API call (e.g. deleting an event). Returns (status, body[:200]).

    Raises httpx.HTTPError on transport failure; callers surface it to the UI.
    """
    if _http.client is None:
        raise httpx.TransportError("proxy client not initialized")
    resp = await _http.client.request(method, url, json=json, headers=headers)
    return resp.status_code, resp.text[:200]


async def get_json(url: str) -> tuple[int, Optional[object]]:
    """GET a JSON API endpoint, whole body. Returns (status, parsed or None).

    call_upstream deliberately truncates bodies to 200 chars (they only feed error
    messages); this is for endpoints whose full response is the point, e.g. Frigate's
    exports list. Raises httpx.HTTPError on transport failure like call_upstream.
    """
    if _http.client is None:
        raise httpx.TransportError("proxy client not initialized")
    resp = await _http.client.get(url)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


# ------------------------------------------------------------------ BirdNET-Go CSRF
# BirdNET-Go guards state-changing API calls (e.g. DELETE /api/v2/detections/<id>) with
# Echo's CSRF middleware: it compares an `X-CSRF-Token` header against a `csrf` cookie.
# Without both, the request fails with 403 {"message":"Invalid CSRF token"}.
#
# The token is minted by GET /api/v2/app/config, which calls EnsureCSRFToken() precisely
# so a non-browser client can obtain one (Echo's Sec-Fetch-Site optimisation would
# otherwise skip generating it). The cookie half is handled for us: the shared client
# keeps a cookie jar, so it is replayed automatically on the follow-up request.
_CSRF_COOKIE = "csrf"
_csrf_token_cache: Optional[str] = None


async def birdnet_csrf_token(base: str, refresh: bool = False) -> Optional[str]:
    """A CSRF token for BirdNET-Go, or None if one couldn't be obtained.

    Cached, because the token is stable for the life of the cookie jar. ``refresh``
    re-fetches, for retrying a 403 after BirdNET-Go rotated tokens (e.g. it restarted
    since we cached ours). Never raises: a failure here should degrade to attempting the
    call without a token, not break deletion outright.
    """
    global _csrf_token_cache
    if _csrf_token_cache and not refresh:
        return _csrf_token_cache
    if _http.client is None:
        return None
    if refresh:
        # Drop the stale pair; the jar would otherwise keep replaying the old cookie.
        _http.client.cookies.delete(_CSRF_COOKIE)
        _csrf_token_cache = None
    try:
        await _http.client.get(birdnet_app_config_url(base))
    except httpx.HTTPError as exc:
        log.debug("Could not reach BirdNET-Go for a CSRF token: %s", exc)
        return None
    _csrf_token_cache = _http.client.cookies.get(_CSRF_COOKIE)
    if not _csrf_token_cache:
        log.debug("BirdNET-Go returned no %s cookie; deleting may fail.", _CSRF_COOKIE)
    return _csrf_token_cache


# ------------------------------------------------------------------- URL builders

def frigate_event_api_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}"


def frigate_sub_label_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/sub_label"


def frigate_retain_url(base: str, event_id: str) -> str:
    """POST sets the event's retain_indefinitely flag; DELETE clears it."""
    return f"{base}/api/events/{event_id}/retain"


def birdnet_detection_url(base: str, native_id: str) -> str:
    return f"{base}/api/v2/detections/{quote(str(native_id), safe='')}"


def birdnet_app_config_url(base: str) -> str:
    """The endpoint that mints a CSRF token (see ``birdnet_csrf_token``)."""
    return f"{base}/api/v2/app/config"


def frigate_clip_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/clip.mp4"


def frigate_snapshot_url(base: str, event_id: str, thumbnail: bool = False) -> str:
    kind = "thumbnail.jpg" if thumbnail else "snapshot.jpg"
    return f"{base}/api/events/{event_id}/{kind}"


def frigate_recordings_url(base: str, camera: str, start: float, end: float) -> str:
    """A recordings window from any recorded camera, event or not.

    Same endpoint (and the same 3-decimal formatting) the aviary-id service uses for
    the cross-camera zoom — see aviary-id's frames.recordings_url.
    """
    return f"{base}/api/{camera}/start/{start:.3f}/end/{end:.3f}/clip.mp4"


def frigate_vod_url(base: str, camera: str, start: float, end: float, filename: str) -> str:
    """One file of the HLS rendition of a recordings window, from Frigate's nginx.

    ``/vod/{camera}/start/{s}/end/{e}/master.m3u8`` is what Frigate's own recordings UI
    plays: nginx-vod-module assembles a VOD playlist over the recording segments, so
    playback starts after one segment instead of after the whole window has been
    concatenated. Frigate configures ``vod_base_url ''`` and ``vod_segments_base_url ''``,
    which makes every URI inside the playlists RELATIVE (``index-v1-a1.m3u8``,
    ``init-v1-a1.mp4``, ``seg-3-v1-a1.m4s``). Aviary's proxy route mirrors this exact path
    shape, so the browser resolves those siblings under the ingress prefix on its own and
    no playlist body is ever rewritten. Should a Frigate release set a base URL, that
    assumption is the thing to check first.
    """
    return f"{base}/vod/{camera}/start/{start:.3f}/end/{end:.3f}/{filename}"


def frigate_export_url(base: str, camera: str, start: float, end: float) -> str:
    """POST here creates an export of the window — recordings that survive retention.

    Body: {"source": "recordings", "name": ...}; Frigate 0.18 answers 202 with
    {"export_id": ...} and processes asynchronously.
    """
    return f"{base}/api/export/{camera}/start/{start:.3f}/end/{end:.3f}"


def frigate_exports_url(base: str) -> str:
    """GET lists exports: [{id, camera, name, video_path, in_progress, ...}]."""
    return f"{base}/api/exports"


def frigate_exports_delete_url(base: str) -> str:
    """POST {"ids": [...]} deletes exports (0.18 dropped the DELETE-verb endpoint)."""
    return f"{base}/api/exports/delete"


def frigate_export_video_url(base: str, filename: str) -> str:
    """The finished export file, served statically by Frigate's nginx."""
    return f"{base}/exports/{filename}"


def birdnet_audio_urls(base: str, det: dict) -> list[str]:
    """Candidate URLs for a BirdNET-Go detection's audio, best first.

    BirdNET-Go serves audio only through its API (there is no static clips path):
    nightlies have by-id ``/api/v2/audio/{id}`` (Range-capable) and by-filename
    ``/api/v2/media/audio/{filename}``; stable v0.6.4 only has
    ``/api/v1/media/audio?clip={ClipName}``. Try newest first and fall through.
    """
    urls: list[str] = []
    if det.get("native_id"):
        urls.append(f"{base}/api/v2/audio/{det['native_id']}")
    clip_ref = det.get("clip_ref")
    if clip_ref:
        if clip_ref.startswith(("http://", "https://")):
            urls.append(clip_ref)
        else:
            filename = clip_ref.replace("\\", "/").split("/")[-1]
            urls.append(f"{base}/api/v2/media/audio/{quote(filename)}")
            urls.append(f"{base}/api/v1/media/audio?clip={quote(clip_ref, safe='')}")
    return urls


def birdnet_species_image_url(base: str, scientific_name: str) -> str:
    """Generic species photo served by BirdNET-Go's image cache (Wikipedia/AviCommons).

    Available on current BirdNET-Go builds only; older ones (e.g. v0.6.4) 404 and the
    templates fall back to their emoji placeholder via ``onerror``.
    """
    return f"{base}/api/v2/media/species-image?name={quote(scientific_name, safe='')}"


def birdnet_spectrogram_urls(base: str, det: dict) -> list[str]:
    """Candidate URLs for a detection's spectrogram PNG, best first (see audio note)."""
    urls: list[str] = []
    if det.get("native_id"):
        urls.append(f"{base}/api/v2/spectrogram/{det['native_id']}?size=md")
    clip_ref = det.get("clip_ref")
    if clip_ref and not clip_ref.startswith(("http://", "https://")):
        filename = clip_ref.replace("\\", "/").split("/")[-1]
        urls.append(f"{base}/api/v2/media/spectrogram/{quote(filename)}")
        urls.append(f"{base}/api/v1/media/spectrogram?clip={quote(clip_ref, safe='')}")
    return urls
