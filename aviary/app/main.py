"""Aviary FastAPI application: ingress-aware web UI + MQTT ingest lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import backfill, db, proxy
from .mqtt_client import MqttIngestor
from .routes import register_routes
from .settings import load_settings

_APP_DIR = os.path.dirname(__file__)


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

    ingestor = MqttIngestor(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        proxy.init_client()
        ingestor.start()
        backfill_task = None
        if settings.backfill_on_start:
            # Run in the background so startup isn't blocked by the source APIs.
            backfill_task = asyncio.create_task(backfill.run_backfill(settings))
        log.info("Aviary started.")
        try:
            yield
        finally:
            if backfill_task is not None:
                backfill_task.cancel()
            ingestor.stop()
            await proxy.close_client()
            log.info("Aviary stopped.")

    app = FastAPI(title="Aviary", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(IngressStripMiddleware)

    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(_APP_DIR, "static")),
        name="static",
    )
    register_routes(app)
    return app


app = create_app()
