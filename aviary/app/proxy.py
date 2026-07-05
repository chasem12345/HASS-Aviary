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

_client: Optional[httpx.AsyncClient] = None


def init_client() -> None:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def stream_upstream(request: Request, url: str, fallbacks: tuple[str, ...] = ()):
    """Proxy ``url``, forwarding Range and relaying media headers as a streaming response.

    ``fallbacks`` are tried in order when a URL errors or returns >= 400 (used for
    BirdNET-Go, where the working audio endpoint differs across versions).
    """
    if _client is None:
        return JSONResponse({"error": "proxy client not initialized"}, status_code=500)

    fwd_headers = {}
    if "range" in request.headers:
        fwd_headers["Range"] = request.headers["range"]

    urls = (url, *fallbacks)
    resp = None
    for i, candidate in enumerate(urls):
        upstream = _client.build_request("GET", candidate, headers=fwd_headers)
        try:
            attempt = await _client.send(upstream, stream=True)
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


async def call_upstream(method: str, url: str, json: Optional[dict] = None) -> tuple[int, str]:
    """One-off upstream API call (e.g. deleting an event). Returns (status, body[:200]).

    Raises httpx.HTTPError on transport failure; callers surface it to the UI.
    """
    if _client is None:
        raise httpx.TransportError("proxy client not initialized")
    resp = await _client.request(method, url, json=json)
    return resp.status_code, resp.text[:200]


# ------------------------------------------------------------------- URL builders

def frigate_event_api_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}"


def frigate_sub_label_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/sub_label"


def birdnet_detection_url(base: str, native_id: str) -> str:
    return f"{base}/api/v2/detections/{quote(str(native_id), safe='')}"


def frigate_clip_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/clip.mp4"


def frigate_snapshot_url(base: str, event_id: str, thumbnail: bool = False) -> str:
    kind = "thumbnail.jpg" if thumbnail else "snapshot.jpg"
    return f"{base}/api/events/{event_id}/{kind}"


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
