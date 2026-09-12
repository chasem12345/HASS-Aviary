"""The app_prefs key/value store."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect

__all__ = [
    "get_pref",
    "set_pref",
]


# ----------------------------------------------------------------------- preferences

def get_pref(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM app_prefs WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_pref(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO app_prefs (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
