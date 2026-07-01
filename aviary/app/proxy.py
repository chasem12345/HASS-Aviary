"""Range-aware streaming proxy for Frigate/BirdNET-Go media.

Clips and snapshots are fetched live from the source and streamed back to the browser.
The incoming ``Range`` header is forwarded and the source's ``Content-Range`` /
``Accept-Ranges`` / ``206`` response is relayed, so ``<video>`` seeking works.
"""

from __future__ import annotations

import logging
from typing import Optional

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


async def stream_upstream(request: Request, url: str) -> StreamingResponse:
    """Proxy ``url``, forwarding Range and relaying media headers as a streaming response."""
    if _client is None:
        return JSONResponse({"error": "proxy client not initialized"}, status_code=500)

    fwd_headers = {}
    if "range" in request.headers:
        fwd_headers["Range"] = request.headers["range"]

    upstream = _client.build_request("GET", url, headers=fwd_headers)
    try:
        resp = await _client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        log.warning("Upstream fetch failed for %s: %s", url, exc)
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


# ------------------------------------------------------------------- URL builders

def frigate_clip_url(base: str, event_id: str) -> str:
    return f"{base}/api/events/{event_id}/clip.mp4"


def frigate_snapshot_url(base: str, event_id: str, thumbnail: bool = False) -> str:
    kind = "thumbnail.jpg" if thumbnail else "snapshot.jpg"
    return f"{base}/api/events/{event_id}/{kind}"


def birdnet_clip_url(base: str, clip_ref: str) -> str:
    """Resolve a BirdNET-Go audio clip reference to a URL.

    BirdNET-Go serves exported clips under its ``/clips/`` path. If the stored ref is
    already an absolute URL we use it as-is; otherwise we treat it as a filename.
    """
    if clip_ref.startswith(("http://", "https://")):
        return clip_ref
    filename = clip_ref.replace("\\", "/").split("/")[-1]
    return f"{base}/clips/{filename}"
