"""SQLite storage + analytics queries for Aviary.

A single ``detections`` table holds rows from both sources, tagged by ``source``.
There is no cross-source correlation. Connections are opened per-operation (SQLite in
WAL mode handles concurrent readers/one writer well), which keeps the MQTT ingest thread
and the FastAPI event loop cleanly separated.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,               -- 'frigate' | 'birdnet'
    source_ref      TEXT    NOT NULL,               -- frigate event id / birdnet detection id
    common_name     TEXT    NOT NULL DEFAULT 'bird',
    scientific_name TEXT,
    species_code    TEXT,
    confidence      REAL,
    location        TEXT,                            -- frigate camera / birdnet source node
    start_time      REAL    NOT NULL,               -- epoch seconds
    end_time        REAL,
    has_clip        INTEGER NOT NULL DEFAULT 0,
    has_snapshot    INTEGER NOT NULL DEFAULT 0,
    clip_ref        TEXT,
    snapshot_ref    TEXT,
    native_id       TEXT,                            -- source-side id (birdnet DB id / frigate event id)
    raw_json        TEXT,
    created_at      REAL    NOT NULL,
    -- External identification (aviary-id). NULL on every BirdNET row and on Frigate rows
    -- from before the feature existed; those are "not applicable", not "failed".
    id_status       TEXT,                            -- pending|ok|low_confidence|failed
    id_score        REAL,                            -- fused top-1 probability
    id_margin       REAL,                            -- top-1 minus top-2; the real confidence signal
    id_model        TEXT,                            -- model@vocabulary digest that produced it
    id_at           REAL,                            -- when the identification completed
    id_candidates   TEXT                             -- JSON shortlist the model considered
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_detections_source_ref
    ON detections (source, source_ref);
CREATE INDEX IF NOT EXISTS idx_detections_start_time ON detections (start_time);
CREATE INDEX IF NOT EXISTS idx_detections_common_name ON detections (common_name);
-- The collation has to be declared on the index, not just in the query, or SQLite won't
-- use it for a `= ? COLLATE NOCASE` comparison. Without this, canonical_species() does a
-- full table scan for every single ingested detection, and the startup remap below is
-- quadratic in the table size (measured: 18s at 20k rows, ~2min at 50k).
CREATE INDEX IF NOT EXISTS idx_detections_scientific_nocase
    ON detections (scientific_name COLLATE NOCASE);
-- NOTE: idx_detections_id_status is deliberately NOT declared here. This script runs
-- against pre-existing databases before the ALTER TABLEs below add the id_* columns, and
-- a partial index whose WHERE clause names a missing column does not get skipped — it
-- raises, aborting init_db and taking the whole add-on down on upgrade. It is created in
-- init_db() after the columns are guaranteed to exist.

-- Tombstones for user-deleted detections: ingest and backfill skip these refs so a
-- deleted misclassification can't be re-imported from the source's history.
CREATE TABLE IF NOT EXISTS deleted_refs (
    source     TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    deleted_at REAL NOT NULL,
    PRIMARY KEY (source, source_ref)
);

-- Cached per-species reference info (Wikipedia blurb + iNaturalist taxonomy).
CREATE TABLE IF NOT EXISTS species_info (
    common_name     TEXT PRIMARY KEY,
    scientific_name TEXT,
    descriptor      TEXT,      -- short one-liner, e.g. "Species of North American bird"
    extract         TEXT,      -- blurb paragraph
    wiki_url        TEXT,
    family          TEXT,
    "order"         TEXT,
    conservation    TEXT,
    fetched_at      REAL,
    ok              INTEGER NOT NULL DEFAULT 0,
    inat_taxon_id   INTEGER    -- reused to look up reference audio (see species_audio)
);

-- Species the user never wants ingested again (chronic misclassifications).
-- Both names are matched at ingest: blacklisting purges the species' rows, which
-- removes the very data canonical_species() needs to map Frigate's scientific-name
-- labels onto common names — so the scientific name has to be remembered here.
-- NOCASE on the primary key matches how every other species lookup compares names.
CREATE TABLE IF NOT EXISTS species_blacklist (
    common_name     TEXT PRIMARY KEY COLLATE NOCASE,
    scientific_name TEXT COLLATE NOCASE,
    added_at        REAL NOT NULL,
    purged          INTEGER NOT NULL DEFAULT 0   -- detections removed when blacklisted
);

-- User preferences editable from the UI (currently just the theme). Kept server-side
-- rather than in the browser so server-rendered pages can stamp the theme during
-- render, with no flash of the wrong theme and no per-device drift.
CREATE TABLE IF NOT EXISTS app_prefs (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Species the user has approved into the registry. ABSENCE MEANS UNCONFIRMED, which is
-- what lets a brand-new species queue for review without ingest having to write anything:
-- a species nobody has approved simply has no row here. Existing species are stamped
-- confirmed by a one-time migration, so enabling the gate never dumps a whole registry
-- into the review queue. NOCASE matches every other species lookup.
CREATE TABLE IF NOT EXISTS species_confirmed (
    common_name  TEXT PRIMARY KEY COLLATE NOCASE,
    confirmed_at REAL NOT NULL
);

-- Cached reference photos per species (licensed iNaturalist taxon photos), so a
-- questionable detection can be compared against known pictures of the bird — the camera
-- snapshot answers "what did we catch", these answer "what should it look like". Ordered
-- by `position` (0 = the taxon's default photo). Metadata only; the image bytes are
-- streamed from the provider on demand, like every other media asset in Aviary.
CREATE TABLE IF NOT EXISTS species_photos (
    common_name  TEXT COLLATE NOCASE,
    position     INTEGER NOT NULL,   -- 0-based rank within the species' photo strip
    photo_id     TEXT,
    file_url     TEXT,               -- medium size, for display
    thumb_url    TEXT,               -- square size, for the strip
    license_code TEXT,
    attribution  TEXT,               -- must always be displayed: these are CC photos
    source_url   TEXT,               -- photo page, for the credit backlink
    fetched_at   REAL,
    ok           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (common_name, position)
);

-- Image embeddings from the identification service, one per identified detection.
--
-- NOTHING READS THIS YET. It exists from day one because the data is free at
-- identification time and ruinously expensive to backfill: reconstructing it later means
-- re-downloading and re-running every clip in the history through the GPU. Each confirmed
-- species in `species_confirmed` is a labelled example from your own cameras, so once
-- enough have accumulated, a nearest-centroid classifier over these vectors can be built
-- to beat the zero-shot model on exactly the locally-confusable pairs it struggles with.
--
-- Stored as a base64 float16 blob keyed by model, because a vector from one model or
-- vocabulary is not comparable with one from another.
CREATE TABLE IF NOT EXISTS identification_embeddings (
    detection_id INTEGER PRIMARY KEY REFERENCES detections(id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    embedding    TEXT NOT NULL,
    created_at   REAL NOT NULL
);

-- Embeddings of reference photos, one row per (species, photo). These bootstrap the
-- few-shot classifier: a brand-new install has no confirmed detections to learn from, but
-- it does have cached iNaturalist reference photos for every species in the registry, and
-- those are labelled images of exactly the right birds.
--
-- Kept apart from identification_embeddings because these are NOT detections — they have
-- no event, no clip, and no detection_id to hang off. They are also weighted lower when
-- building centroids: a posed photo in good light is the right species but the wrong
-- domain, and one real frame from your own feeder is worth several of them.
CREATE TABLE IF NOT EXISTS species_reference_embeddings (
    common_name TEXT NOT NULL COLLATE NOCASE,
    position    INTEGER NOT NULL,      -- matches species_photos.position
    model       TEXT NOT NULL,         -- vectors from different models are incomparable
    embedding   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (common_name, position, model)
);

-- Answers the user has rejected for a specific detection ("that is not a Blue Jay").
-- Excluded from the candidate set on the next re-identify, so each rejection walks the
-- model down its own ranking instead of handing back the same wrong answer.
--
-- Per-detection, not per-species: rejecting a guess for one bird says nothing about
-- whether the species occurs here. The global, permanent version of that judgement is
-- species_blacklist.
CREATE TABLE IF NOT EXISTS identification_rejections (
    detection_id INTEGER NOT NULL REFERENCES detections(id) ON DELETE CASCADE,
    species      TEXT NOT NULL COLLATE NOCASE,
    rejected_at  REAL NOT NULL,
    PRIMARY KEY (detection_id, species)
);

"""

# Split out of _SCHEMA so the kind-column migration below can recreate just this table.
#
# Cached reference recordings per species, so a questionable classification can be
# compared against a known song or call. One row per (species, kind): xeno-canto supplies
# 'song' and 'call' separately, iNaturalist has no type information and yields 'any'.
# Only the metadata is stored; the audio itself is streamed from the provider on demand.
_SCHEMA_SPECIES_AUDIO = """
CREATE TABLE IF NOT EXISTS species_audio (
    common_name    TEXT COLLATE NOCASE,
    kind           TEXT NOT NULL,   -- 'song' | 'call' | 'any'
    provider       TEXT,            -- 'xeno-canto' | 'inaturalist'
    taxon_id       INTEGER,         -- iNaturalist only; reused from species_info
    sound_id       TEXT,            -- XC recording id or iNat sound id
    source_url     TEXT,            -- recording page, for the credit backlink
    file_url       TEXT,
    content_type   TEXT,
    license_code   TEXT,
    attribution    TEXT,            -- must always be displayed: these are CC recordings
    quality        TEXT,            -- XC A-E; NULL for iNaturalist
    fetched_at     REAL,
    ok             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (common_name, kind)
);
"""

_db_path: str = ""


def init_db(db_path: str) -> None:
    """Configure the module and create the schema."""
    global _db_path
    _db_path = db_path
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(_SCHEMA)
        conn.executescript(_SCHEMA_SPECIES_AUDIO)
        # species_audio gained a `kind` column and a composite primary key when reference
        # audio grew separate song/call variants. SQLite can't alter a primary key, and
        # this table is a pure metadata cache (no audio bytes, nothing user-authored,
        # TTL-refilled on next view), so the old shape is simply dropped and rebuilt.
        audio_cols = {r[1] for r in conn.execute("PRAGMA table_info(species_audio)")}
        if "kind" not in audio_cols:
            conn.execute("DROP TABLE species_audio")
            conn.executescript(_SCHEMA_SPECIES_AUDIO)
        # Additive migrations for databases created by older versions.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(detections)")}
        if "native_id" not in cols:
            conn.execute("ALTER TABLE detections ADD COLUMN native_id TEXT")
        # External identification columns. Added individually rather than as a group so a
        # database that was upgraded partway (e.g. an add-on downgrade in between) still
        # converges. All nullable with no default: existing rows are "not applicable".
        for name, decl in (
            ("id_status", "TEXT"), ("id_score", "REAL"), ("id_margin", "REAL"),
            ("id_model", "TEXT"), ("id_at", "REAL"), ("id_candidates", "TEXT"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE detections ADD COLUMN {name} {decl}")
        # Created here, not in _SCHEMA, because a partial index on a column that does not
        # exist yet is a hard error rather than a skip (see the note in _SCHEMA).
        #
        # Partial on purpose: identification queries only ever ask for rows in a non-NULL
        # state, and on a mature database nearly every row is NULL here — every BirdNET
        # row, plus all history predating the feature. Indexing only the interesting rows
        # keeps the restart requeue and the review queue cheap regardless of history size.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_detections_id_status
                ON detections (id_status, start_time) WHERE id_status IS NOT NULL
            """
        )
        # A detection deleted before this table existed leaves no embedding behind, but a
        # row deleted *after* must not leave one either. SQLite enforces the ON DELETE
        # CASCADE only when foreign keys are enabled, which is per-connection and off by
        # default — so delete_detection() cleans up explicitly instead of relying on it.
        info_cols = {r[1] for r in conn.execute("PRAGMA table_info(species_info)")}
        if "inat_taxon_id" not in info_cols:
            conn.execute("ALTER TABLE species_info ADD COLUMN inat_taxon_id INTEGER")
            # Rows cached before this column existed have no taxon id, but their TTL is
            # still fresh — reference audio (which needs the id) would silently find
            # nothing for up to a month. Expire them so the next lookup refills the id.
            # One-time, and only for rows that predate the column.
            conn.execute("UPDATE species_info SET fetched_at = 0 WHERE inat_taxon_id IS NULL")
        # Everything already in the registry counts as approved: enabling the confirmation
        # gate must not dump an existing collection into the review queue. Guarded by a
        # marker rather than "is species_confirmed empty", which would re-stamp for someone
        # who has since rejected every species. One indexed pass, once, ever.
        migrated = conn.execute(
            "SELECT value FROM app_prefs WHERE key = 'species_confirm_migrated'"
        ).fetchone()
        if not migrated:
            conn.execute(
                """
                INSERT OR IGNORE INTO species_confirmed (common_name, confirmed_at)
                SELECT DISTINCT common_name, ? FROM detections
                WHERE common_name != '' AND common_name != 'bird' COLLATE NOCASE
                """,
                (time.time(),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_prefs (key, value) VALUES ('species_confirm_migrated', '1')"
            )
        # Unidentified detections are stored with the placeholder name 'bird', which the
        # confirmation migration above (and earlier versions of it) happily stamped as a
        # species — putting "bird" in the review queue, the registry and the dex numbering.
        # Every query now excludes it, but a row already written has to be cleared out.
        conn.execute(
            "DELETE FROM species_confirmed WHERE common_name = 'bird' COLLATE NOCASE"
        )
        # Same reasoning for the reference caches: they may hold a Wikipedia blurb and
        # photos fetched for the "species" called bird.
        for table in ("species_info", "species_photos", "species_audio"):
            conn.execute(f"DELETE FROM {table} WHERE common_name = 'bird' COLLATE NOCASE")
        # Idempotent cleanup: remap rows whose common_name is actually a scientific
        # name (e.g. Frigate's classifier) onto the species' real common name, when
        # another source has recorded the pairing. Keeps species pages and
        # new-species notifications from splitting one bird into two species.
        #
        # Probe first and skip the UPDATE when there is nothing to remap — the normal
        # case on every start after the first. This runs before uvicorn binds its port,
        # so a slow pass here means Home Assistant's ingress serves 502 until it
        # finishes.
        needs_remap = conn.execute(
            """
            SELECT 1 FROM detections a
            WHERE a.common_name != '' AND EXISTS (
                SELECT 1 FROM detections b
                WHERE b.scientific_name = a.common_name COLLATE NOCASE
                  AND b.common_name != ''
                  AND LOWER(b.common_name) != LOWER(b.scientific_name)
            )
            LIMIT 1
            """
        ).fetchone()
        if needs_remap:
            conn.execute(
                """
                UPDATE detections SET
                    scientific_name = COALESCE(scientific_name, common_name),
                    common_name = (
                        SELECT b.common_name FROM detections b
                        WHERE b.scientific_name = detections.common_name COLLATE NOCASE
                          AND b.common_name != ''
                          AND LOWER(b.common_name) != LOWER(b.scientific_name)
                        ORDER BY b.start_time DESC LIMIT 1
                    )
                WHERE common_name != '' AND EXISTS (
                    SELECT 1 FROM detections b
                    WHERE b.scientific_name = detections.common_name COLLATE NOCASE
                      AND b.common_name != ''
                      AND LOWER(b.common_name) != LOWER(b.scientific_name)
                )
                """
            )


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_detection(row: dict[str, Any]) -> None:
    """Insert a detection, or update it if (source, source_ref) already exists.

    Frigate sends multiple messages per event (new/update/end); the latest wins so the
    final species and confidence are kept. COALESCE preserves any media flags/refs that
    were already captured but are absent from a later message.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO detections (
                source, source_ref, common_name, scientific_name, species_code,
                confidence, location, start_time, end_time,
                has_clip, has_snapshot, clip_ref, snapshot_ref, native_id,
                raw_json, created_at
            ) VALUES (
                :source, :source_ref, :common_name, :scientific_name, :species_code,
                :confidence, :location, :start_time, :end_time,
                :has_clip, :has_snapshot, :clip_ref, :snapshot_ref, :native_id,
                :raw_json, :created_at
            )
            ON CONFLICT(source, source_ref) DO UPDATE SET
                -- Never downgrade a classified species back to generic 'bird' when a
                -- later message arrives without a sub_label.
                common_name     = CASE
                                      WHEN excluded.common_name = 'bird'
                                           AND detections.common_name != 'bird'
                                      THEN detections.common_name
                                      ELSE excluded.common_name
                                  END,
                scientific_name = COALESCE(excluded.scientific_name, detections.scientific_name),
                species_code    = COALESCE(excluded.species_code, detections.species_code),
                -- Scalar MAX() is NULL if either side is NULL; keep the best non-NULL score.
                confidence      = COALESCE(
                                      MAX(detections.confidence, excluded.confidence),
                                      detections.confidence, excluded.confidence
                                  ),
                location        = excluded.location,
                end_time        = COALESCE(excluded.end_time, detections.end_time),
                has_clip        = MAX(detections.has_clip, excluded.has_clip),
                has_snapshot    = MAX(detections.has_snapshot, excluded.has_snapshot),
                clip_ref        = COALESCE(excluded.clip_ref, detections.clip_ref),
                snapshot_ref    = COALESCE(excluded.snapshot_ref, detections.snapshot_ref),
                native_id       = COALESCE(excluded.native_id, detections.native_id),
                raw_json        = excluded.raw_json
            """,
            row,
        )


# --------------------------------------------------------------------------- queries

def _source_clause(source: Optional[str], params: list) -> str:
    if source in ("frigate", "birdnet"):
        params.append(source)
        return " AND source = ?"
    return ""


# The placeholder name carried by a detection with no species. It is the absence of an
# answer, not an answer — kept in sync with ingest.is_unclassified().
UNNAMED = "bird"


def _named_clause(table: str = "detections") -> str:
    """Exclude species-less detections from anything species-shaped.

    Before external identification existed this was unnecessary: ``ignore_unclassified``
    meant a row named 'bird' was never stored, so every query could assume a real species.
    Identification deliberately stores such rows (pending, then possibly unidentifiable),
    which turned that assumption into a bug — 'bird' queued for confirmation as a species,
    took a dex number, and counted toward the species total.

    Applied to every query that answers "which species", so the rule lives in one place
    rather than being remembered independently in a dozen SQL strings.
    """
    return f" AND {table}.common_name != '{UNNAMED}' COLLATE NOCASE"


def recent_detections(
    limit: int = 60,
    source: Optional[str] = None,
    species: Optional[str] = None,
    before: Optional[float] = None,
    since: Optional[float] = None,
) -> list[dict]:
    """Newest-first detections. ``before`` is a start_time cursor for pagination."""
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    if before is not None:
        where += " AND start_time < ?"
        params.append(before)
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM detections {where} ORDER BY start_time DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def change_marker(source: Optional[str] = None, species: Optional[str] = None) -> dict:
    """Cheap poll target: row count + newest start_time for the given filters."""
    params: list = []
    where = "WHERE 1=1" + _source_clause(source, params)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS total, MAX(start_time) AS newest FROM detections {where}",
            params,
        ).fetchone()
    return dict(row) if row else {"total": 0, "newest": None}


def species_stats(name: str) -> dict:
    """Aggregate stats for one species (all-time)."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                AS total,
                MIN(start_time)         AS first_seen,
                MAX(start_time)         AS last_seen,
                MAX(confidence)         AS best_confidence,
                SUM(source = 'frigate') AS frigate_total,
                SUM(source = 'birdnet') AS birdnet_total,
                MIN(CASE WHEN source = 'frigate' THEN start_time END) AS first_frigate,
                MAX(CASE WHEN source = 'frigate' THEN start_time END) AS last_frigate,
                MIN(CASE WHEN source = 'birdnet' THEN start_time END) AS first_birdnet,
                MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS last_birdnet,
                MAX(scientific_name)    AS scientific_name
            FROM detections WHERE common_name = ?
            """,
            (name,),
        ).fetchone()
    return dict(row) if row else {}


# Registry queries hide unconfirmed species while the confirmation gate is on. "Unconfirmed"
# means *absent from* species_confirmed, so this is an EXISTS test rather than a join —
# a join would risk duplicating detection rows and would need an outer join to express
# absence anyway. Callers pass only_confirmed=settings.require_species_confirmation, so with
# the gate off every query runs exactly as it did before and nothing is stranded.
def _confirmed_clause(only_confirmed: bool, table: str = "detections") -> str:
    if not only_confirmed:
        return ""
    return (f" AND EXISTS (SELECT 1 FROM species_confirmed sc"
            f" WHERE sc.common_name = {table}.common_name COLLATE NOCASE)")


def confirm_species(common_name: str) -> None:
    """Approve a species into the registry (idempotent; keeps the original timestamp)."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO species_confirmed (common_name, confirmed_at) VALUES (?, ?)",
            (common_name, time.time()),
        )


def unconfirm_species(common_name: str) -> bool:
    """Send a species back to the review queue. True if it had been confirmed."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM species_confirmed WHERE common_name = ? COLLATE NOCASE", (common_name,)
        )
        return cur.rowcount > 0


def is_species_confirmed(common_name: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM species_confirmed WHERE common_name = ? COLLATE NOCASE", (common_name,)
        ).fetchone()
    return row is not None


def unconfirmed_count() -> int:
    """How many detected species are still awaiting review."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT common_name FROM detections
                WHERE common_name != '' AND common_name != 'bird' COLLATE NOCASE
                  AND NOT EXISTS (
                    SELECT 1 FROM species_confirmed sc
                    WHERE sc.common_name = detections.common_name COLLATE NOCASE
                )
                GROUP BY common_name
            )
            """
        ).fetchone()
    return row["c"] if row else 0


def new_species_count(source: Optional[str] = None, since: Optional[float] = None,
                      only_confirmed: bool = False) -> int:
    """Number of species whose *first-ever* detection falls at/after ``since``.

    With ``since`` None (all-time), every species is "new", so this returns the distinct
    species count.
    """
    params: list = []
    src = _source_clause(source, params) + _confirmed_clause(only_confirmed) + _named_clause()
    if since is None:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(DISTINCT common_name) AS c FROM detections WHERE 1=1{src}",
                params,
            ).fetchone()
        return row["c"] if row else 0
    params.append(since)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT common_name FROM detections WHERE 1=1{src}
                GROUP BY common_name HAVING MIN(start_time) >= ?
            )
            """,
            params,
        ).fetchone()
    return row["c"] if row else 0


def species_list(
    source: Optional[str] = None,
    since: Optional[float] = None,
    only_new: bool = False,
    only_confirmed: bool = False,
    only_unconfirmed: bool = False,
) -> list[dict]:
    """Per-species aggregates for the species index.

    ``only_new`` keeps just species whose first-ever detection is at/after ``since``
    (first_seen/count are all-time). Otherwise, when ``since`` is given, results are
    limited to species active within the window (count is the in-window count).

    ``only_unconfirmed`` inverts the confirmation filter to build the review queue; it
    ignores ``only_confirmed``, since asking for both would always be empty.
    """
    params: list = []
    where = "WHERE 1=1" + _source_clause(source, params) + _named_clause()
    if only_unconfirmed:
        where += (" AND NOT EXISTS (SELECT 1 FROM species_confirmed sc"
                  " WHERE sc.common_name = detections.common_name COLLATE NOCASE)")
    else:
        where += _confirmed_clause(only_confirmed)
    having = ""
    if only_new and since is not None:
        having = " HAVING MIN(start_time) >= ?"
    elif since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    order = "first_seen DESC" if only_new else "count DESC, last_seen DESC"
    sql = f"""
        SELECT common_name,
               MAX(scientific_name) AS scientific_name,
               COUNT(*)             AS count,
               MIN(start_time)      AS first_seen,
               MAX(start_time)      AS last_seen,
               SUM(source = 'frigate') AS frigate_total,
               SUM(source = 'birdnet') AS birdnet_total,
               MIN(CASE WHEN source = 'frigate' THEN start_time END) AS first_frigate,
               MAX(CASE WHEN source = 'frigate' THEN start_time END) AS last_frigate,
               MIN(CASE WHEN source = 'birdnet' THEN start_time END) AS first_birdnet,
               MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS last_birdnet
        FROM detections {where}
        GROUP BY common_name{having}
        ORDER BY {order}
    """
    if only_new and since is not None:
        params.append(since)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def distinct_species(source: Optional[str] = None) -> list[str]:
    params: list = []
    where = "WHERE 1=1" + _source_clause(source, params) + _named_clause()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT common_name FROM detections {where} ORDER BY common_name",
            params,
        ).fetchall()
    return [r["common_name"] for r in rows]


def species_dex_numbers(only_confirmed: bool = False) -> dict[str, int]:
    """Registry number per species: 1-based order of first-ever detection.

    Derived rather than stored, so there's no counter to keep in sync with deletions.
    Removing a species renumbers the ones after it, which is fine — the number is a
    display flourish for the Pokedex theme, not an identifier anything keys on.

    Unconfirmed species get no number at all while the gate is on: the templates already
    render ``No.???`` for a missing one, so a species earns its entry by being approved.
    """
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   ROW_NUMBER() OVER (ORDER BY MIN(start_time), common_name) AS dex_no
            FROM detections
            WHERE 1=1{_confirmed_clause(only_confirmed)}{_named_clause()}
            GROUP BY common_name
            """
        ).fetchall()
    return {r["common_name"]: r["dex_no"] for r in rows}


def registry_stats(only_confirmed: bool = False) -> dict:
    """Species counts for the Pokedex completion readout.

    ``seen`` (on camera, the "caught" analogue) is the subset of ``total`` that has at
    least one Frigate detection; ``heard`` likewise for BirdNET-Go. A species can be
    both. There is no master checklist, so ``total`` is only what's been detected.

    ``unconfirmed`` is the review queue's size and is deliberately NOT filtered — it
    counts what's missing from the registry, so it's the one figure that has to look past
    the gate.
    """
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(seen)  AS seen,
                   SUM(heard) AS heard
            FROM (
                SELECT MAX(source = 'frigate') AS seen,
                       MAX(source = 'birdnet') AS heard
                FROM detections
                WHERE 1=1{_confirmed_clause(only_confirmed)}{_named_clause()}
                GROUP BY common_name
            )
            """
        ).fetchone()
    pending = unconfirmed_count() if only_confirmed else 0
    if not row:
        return {"total": 0, "seen": 0, "heard": 0, "unconfirmed": pending}
    return {
        "total": row["total"] or 0,
        "seen": row["seen"] or 0,
        "heard": row["heard"] or 0,
        "unconfirmed": pending,
    }


def latest_snapshot_refs(names: list[str]) -> dict[str, str]:
    """Newest Frigate snapshot event-ref per species, for thumbnails."""
    if not names:
        return {}
    marks = ",".join("?" * len(names))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name, snapshot_ref, MAX(start_time)
            FROM detections
            WHERE source = 'frigate' AND has_snapshot = 1 AND snapshot_ref IS NOT NULL
              AND common_name IN ({marks})
            GROUP BY common_name
            """,
            names,
        ).fetchall()
    return {r["common_name"]: r["snapshot_ref"] for r in rows}


def scientific_name_for(common_name: str) -> Optional[str]:
    """Latest known scientific name for a species (case-insensitive; any source)."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT scientific_name FROM detections
            WHERE common_name = ? COLLATE NOCASE
              AND scientific_name IS NOT NULL AND scientific_name != ''
            ORDER BY start_time DESC LIMIT 1
            """,
            (common_name,),
        ).fetchone()
    return row["scientific_name"] if row else None


def detection_by_id(det_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM detections WHERE id = ?", (det_id,)).fetchone()
    return dict(row) if row else None


def detection_by_ref(source: str, source_ref: str) -> Optional[dict]:
    """Current row for an upsert key — later source messages update the same row."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM detections WHERE source = ? AND source_ref = ?",
            (source, source_ref),
        ).fetchone()
    return dict(row) if row else None


def delete_detection(det_id: int) -> Optional[dict]:
    """Delete one detection and tombstone its ref. Returns the deleted row."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM detections WHERE id = ?", (det_id,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM detections WHERE id = ?", (det_id,))
        # Explicit rather than relying on ON DELETE CASCADE: SQLite enforces foreign keys
        # only when PRAGMA foreign_keys is on, and it is off by default on every new
        # connection. Orphaned embeddings would otherwise accumulate silently forever.
        conn.execute("DELETE FROM identification_embeddings WHERE detection_id = ?", (det_id,))
        conn.execute("DELETE FROM identification_rejections WHERE detection_id = ?", (det_id,))
        conn.execute(
            "INSERT OR REPLACE INTO deleted_refs (source, source_ref, deleted_at) VALUES (?, ?, ?)",
            (row["source"], row["source_ref"], time.time()),
        )
    return dict(row)


def delete_species(common_name: str) -> list[dict]:
    """Delete every detection of a species (case-insensitive), tombstoning each ref.

    Returns the deleted rows (the API uses their refs/native ids for source-side
    deletion).
    """
    with _connect() as conn:
        rows = [
            dict(r) for r in conn.execute(
                "SELECT * FROM detections WHERE common_name = ? COLLATE NOCASE",
                (common_name,),
            ).fetchall()
        ]
        if rows:
            now = time.time()
            conn.execute(
                "DELETE FROM detections WHERE common_name = ? COLLATE NOCASE",
                (common_name,),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO deleted_refs (source, source_ref, deleted_at) VALUES (?, ?, ?)",
                [(r["source"], r["source_ref"], now) for r in rows],
            )
            # See the note in delete_detection: ON DELETE CASCADE is not enforced unless
            # PRAGMA foreign_keys is on, which it is not.
            conn.executemany(
                "DELETE FROM identification_embeddings WHERE detection_id = ?",
                [(r["id"],) for r in rows],
            )
            conn.executemany(
                "DELETE FROM identification_rejections WHERE detection_id = ?",
                [(r["id"],) for r in rows],
            )
        conn.execute("DELETE FROM species_info WHERE common_name = ? COLLATE NOCASE", (common_name,))
        conn.execute("DELETE FROM species_audio WHERE common_name = ? COLLATE NOCASE", (common_name,))
        conn.execute("DELETE FROM species_photos WHERE common_name = ? COLLATE NOCASE", (common_name,))
        # Drop the approval too, so a species removed as a misclassification queues for
        # review again if it genuinely turns up later.
        conn.execute("DELETE FROM species_confirmed WHERE common_name = ? COLLATE NOCASE", (common_name,))
    return rows


# -------------------------------------------------------------------- identification

def set_identification(
    source: str,
    source_ref: str,
    status: str,
    score: Optional[float] = None,
    margin: Optional[float] = None,
    model: Optional[str] = None,
    embedding: Optional[str] = None,
    set_confidence: bool = False,
    candidates: Optional[str] = None,
) -> None:
    """Record the outcome of an external identification attempt.

    Kept separate from ``upsert_detection`` on purpose: that function is on the hot path
    for every MQTT message, whereas this runs once per event. Threading identification
    fields through the row dict would mean every caller that builds a row — including the
    BirdNET path, which has no identification — carrying five columns it does not use.

    ``set_confidence`` forces ``detections.confidence`` to ``score``, deliberately
    bypassing the "keep the best score" merge in ``upsert_detection``. That merge is
    correct while both values mean the same thing, but Frigate's score answers "is this a
    bird" (typically 0.85+) and ours answers "is this a Black-capped Chickadee". Taking
    the maximum would put Frigate's high object score in the field the UI labels as
    species confidence, making an uncertain identification look authoritative.
    """
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE detections SET
                id_status = ?, id_score = ?, id_margin = ?, id_model = ?, id_at = ?,
                -- COALESCE so a later status-only update (a retry that failed, say) does
                -- not wipe a shortlist we already have to show the user.
                id_candidates = COALESCE(?, id_candidates)
                {", confidence = ?" if set_confidence else ""}
            WHERE source = ? AND source_ref = ?
            """,
            (status, score, margin, model, time.time(), candidates)
            + ((score,) if set_confidence else ())
            + (source, source_ref),
        )
        if embedding and model:
            row = conn.execute(
                "SELECT id FROM detections WHERE source = ? AND source_ref = ?",
                (source, source_ref),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    INSERT INTO identification_embeddings
                        (detection_id, model, embedding, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(detection_id) DO UPDATE SET
                        model = excluded.model,
                        embedding = excluded.embedding,
                        created_at = excluded.created_at
                    """,
                    (row["id"], model, embedding, time.time()),
                )


def set_species_manually(detection_id: int, common_name: str,
                         scientific_name: Optional[str] = None,
                         species_code: Optional[str] = None) -> Optional[dict]:
    """Name a detection by hand. Returns the updated row.

    Written directly rather than through ``upsert_detection`` because that function's merge
    rules exist to reconcile repeated messages from a source, and none of them apply here:
    a person typing a species is the most authoritative input there is, and it must be able
    to overwrite whatever the model decided — including replacing a name with a different
    one, which the "never downgrade" rule would otherwise fight.

    Confidence is cleared rather than set to 1.0: the field means "how sure was the
    classifier", and a human answer has no place on that scale. ``id_status = 'manual'``
    is what records that this was a person.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE detections SET
                common_name = ?, scientific_name = ?, species_code = ?,
                confidence = NULL, id_status = 'manual', id_at = ?
            WHERE id = ?
            """,
            (common_name, scientific_name, species_code, time.time(), detection_id),
        )
        if not cur.rowcount:
            return None
        row = conn.execute("SELECT * FROM detections WHERE id = ?", (detection_id,)).fetchone()
    return dict(row) if row else None


def reject_identification(detection_id: int, species: str) -> None:
    """Record that a species is the wrong answer for this specific detection."""
    if not (species or "").strip() or species.strip().lower() == "bird":
        return  # "bird" is the absence of an answer, not an answer to reject
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO identification_rejections
                (detection_id, species, rejected_at) VALUES (?, ?, ?)
            """,
            (detection_id, species.strip(), time.time()),
        )


def reset_species(detection_id: int) -> None:
    """Strip a detection back to an unnamed 'bird'.

    Needed before re-identifying a rejected answer. ``upsert_detection`` deliberately
    refuses to downgrade a named species back to 'bird' — that rule protects against a
    later Frigate message arriving without a sub_label — so if the reroll came back below
    threshold and never re-stored the row, the name the user just rejected would stay on
    screen. Clearing it first means a failed reroll correctly leaves the detection
    unidentified and in the review queue.
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE detections SET
                common_name = 'bird', scientific_name = NULL,
                species_code = NULL, confidence = NULL
            WHERE id = ?
            """,
            (detection_id,),
        )


def rejections_for(detection_id: int) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT species FROM identification_rejections WHERE detection_id = ?",
            (detection_id,),
        ).fetchall()
    return [r["species"] for r in rows]


def clear_rejections(detection_id: int) -> None:
    """Start over on a detection whose rejections have painted it into a corner."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM identification_rejections WHERE detection_id = ?", (detection_id,)
        )


def drop_detection(source: str, source_ref: str) -> None:
    """Remove a row without tombstoning it.

    Distinct from ``delete_detection``, which records a tombstone because the user chose
    to remove something. This is for a row Aviary itself stored provisionally and then
    decided against (an identification that resolved to a blacklisted species). A
    tombstone would be wrong: it would permanently block re-import of an event the user
    might later un-blacklist.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM detections WHERE source = ? AND source_ref = ?",
            (source, source_ref),
        ).fetchone()
        if row is None:
            return
        conn.execute("DELETE FROM detections WHERE id = ?", (row["id"],))
        conn.execute(
            "DELETE FROM identification_embeddings WHERE detection_id = ?", (row["id"],)
        )
        conn.execute(
            "DELETE FROM identification_rejections WHERE detection_id = ?", (row["id"],)
        )


def confirmed_embeddings(model: str) -> list[tuple[str, str]]:
    """(species, embedding) for every confirmed detection identified by ``model``.

    Confirmed only: the whole point is to learn from labels a human stands behind. An
    unreviewed automatic guess would teach the classifier its own mistakes, which is how a
    feedback loop starts.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.common_name AS name, e.embedding AS embedding
            FROM identification_embeddings e
            JOIN detections d ON d.id = e.detection_id
            JOIN species_confirmed sc ON sc.common_name = d.common_name COLLATE NOCASE
            WHERE e.model = ? AND d.common_name != 'bird' COLLATE NOCASE
            """,
            (model,),
        ).fetchall()
    return [(r["name"], r["embedding"]) for r in rows]


def reference_embeddings(model: str) -> list[tuple[str, str]]:
    """(species, embedding) for the bootstrap reference photos."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT common_name AS name, embedding FROM species_reference_embeddings WHERE model = ?",
            (model,),
        ).fetchall()
    return [(r["name"], r["embedding"]) for r in rows]


def put_reference_embedding(common_name: str, position: int, model: str,
                            embedding: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO species_reference_embeddings
                (common_name, position, model, embedding, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(common_name, position, model) DO UPDATE SET
                embedding = excluded.embedding, created_at = excluded.created_at
            """,
            (common_name, position, model, embedding, time.time()),
        )


def species_missing_reference_embeddings(model: str, limit: int = 50) -> list[dict]:
    """Cached reference photos with no embedding yet, for the given model.

    Drives the bootstrap: photos are already in ``species_photos`` (fetched so a human can
    compare a detection against known pictures), so this only has to find the gaps.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT p.common_name, p.position, p.file_url, p.thumb_url
            FROM species_photos p
            WHERE p.ok = 1 AND p.file_url IS NOT NULL AND p.file_url != ''
              AND p.common_name != 'bird' COLLATE NOCASE
              AND NOT EXISTS (
                  SELECT 1 FROM species_reference_embeddings r
                  WHERE r.common_name = p.common_name COLLATE NOCASE
                    AND r.position = p.position AND r.model = ?
              )
            ORDER BY p.common_name, p.position
            LIMIT ?
            """,
            (model, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def species_heard_between(start: float, end: float) -> list[str]:
    """Distinct species BirdNET-Go heard in a time window.

    Used as a prior for visual identification: a bird whose song was picked up by the
    microphone two minutes ago is genuinely more likely to be the one in the picture.
    BirdNET only — a Frigate row in the window is another *visual* guess, and feeding
    those back in would just reinforce whatever the model already tends to say.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT common_name FROM detections
            WHERE source = 'birdnet' AND start_time BETWEEN ? AND ?
              AND common_name != '' AND common_name != 'bird'
            """,
            (start, end),
        ).fetchall()
    return [r["common_name"] for r in rows]


def pending_identifications(limit: int = 500) -> list[dict]:
    """Detections stuck in 'pending', oldest first — requeued at startup.

    A restart mid-flight (add-on update, host reboot) otherwise strands these forever:
    the MQTT ``end`` message that would have triggered them is long gone.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM detections
            WHERE id_status = 'pending'
            ORDER BY start_time ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# A detection with no species. Deliberately defined by the name rather than by id_status:
# this is exactly the set _named_clause() keeps out of the species registry, so the tab and
# the registry can never disagree about what counts as identified. It also catches rows
# that predate identification, which have no status at all.
_UNIDENTIFIED = f"common_name = '{UNNAMED}' COLLATE NOCASE"


def unidentified_detections(limit: int = 100, before: Optional[float] = None,
                            include_pending: bool = False) -> list[dict]:
    """Detections still waiting for a species, newest first.

    ``include_pending`` covers rows in flight to the identification service. Off by
    default: they resolve within seconds and are reported as a count instead, so the list
    stays a to-do rather than a progress bar.
    """
    params: list = []
    where = f"WHERE {_UNIDENTIFIED}"
    if not include_pending:
        where += " AND (id_status IS NULL OR id_status != 'pending')"
    if before is not None:
        where += " AND start_time < ?"
        params.append(before)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM detections {where} ORDER BY start_time DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def unidentified_counts() -> dict:
    """Actionable vs in-flight counts for the tab badge and header."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN id_status = 'pending' THEN 1 ELSE 0 END)  AS pending,
                SUM(CASE WHEN id_status IS NULL OR id_status != 'pending'
                         THEN 1 ELSE 0 END)                              AS actionable
            FROM detections WHERE {_UNIDENTIFIED}
            """
        ).fetchone()
    return {
        "pending": int((row["pending"] if row else 0) or 0),
        # What the badge shows: things you can actually do something about.
        "actionable": int((row["actionable"] if row else 0) or 0),
    }


def unidentified_count() -> int:
    return unidentified_counts()["actionable"]


def purge_unidentified(older_than: float) -> int:
    """Drop stale unidentifiable rows. Returns the number removed.

    These are kept deliberately (with Frigate's own classifier off, dropping them would
    mean no record a bird was ever there) but they must not grow without bound on a busy
    feeder. No tombstone is written: this is housekeeping, not a user deletion, and
    tombstoning would stop a future backfill from re-importing a row that a better model
    could since identify.
    """
    with _connect() as conn:
        ids = [
            r["id"] for r in conn.execute(
                """
                SELECT id FROM detections
                WHERE id_status IN ('failed', 'low_confidence') AND start_time < ?
                """,
                (older_than,),
            ).fetchall()
        ]
        if not ids:
            return 0
        conn.executemany("DELETE FROM detections WHERE id = ?", [(i,) for i in ids])
        conn.executemany(
            "DELETE FROM identification_embeddings WHERE detection_id = ?", [(i,) for i in ids]
        )
        conn.executemany(
            "DELETE FROM identification_rejections WHERE detection_id = ?", [(i,) for i in ids]
        )
    return len(ids)


def tombstoned_refs() -> list[tuple[str, str]]:
    """All (source, source_ref) pairs the user has deleted."""
    with _connect() as conn:
        rows = conn.execute("SELECT source, source_ref FROM deleted_refs").fetchall()
    return [(r["source"], r["source_ref"]) for r in rows]


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


def canonical_species(name: str) -> Optional[dict]:
    """Canonical ``{common_name, scientific_name}`` for an incoming species label.

    Handles cross-source naming drift: a label that matches another species'
    scientific name maps to that species' common name (Frigate's classifier emits
    scientific names while BirdNET-Go emits common names); otherwise a
    case-insensitive match adopts the spelling already stored, preferring rows that
    carry a scientific name (BirdNET's proper-cased entries). None when unknown.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT common_name, scientific_name FROM detections
            WHERE scientific_name = ? COLLATE NOCASE
              AND common_name != ''
              AND LOWER(common_name) != LOWER(scientific_name)
            ORDER BY start_time DESC LIMIT 1
            """,
            (name,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT common_name, scientific_name FROM detections
                WHERE common_name = ? COLLATE NOCASE
                ORDER BY (scientific_name IS NOT NULL) DESC, start_time DESC
                LIMIT 1
                """,
                (name,),
            ).fetchone()
    return dict(row) if row else None


def species_last_times(common_name: str, source: str, source_ref: str) -> dict:
    """The species' most recent detection time — overall and per source — excluding
    one row (already upserted).

    Feeds the notification blueprint's per-species cooldown: 'how long has this
    species been quiet before this detection?', split by source so a camera cooldown
    isn't fed by audio detections (and vice versa).
    """
    empty = {"any": None, "seen": None, "heard": None}
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT MAX(start_time) AS any_t,
                   MAX(CASE WHEN source = 'frigate' THEN start_time END) AS seen_t,
                   MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS heard_t
            FROM detections
            WHERE common_name = ? COLLATE NOCASE
              AND NOT (source = ? AND source_ref = ?)
            """,
            (common_name, source, source_ref),
        ).fetchone()
    if not row:
        return empty
    return {"any": row["any_t"], "seen": row["seen_t"], "heard": row["heard_t"]}


def recent_refs(since: float) -> list[tuple[str, str]]:
    """(source, source_ref) pairs of recent detections, to pre-mark them announced."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT source, source_ref FROM detections WHERE start_time >= ?",
            (since,),
        ).fetchall()
    return [(r["source"], r["source_ref"]) for r in rows]


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
                family, "order", conservation, fetched_at, ok, inat_taxon_id
            ) VALUES (
                :common_name, :scientific_name, :descriptor, :extract, :wiki_url,
                :family, :order, :conservation, :fetched_at, :ok, :inat_taxon_id
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
                inat_taxon_id   = COALESCE(excluded.inat_taxon_id, species_info.inat_taxon_id)
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


def summary_stats(source: Optional[str] = None, since: Optional[float] = None,
                  only_confirmed: bool = False) -> dict:
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    where += _confirmed_clause(only_confirmed)
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*)                     AS total,
                -- Species count excludes unidentified rows, but `total` deliberately
                -- does NOT: a bird nobody could name was still a bird that showed up,
                -- and dropping it would understate the detection count for the day.
                COUNT(DISTINCT CASE WHEN common_name != 'bird' COLLATE NOCASE
                                    THEN common_name END) AS species,
                SUM(source = 'frigate')      AS frigate_total,
                SUM(source = 'birdnet')      AS birdnet_total
            FROM detections {where}
            """,
            params,
        ).fetchone()
    return dict(row) if row else {}


def top_species(
    limit: int = 10,
    source: Optional[str] = None,
    since: Optional[float] = None,
    only_confirmed: bool = False,
) -> list[dict]:
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    where += _confirmed_clause(only_confirmed)
    where += _named_clause()
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name, scientific_name,
                   COUNT(*) AS count, MAX(start_time) AS last_seen,
                   SUM(source = 'birdnet') AS heard,
                   SUM(source = 'frigate') AS seen,
                   MAX(CASE WHEN source = 'frigate' THEN start_time END) AS last_frigate,
                   MAX(CASE WHEN source = 'birdnet' THEN start_time END) AS last_birdnet
            FROM detections {where}
            GROUP BY common_name
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def detections_per_day(
    days: int = 30,
    source: Optional[str] = None,
    species: Optional[str] = None,
    since: Optional[float] = None,
) -> list[dict]:
    """Count per local day (SQLite localtime); ``since`` epoch overrides ``days``."""
    params: list = []
    if since is not None:
        where = "WHERE start_time >= ?"
        params.append(since)
    else:
        days = max(1, min(int(days), 3650))
        where = "WHERE start_time >= CAST(strftime('%s', 'now', ?) AS REAL)"
        params.append(f"-{days} days")
    where += _source_clause(source, params)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT date(start_time, 'unixepoch', 'localtime') AS day,
                   COUNT(*) AS count
            FROM detections {where}
            GROUP BY day
            ORDER BY day
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def hourly_activity(
    source: Optional[str] = None,
    since: Optional[float] = None,
    species: Optional[str] = None,
) -> list[dict]:
    """Count per hour-of-day (0-23), aggregated across all matching detections."""
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    if since is not None:
        where += " AND start_time >= ?"
        params.append(since)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT CAST(strftime('%H', start_time, 'unixepoch', 'localtime') AS INTEGER) AS hour,
                   COUNT(*) AS count
            FROM detections {where}
            GROUP BY hour
            ORDER BY hour
            """,
            params,
        ).fetchall()
    counts = {int(r["hour"]): r["count"] for r in rows}
    return [{"hour": h, "count": counts.get(h, 0)} for h in range(24)]
