"""Aviary FastAPI application: ingress-aware web UI + MQTT ingest lifecycle."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import db, proxy
from .mqtt_client import MqttIngestor
from .routes import register_routes
from .settings import load_settings

_APP_DIR = os.path.dirname(__file__)


class IngressPathMiddleware(BaseHTTPMiddleware):
    """Set ``root_path`` from the HA ``X-Ingress-Path`` header.

    Home Assistant serves the add-on under a dynamic prefix (e.g.
    ``/api/hassio_ingress/<token>``) and passes it in this header. Setting
    ``root_path`` makes ``request.url_for`` emit correctly-prefixed links so the UI works
    both through ingress and when hit directly (header absent → prefix is empty).
    """

    async def dispatch(self, request: Request, call_next):
        ingress_path = request.headers.get("X-Ingress-Path", "")
        if ingress_path:
            request.scope["root_path"] = ingress_path
        return await call_next(request)


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
        log.info("Aviary started.")
        try:
            yield
        finally:
            ingestor.stop()
            await proxy.close_client()
            log.info("Aviary stopped.")

    app = FastAPI(title="Aviary", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(IngressPathMiddleware)

    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(_APP_DIR, "static")),
        name="static",
    )
    register_routes(app)
    return app


app = create_app()
