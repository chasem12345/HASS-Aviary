"""The one SQLite connection factory and the path init_db points it at."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional


__all__ = [
    "_db_path",
    "set_path",
    "_connect",
]


_db_path: str = ""


def set_path(path: str) -> None:
    """Point every connection at ``path`` (called once by init_db)."""
    global _db_path
    _db_path = path


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
