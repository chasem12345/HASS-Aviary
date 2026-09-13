"""SQLite storage + analytics queries for Aviary.

A single ``detections`` table holds rows from both sources, tagged by ``source``.
There is no cross-source correlation. Connections are opened per-operation (SQLite in
WAL mode handles concurrent readers/one writer well), which keeps the MQTT ingest thread
and the FastAPI event loop cleanly separated.
"""

# Split by domain in 0.31.0 (this used to be one 3300-line module). Every submodule
# lists all of its names in __all__ — private ones too — so ``from app import db`` still
# sees the whole surface (``db._connect``, ``db.UNNAMED``, ...). One caveat: the
# submodules call each other directly, so monkeypatching ``app.db.<name>`` no longer
# intercepts calls made from INSIDE the package (only callers outside it).

from ._conn import *  # noqa: F401,F403
from .schema import *  # noqa: F401,F403
from ._sql import *  # noqa: F401,F403
from .detections import *  # noqa: F401,F403
from .visits import *  # noqa: F401,F403
from .subjects import *  # noqa: F401,F403
from .species import *  # noqa: F401,F403
from .recap import *  # noqa: F401,F403
from .identification import *  # noqa: F401,F403
from .learning import *  # noqa: F401,F403
from .keepsakes import *  # noqa: F401,F403
from .media_audit import *  # noqa: F401,F403
from .blacklist import *  # noqa: F401,F403
from .prefs import *  # noqa: F401,F403
from .lifelist import *  # noqa: F401,F403
from .caches import *  # noqa: F401,F403
from .stats import *  # noqa: F401,F403
