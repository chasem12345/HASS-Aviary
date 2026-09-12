"""The species blacklist."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect

__all__ = [
    "blacklist_add",
    "blacklist_remove",
    "is_blacklisted_name",
    "blacklist_entries",
    "blacklist_names",
]


# ------------------------------------------------------------------------- blacklist

def blacklist_add(
    common_name: str,
    scientific_name: Optional[str] = None,
    purged: int = 0,
) -> None:
    """Blacklist a species so ingest drops it from now on.

    Re-blacklisting an existing entry keeps the earlier ``added_at`` but refreshes the
    scientific name (which may only have become known later) and the purged flag.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO species_blacklist (common_name, scientific_name, added_at, purged)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(common_name) DO UPDATE SET
                scientific_name = COALESCE(excluded.scientific_name, species_blacklist.scientific_name),
                purged          = MAX(species_blacklist.purged, excluded.purged)
            """,
            (common_name, scientific_name or None, time.time(), 1 if purged else 0),
        )


def blacklist_remove(common_name: str) -> bool:
    """Un-blacklist a species (re-opens ingest). Returns True if an entry was removed."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM species_blacklist WHERE common_name = ? COLLATE NOCASE",
            (common_name,),
        )
    return cur.rowcount > 0


def is_blacklisted_name(name: str) -> bool:
    """Whether a name is blacklisted under either its common or scientific form."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM species_blacklist
            WHERE common_name = ? COLLATE NOCASE OR scientific_name = ? COLLATE NOCASE
            LIMIT 1
            """,
            (name, name),
        ).fetchone()
    return row is not None


def blacklist_entries() -> list[dict]:
    """All blacklist entries, newest first (for the settings page)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM species_blacklist ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def blacklist_names() -> list[tuple[str, Optional[str]]]:
    """(common_name, scientific_name) pairs, for seeding ingest's in-memory set."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT common_name, scientific_name FROM species_blacklist"
        ).fetchall()
    return [(r["common_name"], r["scientific_name"]) for r in rows]
