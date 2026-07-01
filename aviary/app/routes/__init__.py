"""Route registration + shared Jinja2 templates environment."""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")

templates = Jinja2Templates(directory=_TEMPLATE_DIR)


def _fmt_time(epoch) -> str:
    if not epoch:
        return "—"
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_pct"] = _fmt_pct


def register_routes(app: FastAPI) -> None:
    # Imported here to avoid a circular import (submodules import `templates`).
    from . import api, media, views

    app.include_router(views.router)
    app.include_router(api.router, prefix="/api")
    app.include_router(media.router, prefix="/media")
