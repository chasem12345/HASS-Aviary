"""Aviary FastAPI application: ingress-aware web UI + MQTT ingest lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import (
    backfill, bootstrap, crops, db, http, identify, inat, ingest, keepsakes, kept, media_audit, notify, probe, proxy, seasonality, solar, species_audio, species_info, species_photos,
)
from . import hastats
from .mqtt_client import MqttIngestor
from .routes import ASSET_VER, register_routes
from .settings import load_settings

_APP_DIR = os.path.dirname(__file__)

# Module-level, because the background maintenance task lives outside create_app() and its
# local logger. Configured by basicConfig in create_app before anything logs through it.
log = logging.getLogger("aviary")

# Python's mimetypes table has no web-font entries, so StaticFiles would serve the
# vendored dex font as text/plain.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")


class NoCacheStaticFiles(StaticFiles):
    """Serve static files with ``Cache-Control: no-cache``.

    We already cache-bust asset URLs with a ``?v=`` build token, but a reverse proxy in
    front of Home Assistant (e.g. nginx caching ``*.js``/``*.css`` by path) can serve a
    stale body and ignore the query string. Sending an explicit ``no-cache`` tells
    well-behaved caches to revalidate (via ETag) instead of storing, so an add-on update
    is reflected immediately. ETag revalidation keeps this cheap (304s when unchanged).
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class IngressStripMiddleware:
    """Normalize the request path for Home Assistant ingress.

    Depending on the HA/Supervisor version, ingress may forward either the *stripped*
    path (``/static/app.css``) or the *full* path including the
    ``/api/hassio_ingress/<token>`` prefix. We strip the prefix (from ``X-Ingress-Path``)
    when it's present so routing always sees the real app path, and we do NOT touch
    ``root_path`` (which would break Starlette routing on an already-stripped path).
    Outgoing URLs are prefixed separately in templates via ``u()``.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            prefix = ""
            for key, value in scope.get("headers", []):
                if key == b"x-ingress-path":
                    prefix = value.decode("latin-1").rstrip("/")
                    break
            if prefix:
                path = scope.get("path", "")
                if path.startswith(prefix):
                    new_path = path[len(prefix):] or "/"
                    scope = dict(scope)
                    scope["path"] = new_path
                    scope["raw_path"] = new_path.encode("latin-1")
        await self.app(scope, receive, send)


async def _identify_maintenance(settings) -> None:
    """Post-start identification housekeeping.

    Deliberately after a short delay rather than inline in startup: requeueing pending
    rows immediately would race the identification workers into a cold GPU service that
    is still loading its model, and every one of those requests would fail.
    """
    await asyncio.sleep(15)
    await identify.requeue_pending()
    await identify.purge_old()
    # Uncertain fragments already in the review queue whose visit siblings can name
    # them (0.36.0): database only, no GPU, so it need not wait for the service.
    try:
        await identify.inherit_backlog()
    except Exception:
        log.exception("Visit-context pass over the review queue failed.")
    # Retro-protect the zoomed footage of events pinned before kept-exports existed.
    # Before the probe wait on purpose: this needs only Frigate, not the GPU service.
    try:
        await kept.backfill_exports(settings)
    except Exception:
        log.exception("Kept-export backfill failed; will retry next start.")

    # Build the few-shot probe from whatever labels already exist. The embedding key has
    # to come from the service, because an embedding is only comparable with others from
    # the same model — and only the service knows what it is running.
    #
    # Poll rather than probe once: the GPU service legitimately takes minutes to come up
    # (container rebuild, cold model cache, boot ordering across two machines), and a
    # one-shot check here used to silently cost the whole learning layer until the next
    # add-on restart. Frequent at first — the common case is a service seconds away —
    # then relaxed; indefinite, because this task dies with the app anyway and a probe
    # that eventually loads is strictly better than one that never does.
    model = await identify.probe_model()
    waited = 0
    if not model:
        log.info("Identification service not reachable yet; waiting for it to come up.")
    while not model:
        delay = 30 if waited < 600 else 300
        await asyncio.sleep(delay)
        waited += delay
        model = await identify.probe_model()
    if waited:
        log.info("Identification service is up (waited %ds); building the probe.", waited)
    await asyncio.to_thread(probe.rebuild, model)
    # Fill in any missing reference embeddings in the background. Idempotent, so this is a
    # no-op once the registry has been covered.
    bootstrap.start(settings, model)

    # Hand-named detections whose example is missing, or was chosen for a different
    # species (a relabel from before 0.32.0), get one chosen for their label — see
    # identify.reharvest_manual. Recent rows only: older media is past Frigate's retention.
    counts = await identify.reharvest_manual(50)
    if counts["recovered"] or counts["retargeted"]:
        await asyncio.to_thread(probe.rebuild, model)


def create_app() -> FastAPI:
    settings = load_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    os.makedirs(settings.data_dir, exist_ok=True)
    db.init_db(settings.db_path)
    crops.configure(settings.data_dir)
    ingest.configure(
        ignore_unclassified=settings.ignore_unclassified,
        ignore_cameras=settings.ignore_cameras,
    )
    notify.configure(settings)
    species_audio.configure(settings)
    identify.configure(settings)
    probe.configure(settings.identify_learn_from_auto)
    keepsakes.configure(settings)
    if keepsakes.enabled():
        ingest.set_keepsake_hook(keepsakes.schedule)
    media_audit.configure(settings)
    inat.configure(settings)
    if inat.enabled() and settings.inat_auto_post and not settings.require_species_confirmation:
        # No review queue: the first live detection of a species IS its confirmation.
        ingest.set_new_species_hook(inat.on_new_species)
    # One-time catch-up for the has_crop column: rows from before it existed learn
    # whether their crop file is on disk (one directory listing, one UPDATE).
    if not db.get_pref("has_crop_scan_done"):
        flagged = db.mark_crops_present(crops.list_primary_refs())
        db.set_pref("has_crop_scan_done", "1")
        if flagged:
            log.info("Flagged %d detection(s) as having a stored crop.", flagged)
    # Seed BEFORE MQTT/backfill start so existing species/refs never fire notifications.
    ingest.seed_notify_state()
    # Same for visits: species already present in a recent/open visit were announced (or
    # deliberately not) before this restart — a member arriving now must not re-notify.
    # And the reverse repair (0.36.0): a claim taken for a row whose verdict has not
    # landed was the link-time seed's doing, not an announcement — release it so the
    # verdict can announce.
    released = db.unseed_pending_claims()
    seeded = db.seed_visit_announcements(time.time() - 3600)
    vstats = db.visit_stats()
    log.info("Visits: %d, covering %d Frigate events (%d ungrouped); pre-marked %d "
             "recent visit/species pairs as announced%s.",
             vstats["visits"], vstats["grouped"], vstats["ungrouped"], seeded,
             f", released {released} premature claim(s)" if released else "")

    if identify.enabled():
        # Two routes into the identifier. An event Frigate could not name arrives as
        # generic 'bird' and gets the full identification instead of being discarded by
        # the unclassified gate. One Frigate DID name is held for a quick confirmation
        # of Frigate's own crop (service 0.12.0+), and a label that lands after the
        # identifier already answered is reconciled without a second GPU pass.
        ingest.set_identify_hook(identify.submit)
        ingest.set_confirm_capable(identify.confirm_available)
        ingest.set_late_label_hook(identify.resolve_late_label)
    elif settings.identify_enabled:
        # Enabled with no URL: without this warning every Frigate detection would simply
        # vanish (unclassified, no identifier) with nothing in the log to explain it.
        log.warning(
            "identify_enabled is on but identify_url is empty; external identification "
            "is inactive. With Frigate's own bird classification turned off, no Frigate "
            "detections will be recorded."
        )

    ingestor = MqttIngestor(settings)
    # Bird statistics for Home Assistant ride the same MQTT client, publishing. Off
    # silently without a broker; with one, every species gets a sensor and every Frigate
    # zone a rarity score (see hastats).
    hastats.configure(settings)
    if hastats.enabled():
        hastats.attach(ingestor)
        ingest.set_stats_hook(hastats.on_hook)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http.init_all()
        loop = asyncio.get_running_loop()
        ingest.set_event_loop(loop)
        keepsakes.set_event_loop(loop)
        inat.set_event_loop(loop)
        hastats.set_event_loop(loop)
        notify.install_blueprint()
        await identify.start(loop)
        ingestor.start()
        backfill_task = None
        if settings.backfill_on_start:
            # Run in the background so startup isn't blocked by the source APIs.
            backfill_task = asyncio.create_task(backfill.run_backfill(settings))
        # Recover work stranded by a restart, then clear out stale unidentifiable rows.
        # Both are background tasks: neither should delay serving the UI.
        identify_maint_task = asyncio.create_task(_identify_maintenance(settings))
        # Sunrise for the recap's dawn chorus and the chart markers: one Core API call
        # for the configured location, refreshed occasionally. Nothing waits on it.
        solar_task = asyncio.create_task(solar.location_loop())
        # Species keepsakes: pin each species' first and latest sighting at Frigate. After
        # the backfill (so imported history counts), then a periodic safety-net sweep.
        keepsake_task = asyncio.create_task(keepsakes.run(after=backfill_task))
        # Media audit: mark detections whose footage Frigate has expired, so cards and
        # feeds stop offering players for video that is gone. Keepsakes wait for its
        # first pass.
        audit_task = asyncio.create_task(media_audit.run(after=backfill_task))
        # Statistics to Home Assistant: after the backfill (imported history changes
        # every count), then every 15 minutes, plus the debounced nudges from ingest.
        stats_task = asyncio.create_task(hastats.run(after=backfill_task))
        # Charts and the "today" boundary use OS localtime; make misconfiguration visible.
        log.info("Aviary started (timezone: %s, TZ=%s).", time.strftime("%Z"), os.environ.get("TZ", "unset"))
        try:
            yield
        finally:
            for task in (backfill_task, identify_maint_task, solar_task, keepsake_task,
                         audit_task, stats_task):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            ingestor.stop()
            await bootstrap.stop()
            await identify.stop()
            await http.close_all()
            log.info("Aviary stopped.")

    app = FastAPI(title="Aviary", lifespan=lifespan)
    app.state.settings = settings
    app.state.ingestor = ingestor
    app.add_middleware(IngressStripMiddleware)

    # Version the static mount PATH (not just a ?v query) so a reverse proxy that
    # caches by path and ignores query strings still can't serve stale JS/CSS
    # after an update — each build is a brand-new URL path.
    app.mount(
        f"/static-{ASSET_VER}",
        NoCacheStaticFiles(directory=os.path.join(_APP_DIR, "static")),
        name="static",
    )
    register_routes(app)
    return app


app = create_app()
