"""Give the probe something to learn from before any bird has been confirmed.

The awkward part of a few-shot classifier is the first shot. A fresh install has no
confirmed detections, so every centroid is empty and the probe contributes nothing until
the user has patiently confirmed a dozen birds.

Aviary already has a way out: it caches **iNaturalist reference photos** for every species
in the registry, so a human can compare a questionable detection against known pictures of
the bird. Those are labelled images of exactly the right species. Running them through the
identification service's ``/identify/image`` endpoint yields an embedding apiece, and every
species starts with a usable centroid on day one.

They are a starting point, not the destination. A posed photo in good light is the right
species in the wrong domain, which is why ``probe._REFERENCE_WEIGHT`` discounts them and
your own confirmed frames displace them as they accumulate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from . import db, probe

log = logging.getLogger("aviary.bootstrap")

# Gentle on purpose. This is background work with no deadline, competing with real
# identifications for one GPU, and hitting a public photo host. A species or two a second
# is plenty when the whole job runs once.
_BATCH = 25
_PAUSE = 0.4

_task: Optional[asyncio.Task] = None


async def _embed_one(client: httpx.AsyncClient, settings, photo: dict) -> Optional[str]:
    """Fetch one reference photo and ask the service for its embedding."""
    url = photo.get("file_url") or photo.get("thumb_url")
    if not url:
        return None
    try:
        # iNaturalist asks API users to identify themselves; the same courtesy the rest of
        # Aviary's outbound calls extend.
        img = await client.get(url, headers={"User-Agent": "Aviary/HomeAssistant add-on"})
        if img.status_code != 200 or not img.content:
            return None
    except httpx.HTTPError as exc:
        log.debug("Reference photo fetch failed for %s: %s", photo.get("common_name"), exc)
        return None

    headers = {}
    if settings.identify_token:
        headers["Authorization"] = f"Bearer {settings.identify_token}"
    try:
        resp = await client.post(
            f"{settings.identify_url}/identify/image",
            files={"file": ("reference.jpg", img.content, "image/jpeg")},
            headers=headers,
        )
        if resp.status_code != 200:
            log.debug("Service rejected a reference photo: HTTP %s", resp.status_code)
            return None
        return resp.json().get("embedding")
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("Reference embedding failed for %s: %s", photo.get("common_name"), exc)
        return None


async def run(settings, model: str, limit: int = 500) -> dict:
    """Embed reference photos that don't have an embedding yet for ``model``.

    Idempotent and resumable: it only ever asks for the gaps, so an interrupted run picks
    up where it left off and a completed one is a no-op.
    """
    if not model:
        return {"embedded": 0, "reason": "no model version known yet"}

    done = failed = 0
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        while done + failed < limit:
            batch = await asyncio.to_thread(
                db.species_missing_reference_embeddings, model, _BATCH)
            if not batch:
                break
            for photo in batch:
                embedding = await _embed_one(client, settings, photo)
                if embedding:
                    await asyncio.to_thread(
                        db.put_reference_embedding, photo["common_name"],
                        photo["position"], model, embedding)
                    done += 1
                else:
                    # Record nothing on failure so the row stays in the "missing" set and a
                    # later run retries it — a transient iNat blip should not permanently
                    # cost a species its bootstrap.
                    failed += 1
                await asyncio.sleep(_PAUSE)

    if done:
        log.info("Bootstrapped %d reference embedding(s) (%d failed).", done, failed)
        await asyncio.to_thread(probe.rebuild, model)
    return {"embedded": done, "failed": failed}


def start(settings, model: str) -> None:
    """Kick off a background bootstrap, unless one is already running."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(run(settings, model))


async def stop() -> None:
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
