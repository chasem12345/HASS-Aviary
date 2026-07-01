"""Route registration + shared Jinja2 templates environment.

URL generation for Home Assistant ingress
------------------------------------------
HA ingress strips the ``/api/hassio_ingress/<token>`` prefix before forwarding the
request to the add-on (the add-on sees ``/static/app.css``), and passes the prefix in the
``X-Ingress-Path`` header. So we must NOT set Starlette's ``root_path`` (that corrupts
routing on the already-stripped path); instead we build root-relative URLs ourselves by
prepending the header value. The ``u(name, **params)`` template helper does this. When the
header is absent (direct access, or local dev) the prefix is empty and URLs still work.
"""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")


def _ingress_context(request: Request) -> dict:
    prefix = request.headers.get("X-Ingress-Path", "").rstrip("/")

    def u(endpoint, /, **params) -> str:
        """Root-relative URL for a named route, prefixed with the ingress path.

        ``endpoint`` is positional-only so route path params named ``name`` (e.g.
        ``species_detail``) don't collide with it.
        """
        return f"{prefix}{request.app.url_path_for(endpoint, **params)}"

    return {"ingress_path": prefix, "u": u}


templates = Jinja2Templates(directory=_TEMPLATE_DIR, context_processors=[_ingress_context])


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
