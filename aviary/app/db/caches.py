"""Cached external species data: info, reference audio, photos, seasonality."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ._conn import _connect

__all__ = [
    "get_species_info",
    "put_species_info",
    "get_species_audio",
    "put_species_audio",
    "get_species_photos",
    "put_species_photos",
    "get_species_season",
    "put_species_season",
]


def get_species_info(common_name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM species_info WHERE common_name = ?", (common_name,)
        ).fetchone()
    return dict(row) if row else None


def put_species_info(row: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO species_info (
                common_name, scientific_name, descriptor, extract, wiki_url,
                family, "order", conservation, fetched_at, ok, inat_taxon_id, sections
            ) VALUES (
                :common_name, :scientific_name, :descriptor, :extract, :wiki_url,
                :family, :order, :conservation, :fetched_at, :ok, :inat_taxon_id, :sections
            )
            ON CONFLICT(common_name) DO UPDATE SET
                scientific_name = excluded.scientific_name,
                descriptor      = excluded.descriptor,
                extract         = excluded.extract,
                wiki_url        = excluded.wiki_url,
                family          = excluded.family,
                "order"         = excluded."order",
                conservation    = excluded.conservation,
                fetched_at      = excluded.fetched_at,
                ok              = excluded.ok,
                -- A later fetch that failed to resolve the taxon shouldn't discard an
                -- id we already have; reference audio depends on it.
                inat_taxon_id   = COALESCE(excluded.inat_taxon_id, species_info.inat_taxon_id),
                sections        = excluded.sections
            """,
            row,
        )


def get_species_audio(common_name: str, kind: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM species_audio WHERE common_name = ? COLLATE NOCASE AND kind = ?",
            (common_name, kind),
        ).fetchone()
    return dict(row) if row else None


def put_species_audio(row: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO species_audio (
                common_name, kind, provider, taxon_id, sound_id, source_url, file_url,
                content_type, license_code, attribution, quality, fetched_at, ok
            ) VALUES (
                :common_name, :kind, :provider, :taxon_id, :sound_id, :source_url, :file_url,
                :content_type, :license_code, :attribution, :quality, :fetched_at, :ok
            )
            ON CONFLICT(common_name, kind) DO UPDATE SET
                provider       = excluded.provider,
                taxon_id       = COALESCE(excluded.taxon_id, species_audio.taxon_id),
                sound_id       = excluded.sound_id,
                source_url     = excluded.source_url,
                file_url       = excluded.file_url,
                content_type   = excluded.content_type,
                license_code   = excluded.license_code,
                attribution    = excluded.attribution,
                quality        = excluded.quality,
                fetched_at     = excluded.fetched_at,
                ok             = excluded.ok
            """,
            row,
        )


def get_species_photos(common_name: str) -> list[dict]:
    """Cached reference photos for a species, in strip order."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM species_photos WHERE common_name = ? COLLATE NOCASE ORDER BY position",
            (common_name,),
        ).fetchall()
    return [dict(r) for r in rows]


def put_species_photos(common_name: str, rows: list[dict]) -> None:
    """Replace a species' cached photos wholesale.

    A rewrite rather than an upsert: a refetch can return fewer photos than last time (a
    photo relicensed or removed upstream), and leftover rows would keep a stale image in
    the strip.
    """
    with _connect() as conn:
        conn.execute("DELETE FROM species_photos WHERE common_name = ? COLLATE NOCASE",
                     (common_name,))
        conn.executemany(
            """
            INSERT INTO species_photos (
                common_name, position, photo_id, file_url, thumb_url,
                license_code, attribution, source_url, fetched_at, ok
            ) VALUES (
                :common_name, :position, :photo_id, :file_url, :thumb_url,
                :license_code, :attribution, :source_url, :fetched_at, :ok
            )
            """,
            rows,
        )


# ---------------------------------------------------------------- seasonality cache

def get_species_season(common_name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM species_season WHERE common_name = ? COLLATE NOCASE",
            (common_name,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["months"] = json.loads(d["months"]) if d.get("months") else None
    except (ValueError, TypeError):
        d["months"] = None
    return d


def put_species_season(row: dict) -> None:
    values = dict(row)
    months = values.get("months")
    values["months"] = json.dumps(months) if months is not None else None
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO species_season
                (common_name, taxon_id, lat, lon, radius_km, months, total, fetched_at, ok)
            VALUES (:common_name, :taxon_id, :lat, :lon, :radius_km, :months, :total,
                    :fetched_at, :ok)
            ON CONFLICT(common_name) DO UPDATE SET
                taxon_id = excluded.taxon_id, lat = excluded.lat, lon = excluded.lon,
                radius_km = excluded.radius_km, months = excluded.months,
                total = excluded.total, fetched_at = excluded.fetched_at, ok = excluded.ok
            """,
            values,
        )
