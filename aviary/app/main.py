"""Aviary FastAPI application: ingress-aware web UI + MQTT ingest lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import backfill, db, ingest, notify, proxy, species_audio, species_info
from .mqtt_client import MqttIngestor
from .routes import ASSET_VER, register_routes
from .settings import load_settings

_APP_DIR = os.path.dirname(__file__)


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


def create_app() -> FastAPI:
    settings = load_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("aviary")

    os.makedirs(settings.data_dir, exist_ok=True)
    db.init_db(settings.db_path)
    ingest.configure(ignore_unclassified=settings.ignore_unclassified)
    notify.configure(settings)
    # Seed BEFORE MQTT/backfill start so existing species/refs never fire notifications.
    ingest.seed_notify_state()

    ingestor = MqttIngestor(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        proxy.init_client()
        species_info.init_client()
        species_audio.init_client()
        notify.init_client()
        ingest.set_event_loop(asyncio.get_running_loop())
        notify.install_blueprint()
        ingestor.start()
        backfill_task = None
        if settings.backfill_on_start:
            # Run in the background so startup isn't blocked by the source APIs.
            backfill_task = asyncio.create_task(backfill.run_backfill(settings))
        # Charts and the "today" boundary use OS localtime; make misconfiguration visible.
        log.info("Aviary started (timezone: %s, TZ=%s).", time.strftime("%Z"), os.environ.get("TZ", "unset"))
        try:
            yield
        finally:
            if backfill_task is not None:
                backfill_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await backfill_task
            ingestor.stop()
            await proxy.close_client()
            await species_info.close_client()
            await species_audio.close_client()
            await notify.close_client()
            log.info("Aviary stopped.")

    app = FastAPI(title="Aviary", lifespan=lifespan)
    app.state.settings = settings
    app.state.ingestor = ingestor
    app.add_middleware(IngressStripMiddleware)

    # Version the static mount PATH (not just a ?v query) so a reverse proxy that
    # caches by path and ignores query strings still can't serve a stale app.js/app.css
    # after an update — each build is a brand-new URL path.
    app.mount(
        f"/static-{ASSET_VER}",
        NoCacheStaticFiles(directory=os.path.join(_APP_DIR, "static")),
        name="static",
    )
    register_routes(app)
    return app


app = create_app()
