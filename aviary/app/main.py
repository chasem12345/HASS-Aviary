"""Aviary FastAPI application: ingress-aware web UI + MQTT ingest lifecycle."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, proxy
from .mqtt_client import MqttIngestor
from .routes import register_routes
from .settings import load_settings

_APP_DIR = os.path.dirname(__file__)


# NOTE: We deliberately do NOT set Starlette's ``root_path`` from ``X-Ingress-Path``.
# HA ingress strips its prefix before forwarding, so the add-on receives the real path
# (e.g. ``/static/app.css``); setting ``root_path`` would make Starlette strip a prefix
# that isn't in the path and break routing. URLs are prefixed in templates instead
# (see ``routes/__init__.py`` -> ``u()``).
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

    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(_APP_DIR, "static")),
        name="static",
    )
    register_routes(app)
    return app


app = create_app()
