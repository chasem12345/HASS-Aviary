"""SQLite storage + analytics queries for Aviary.

A single ``detections`` table holds rows from both sources, tagged by ``source``.
There is no cross-source correlation. Connections are opened per-operation (SQLite in
WAL mode handles concurrent readers/one writer well), which keeps the MQTT ingest thread
and the FastAPI event loop cleanly separated.
"""

from __future__ import annotations

import json
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
-- These are the probe's training data (see probe.py): each confirmed species in
-- `species_confirmed` turns its detections' embeddings into labelled examples from your
-- own cameras, and the probe blends nearest-example similarity against them into the
-- zero-shot answer. Captured at identification time because reconstructing them later
-- means re-downloading and re-running every clip in the history through the GPU.
--
-- Stored as a base64 float16 blob keyed by the service's embedding_key (the model name
-- alone — NOT the full model_version, whose vocabulary digest would orphan every row on
-- a routine species-list refresh). Vectors from different models are not comparable.
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
-- no event, no clip, and no detection_id to hang off. They also count for less when
-- matching: a posed photo in good light is the right species but the wrong domain, and
-- one real frame from your own feeder is worth several of them.
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

-- Visits: one row per Frigate REVIEW ITEM that involved a bird. Frigate's tracker splits
-- a single bird's stay into many short tracked objects (a bird hopping around a bath for
-- 90 s is routinely 10-25 event ids), and its Review page is what re-joins them into one
-- stretch of activity per camera. Aviary mirrors that grouping exactly — same ids, same
-- boundaries (Frigate's review cutoff_time) — so "one visit" means the same thing in both
-- UIs and the window is tuned in one place. Members are detections.visit_id.
--
-- start/end are widened to cover the members: Frigate closes a review item on its own
-- clock and the last tracked object can outlive it by a few seconds.
CREATE TABLE IF NOT EXISTS visits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id   TEXT NOT NULL UNIQUE,   -- Frigate review item id
    camera      TEXT NOT NULL,          -- lowercased camera name
    start_time  REAL NOT NULL,          -- MIN(review start, members' start)
    end_time    REAL,                   -- MAX(review end, members' end); NULL while open
    review_start REAL NOT NULL,         -- Frigate's own bounds, unmodified
    review_end  REAL,
    severity    TEXT,                   -- 'detection' | 'alert'
    zones       TEXT,                   -- comma-joined, like detections.zone
    raw_json    TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visits_camera_start ON visits (camera, start_time);
CREATE INDEX IF NOT EXISTS idx_visits_start ON visits (start_time);

-- Event ids Frigate listed under a review item. The review message and the event
-- messages race on MQTT (both fire when the object appears), and a review item's member
-- list grows over its lifetime — so the link is made from whichever side arrives second:
-- upsert_visit stamps members that already exist, upsert_detection looks itself up here.
-- One event belongs to at most one visit.
CREATE TABLE IF NOT EXISTS visit_refs (
    source_ref TEXT PRIMARY KEY,
    visit_id   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visit_refs_visit ON visit_refs (visit_id);

-- (visit, species) pairs already announced as an aviary_detection event. Notifications
-- fire once per species per visit rather than once per Frigate event; persisted (not an
-- in-memory set) because identification results land seconds after the event ends and
-- may straddle an add-on restart.
CREATE TABLE IF NOT EXISTS visit_announced (
    visit_id     INTEGER NOT NULL,
    common_name  TEXT NOT NULL COLLATE NOCASE,
    detection_id INTEGER,
    announced_at REAL NOT NULL,
    PRIMARY KEY (visit_id, common_name)
);

-- Subjects: every BIRD the identification service found in one event, classified on its
-- own crops (aviary-id 0.10.0+). idx 0 is the primary — the tracked object the event is
-- about, which the detections row itself mirrors — and the rest are birds that shared the
-- view. Each has its own crop file and embedding, so labelling one can never file a
-- picture of the other under that name. Rows are replaced wholesale on re-identify; a
-- human label (manual_name) is carried across by matching embeddings.
--
-- The primary's row exists only so labelling logic is uniform: detections.* and
-- identification_embeddings stay the source of truth for it, and every existing query is
-- untouched. A detection with no subject rows simply IS its one subject.
CREATE TABLE IF NOT EXISTS detection_subjects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id    INTEGER NOT NULL REFERENCES detections(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,             -- 0 = primary, as the service numbered them
    is_primary      INTEGER NOT NULL DEFAULT 0,
    common_name     TEXT,                         -- service answer after the probe; NULL below threshold
    scientific_name TEXT,
    species_code    TEXT,
    score           REAL,
    margin          REAL,
    candidates      TEXT,                         -- JSON top-5, same slim shape as detections.id_candidates
    id_status       TEXT NOT NULL,                -- ok | low_confidence | manual | rejected
    manual_name     TEXT,                         -- a person's label; outranks common_name
    manual_sci      TEXT,
    embedding_model TEXT,
    embedding       TEXT,
    crop_file       TEXT,                         -- basename under /data/crops
    anchored        INTEGER NOT NULL DEFAULT 1,
    n_frames        INTEGER,
    created_at      REAL NOT NULL,
    UNIQUE (detection_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_subjects_detection ON detection_subjects (detection_id);

-- "Not a Blue Jay" said about one OTHER bird in an event (the primary's rejections live
-- in identification_rejections). Enforced add-on side when the service's answers come
-- back; the service's own exclude list is per call, not per subject.
CREATE TABLE IF NOT EXISTS detection_subject_rejections (
    detection_id INTEGER NOT NULL,
    idx          INTEGER NOT NULL,
    species      TEXT NOT NULL COLLATE NOCASE,
    rejected_at  REAL NOT NULL,
    PRIMARY KEY (detection_id, idx, species)
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
            # Probe provenance: how much the few-shot blend influenced this answer and
            # how many of the user's confirmed examples backed it. NULL means the probe
            # abstained (or predates these columns).
            ("id_probe_weight", "REAL"), ("id_probe_examples", "INTEGER"),
            # Frigate zones the object entered, comma-joined ("bird_bath, porch").
            # Separate from `location` (the camera name), which the notification
            # blueprint's camera allowlist filters on.
            ("zone", "TEXT"),
            # When the user pinned this event's clip as kept-forever at Frigate
            # (retain_indefinitely). NULL = not retained. Deliberately absent from
            # upsert_detection so a later Frigate message can't clobber the flag.
            ("retained_at", "REAL"),
            # Frigate export of the PAIRED camera's window for a kept event — the zoomed
            # footage, which plain retain_indefinitely cannot protect (it is continuous
            # recordings, not part of the event). `kept_export_file` is the served
            # basename, resolved lazily once the async export finishes. User state,
            # like retained_at: never touched by upsert_detection.
            ("kept_export_id", "TEXT"), ("kept_export_file", "TEXT"),
            # The Frigate review item (visits.id) this event belongs to. NULL for BirdNET
            # rows, for events whose review item Frigate no longer retains, and for
            # history from before review items existed — those render and count as
            # standalone events, exactly as they always did.
            ("visit_id", "INTEGER"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE detections ADD COLUMN {name} {decl}")
        # Same reasoning as idx_detections_id_status below: the column is added by the
        # loop above, so the index cannot live in _SCHEMA.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_detections_visit ON detections (visit_id)"
        )
        # One-time, best-effort zone backfill for rows that predate the zone column.
        # raw_json stores the Frigate `after` object (size-capped, largest values
        # dropped first — entered_zones is small and usually survives; some rows have
        # raw_json NULL and simply stay zoneless).
        migrated = conn.execute(
            "SELECT value FROM app_prefs WHERE key = 'zone_backfill_done'"
        ).fetchone()
        if not migrated:
            rows = conn.execute(
                """
                SELECT id, raw_json FROM detections
                WHERE source = 'frigate' AND zone IS NULL AND raw_json IS NOT NULL
                """
            ).fetchall()
            for det_id, raw in rows:
                try:
                    obj = json.loads(raw)
                    zones = (obj.get("entered_zones") or obj.get("zones")
                             or obj.get("current_zones") or [])
                except (ValueError, TypeError, AttributeError):
                    continue
                joined = ", ".join(str(z) for z in zones if z)
                if joined:
                    conn.execute("UPDATE detections SET zone = ? WHERE id = ?",
                                 (joined, det_id))
            conn.execute(
                "INSERT OR REPLACE INTO app_prefs (key, value) "
                "VALUES ('zone_backfill_done', '1')"
            )
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
        # Embeddings used to be keyed by the service's full model_version
        # ("model/label_format@vocab_digest"), whose digest hashes the entire species
        # list. That was wrong for IMAGE embeddings: they come from the frozen image
        # tower and are untouched by vocabulary or label-format changes — yet a routine
        # eBird regional-list refresh changed the digest and silently orphaned every
        # learned example. Rewrite old keys down to the bare model name, once. Keys are
        # rewritten, never deleted: these rows are the user's accumulated training data.
        migrated = conn.execute(
            "SELECT value FROM app_prefs WHERE key = 'embedding_key_migrated'"
        ).fetchone()
        if not migrated:
            for table in ("identification_embeddings", "species_reference_embeddings"):
                olds = [
                    r[0] for r in conn.execute(f"SELECT DISTINCT model FROM {table}")
                ]
                for old in olds:
                    new = embedding_key_from(old)
                    if new == old:
                        continue
                    # OR IGNORE: species_reference_embeddings' primary key includes the
                    # model, so rows may already exist under BOTH an old digest and the
                    # new key (two historical digests, or a re-bootstrap after this
                    # change). Colliding old rows are dropped in favour of the newer key.
                    conn.execute(
                        f"UPDATE OR IGNORE {table} SET model = ? WHERE model = ?",
                        (new, old),
                    )
                    conn.execute(f"DELETE FROM {table} WHERE model = ?", (old,))
            conn.execute(
                "INSERT OR REPLACE INTO app_prefs (key, value) "
                "VALUES ('embedding_key_migrated', '1')"
            )


def embedding_key_from(model_version: str) -> str:
    """Reduce a service model_version to the key embeddings are stored under.

    The full form is ``{model_name}/{label_format}@{vocab_digest}`` — the model name
    itself may contain slashes ("hf-hub:imageomics/bioclip-2"), so strip from the right:
    the digest after the last ``@``, then the label format after the last remaining
    ``/``. A string with no ``@`` is already a bare key and passes through unchanged,
    which is what makes this safe to apply to values from either an old or a new service.
    """
    if "@" not in model_version:
        return model_version
    return model_version.rsplit("@", 1)[0].rsplit("/", 1)[0]


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

    Side effect on the caller's dict: ``row["id"]`` is filled in, and for a Frigate event
    that a review item has claimed, ``row["visit_id"]`` too — the notification path keys
    its once-per-visit dedupe on it, and the tap-action deep link needs the id.
    """
    # Only Frigate rows carry a zone; default it so BirdNET's row shape (and any older
    # caller) satisfies the named parameters without every builder growing the key.
    defaults = {"zone": None}
    params = {**defaults, **row}
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO detections (
                source, source_ref, common_name, scientific_name, species_code,
                confidence, location, zone, start_time, end_time,
                has_clip, has_snapshot, clip_ref, snapshot_ref, native_id,
                raw_json, created_at
            ) VALUES (
                :source, :source_ref, :common_name, :scientific_name, :species_code,
                :confidence, :location, :zone, :start_time, :end_time,
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
                -- COALESCE, not last-write-wins: entered_zones is cumulative so the end
                -- message carries the full list, but a message WITHOUT zones (or a
                -- BirdNET row shape) must not wipe one already recorded.
                zone            = COALESCE(excluded.zone, detections.zone),
                end_time        = COALESCE(excluded.end_time, detections.end_time),
                has_clip        = MAX(detections.has_clip, excluded.has_clip),
                has_snapshot    = MAX(detections.has_snapshot, excluded.has_snapshot),
                clip_ref        = COALESCE(excluded.clip_ref, detections.clip_ref),
                snapshot_ref    = COALESCE(excluded.snapshot_ref, detections.snapshot_ref),
                native_id       = COALESCE(excluded.native_id, detections.native_id),
                raw_json        = excluded.raw_json
            """,
            params,
        )
        stored = conn.execute(
            "SELECT id, visit_id FROM detections WHERE source = ? AND source_ref = ?",
            (params["source"], params["source_ref"]),
        ).fetchone()
        if stored is None:  # cannot happen after a successful upsert; be defensive
            return
        row["id"] = stored["id"]
        visit_id = stored["visit_id"]
        if params["source"] == "frigate":
            if visit_id is None:
                # The review message may have listed this event before its own first
                # message arrived (or before Aviary stored it) — pick the link up now.
                ref = conn.execute(
                    "SELECT visit_id FROM visit_refs WHERE source_ref = ?",
                    (params["source_ref"],),
                ).fetchone()
                if ref is not None:
                    visit_id = ref["visit_id"]
                    conn.execute("UPDATE detections SET visit_id = ? WHERE id = ?",
                                 (visit_id, stored["id"]))
            if visit_id is not None:
                # A later message can extend the event's end_time past the review item's.
                _refresh_visit(conn, visit_id)
        row["visit_id"] = visit_id


# ---------------------------------------------------------------------------- visits

def _refresh_visit(conn: sqlite3.Connection, visit_id: int) -> None:
    """Recompute a visit's bounds from Frigate's review bounds and its members.

    The start is the earliest of the two, the end the latest — Frigate closes a review
    item on its own clock and the last tracked object can outlive it by seconds. A visit
    Frigate still has open (no review end) stays open regardless of member ends, so the
    card reads "in progress" for exactly as long as Frigate's Review page does.
    """
    conn.execute(
        """
        UPDATE visits SET
            start_time = MIN(review_start, COALESCE(
                (SELECT MIN(start_time) FROM detections WHERE visit_id = visits.id),
                review_start)),
            end_time = CASE WHEN review_end IS NULL THEN NULL ELSE MAX(review_end, COALESCE(
                (SELECT MAX(COALESCE(end_time, start_time)) FROM detections
                 WHERE visit_id = visits.id),
                review_end)) END
        WHERE id = ?
        """,
        (visit_id,),
    )


def upsert_visit(row: dict[str, Any]) -> int:
    """Store or update a Frigate review item and link its listed events. Returns visits.id.

    ``row`` is ``ingest.build_review_row``'s shape; ``refs`` is the review item's
    ``data.detections`` list, which grows over the item's lifetime — every message
    re-stamps the full list, so a member missed on one message is caught on the next.
    Members that already exist as detections are linked here; members that arrive later
    link themselves from ``visit_refs`` in ``upsert_detection``.
    """
    refs = [str(r) for r in (row.get("refs") or []) if r]
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO visits (
                review_id, camera, start_time, end_time, review_start, review_end,
                severity, zones, raw_json, created_at, updated_at
            ) VALUES (
                :review_id, :camera, :start_time, :end_time, :start_time, :end_time,
                :severity, :zones, :raw_json, :now, :now
            )
            ON CONFLICT(review_id) DO UPDATE SET
                camera       = excluded.camera,
                review_start = MIN(visits.review_start, excluded.review_start),
                -- A later message never un-ends a visit; an `update` after `end` does
                -- not happen in practice, but a replayed/backfilled item must not reopen.
                review_end   = COALESCE(excluded.review_end, visits.review_end),
                severity     = COALESCE(excluded.severity, visits.severity),
                zones        = COALESCE(excluded.zones, visits.zones),
                raw_json     = COALESCE(excluded.raw_json, visits.raw_json),
                updated_at   = excluded.updated_at
            """,
            {
                "review_id": row["review_id"],
                "camera": (row.get("camera") or "").lower(),
                "start_time": float(row["start_time"]),
                "end_time": row.get("end_time"),
                "severity": row.get("severity"),
                "zones": row.get("zones"),
                "raw_json": row.get("raw_json"),
                "now": now,
            },
        )
        visit_id = conn.execute(
            "SELECT id FROM visits WHERE review_id = ?", (row["review_id"],)
        ).fetchone()["id"]
        if refs:
            conn.executemany(
                "INSERT OR REPLACE INTO visit_refs (source_ref, visit_id) VALUES (?, ?)",
                [(ref, visit_id) for ref in refs],
            )
            marks = ",".join("?" for _ in refs)
            conn.execute(
                f"""
                UPDATE detections SET visit_id = ?
                WHERE source = 'frigate' AND source_ref IN ({marks})
                  AND (visit_id IS NULL OR visit_id != ?)
                """,
                [visit_id, *refs, visit_id],
            )
            # A member that already HAD a species when it got linked was announced on
            # its own (per-event) before the review item listed it — or was labelled by
            # hand, or backfilled, none of which should notify again. Record those
            # species as announced for the visit so a later sibling stays silent.
            conn.execute(
                f"""
                INSERT OR IGNORE INTO visit_announced
                    (visit_id, common_name, detection_id, announced_at)
                SELECT visit_id, common_name, id, ?
                FROM detections
                WHERE visit_id = ? AND source_ref IN ({marks})
                  {_named_clause()}
                """,
                [now, visit_id, *refs],
            )
        _refresh_visit(conn, visit_id)
    return visit_id


def _visit_ids_of(conn: sqlite3.Connection, det_ids: list[int]) -> list[int]:
    """Visits the given detections belong to. Read BEFORE deleting them (the members
    must still exist to be looked up); pair with ``_settle_visits`` afterwards."""
    if not det_ids:
        return []
    marks = ",".join("?" for _ in det_ids)
    rows = conn.execute(
        f"SELECT DISTINCT visit_id FROM detections WHERE id IN ({marks}) "
        f"AND visit_id IS NOT NULL",
        det_ids,
    ).fetchall()
    return [r["visit_id"] for r in rows]


def _settle_visits(conn: sqlite3.Connection, visit_ids: list[int]) -> None:
    """After members were deleted: shrink bounds, forget announcements for species that
    no longer have a member, and drop visits left with no members at all.

    The announcement cleanup mirrors ``_forget_if_gone`` for species: a deleted
    misclassification must not leave "already announced" behind, or the real bird that
    turns up in the same visit a minute later would be silent.
    """
    for vid in visit_ids:
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM detections WHERE visit_id = ?", (vid,)
        ).fetchone()["c"]
        if not remaining:
            conn.execute("DELETE FROM visits WHERE id = ?", (vid,))
            conn.execute("DELETE FROM visit_refs WHERE visit_id = ?", (vid,))
            conn.execute("DELETE FROM visit_announced WHERE visit_id = ?", (vid,))
            continue
        conn.execute(
            """
            DELETE FROM visit_announced
            WHERE visit_id = ? AND NOT EXISTS (
                SELECT 1 FROM detections d
                WHERE d.visit_id = visit_announced.visit_id
                  AND d.common_name = visit_announced.common_name COLLATE NOCASE
            )
            """,
            (vid,),
        )
        _refresh_visit(conn, vid)


def claim_visit_announcement(visit_id: int, common_name: str,
                             detection_id: Optional[int]) -> bool:
    """Atomically mark (visit, species) announced. True only for the first claimant."""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO visit_announced
                (visit_id, common_name, detection_id, announced_at)
            VALUES (?, ?, ?, ?)
            """,
            (visit_id, common_name, detection_id, time.time()),
        )
        return cur.rowcount == 1


def seed_visit_announcements(since: float) -> int:
    """Pre-mark every named species of recent/open visits as announced.

    Startup only — the persisted twin of ``ingest.seed_notify_state``'s recent-refs
    pre-marking, so upgrading (or restarting) mid-visit doesn't re-notify for birds that
    were announced before the restart. Returns the number of rows added.
    """
    with _connect() as conn:
        cur = conn.execute(
            f"""
            INSERT OR IGNORE INTO visit_announced
                (visit_id, common_name, detection_id, announced_at)
            SELECT d.visit_id, d.common_name, MIN(d.id), ?
            FROM detections d JOIN visits v ON v.id = d.visit_id
            WHERE (v.end_time IS NULL OR v.end_time >= ?)
              {_named_clause('d')}
            GROUP BY d.visit_id, d.common_name
            """,
            (time.time(), since),
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def visit_by_id(visit_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM visits WHERE id = ?", (visit_id,)).fetchone()
    return dict(row) if row else None


def visit_by_review_id(review_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM visits WHERE review_id = ?", (review_id,)
        ).fetchone()
    return dict(row) if row else None


def visit_members(visit_id: int) -> list[dict]:
    """A visit's detections, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM detections WHERE visit_id = ? ORDER BY start_time",
            (visit_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def visits_with_members(visit_ids: list[int]) -> dict[int, list[dict]]:
    """Members for many visits in one query, keyed by visit id, oldest first."""
    if not visit_ids:
        return {}
    marks = ",".join("?" for _ in visit_ids)
    out: dict[int, list[dict]] = {vid: [] for vid in visit_ids}
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM detections WHERE visit_id IN ({marks}) ORDER BY start_time",
            visit_ids,
        ).fetchall()
    for r in rows:
        out.setdefault(r["visit_id"], []).append(dict(r))
    return out


def _zone_like(column: str, params: list, zone: str) -> str:
    """Delimiter-safe match of one zone inside a comma-joined list column."""
    params.append(zone.replace(" ", ""))
    return f" ',' || REPLACE(COALESCE({column}, ''), ' ', '') || ',' LIKE '%,' || ? || ',%'"


def feed_page(
    limit: int = 60,
    source: Optional[str] = None,
    species: Optional[str] = None,
    before: Optional[float] = None,
    since: Optional[float] = None,
    zone: Optional[str] = None,
) -> list[dict]:
    """Newest-first feed items: visits (with members) plus ungrouped detections.

    The Recent page's unit of display. A Frigate event that belongs to a visit is shown
    only inside that visit; BirdNET rows and Frigate events with no review item (older
    than Frigate's review retention, or from before review items existed) appear as their
    own cards, exactly as before. ``before``/``since`` are start_time cursors, valid for
    both kinds because a visit's start_time is a real timestamp too.

    Visit dicts carry ``kind="visit"`` and ``members``; detection dicts are unchanged.
    """
    v_params: list = []
    v_where = "WHERE 1=1"
    d_params: list = []
    d_where = "WHERE 1=1"
    include_visits = source in (None, "frigate")
    if source == "frigate":
        d_where += " AND source = 'frigate' AND visit_id IS NULL"
    elif source == "birdnet":
        d_where += " AND source = 'birdnet'"
    else:
        d_where += " AND (source != 'frigate' OR visit_id IS NULL)"
    if species:
        v_where += (" AND EXISTS (SELECT 1 FROM detections m WHERE m.visit_id = v.id"
                    " AND m.common_name = ? COLLATE NOCASE)")
        v_params.append(species)
        d_where += " AND common_name = ? COLLATE NOCASE"
        d_params.append(species)
    if zone:
        v_where += (" AND (" + _zone_like("v.zones", v_params, zone)
                    + " OR EXISTS (SELECT 1 FROM detections m WHERE m.visit_id = v.id AND"
                    + _zone_like("m.zone", v_params, zone) + "))")
        d_where += " AND" + _zone_like("zone", d_params, zone)
    if before is not None:
        v_where += " AND v.start_time < ?"
        v_params.append(before)
        d_where += " AND start_time < ?"
        d_params.append(before)
    if since is not None:
        v_where += " AND v.start_time >= ?"
        v_params.append(since)
        d_where += " AND start_time >= ?"
        d_params.append(since)

    with _connect() as conn:
        dets = [dict(r) for r in conn.execute(
            f"SELECT * FROM detections {d_where} ORDER BY start_time DESC LIMIT ?",
            [*d_params, limit],
        ).fetchall()]
        visits: list[dict] = []
        if include_visits:
            visits = [dict(r) for r in conn.execute(
                f"SELECT v.* FROM visits v {v_where} ORDER BY v.start_time DESC LIMIT ?",
                [*v_params, limit],
            ).fetchall()]
    for v in visits:
        v["kind"] = "visit"
    members = visits_with_members([v["id"] for v in visits])
    for v in visits:
        v["members"] = members.get(v["id"], [])
    # A visit with no members (all deleted, or its events never imported) has nothing to
    # show; hide rather than render an empty shell. _settle_visits removes these on the
    # deletion paths, so this is belt-and-braces for import ordering.
    visits = [v for v in visits if v["members"]]
    items = sorted(visits + dets, key=lambda x: x["start_time"], reverse=True)
    return items[:limit]


def reset_visits() -> None:
    """Forget every visit (for a full re-import from Frigate's review API)."""
    with _connect() as conn:
        conn.execute("UPDATE detections SET visit_id = NULL WHERE visit_id IS NOT NULL")
        conn.execute("DELETE FROM visits")
        conn.execute("DELETE FROM visit_refs")
        conn.execute("DELETE FROM visit_announced")


def visit_stats() -> dict:
    """Counts for the startup log: visits, grouped and ungrouped Frigate events."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM visits) AS visits,
                   SUM(visit_id IS NOT NULL) AS grouped,
                   SUM(visit_id IS NULL) AS ungrouped
            FROM detections WHERE source = 'frigate'
            """
        ).fetchone()
    return {"visits": row["visits"] or 0, "grouped": row["grouped"] or 0,
            "ungrouped": row["ungrouped"] or 0}


# -------------------------------------------------------------------------- subjects

_SUBJECT_COLS = (
    "detection_id", "idx", "is_primary", "common_name", "scientific_name", "species_code",
    "score", "margin", "candidates", "id_status", "manual_name", "manual_sci",
    "embedding_model", "embedding", "crop_file", "anchored", "n_frames", "created_at",
)


def replace_subjects(detection_id: int, rows: list[dict[str, Any]]) -> None:
    """Replace a detection's subjects wholesale (one transaction).

    Rejections are keyed by idx and survive on purpose: the caller matches old subjects
    to new ones by embedding and rewrites the idx on any it carried over, and drops the
    rest — see identify._carry_forward.
    """
    now = time.time()
    with _connect() as conn:
        conn.execute("DELETE FROM detection_subjects WHERE detection_id = ?", (detection_id,))
        for r in rows:
            values = {c: r.get(c) for c in _SUBJECT_COLS}
            values["detection_id"] = detection_id
            values["created_at"] = values.get("created_at") or now
            values["is_primary"] = 1 if values.get("is_primary") else 0
            values["anchored"] = 0 if values.get("anchored") is False else 1
            conn.execute(
                f"INSERT INTO detection_subjects ({', '.join(_SUBJECT_COLS)}) "
                f"VALUES ({', '.join(':' + c for c in _SUBJECT_COLS)})",
                values,
            )


def subjects_for(detection_id: int) -> list[dict]:
    """All of a detection's subjects, primary first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM detection_subjects WHERE detection_id = ? ORDER BY idx",
            (detection_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def secondary_subjects_for(detection_id: int) -> list[dict]:
    """The OTHER birds in a detection's event, for the card's "also in view" strip.

    ``display_name`` is what the card should call it: the human's label if there is
    one, else the service's answer when it passed the thresholds, else None (show the
    candidates instead).
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, (
                SELECT GROUP_CONCAT(species, '|') FROM detection_subject_rejections r
                WHERE r.detection_id = s.detection_id AND r.idx = s.idx
            ) AS rejected
            FROM detection_subjects s
            WHERE s.detection_id = ? AND s.is_primary = 0
            ORDER BY s.idx
            """,
            (detection_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["display_name"] = d.get("manual_name") or (
            d.get("common_name") if d.get("id_status") == "ok" else None)
        d["rejected"] = [x for x in (d.get("rejected") or "").split("|") if x]
        out.append(d)
    return out


def set_subject_species_manually(detection_id: int, idx: int, common_name: str,
                                 scientific_name: Optional[str]) -> bool:
    """Name one OTHER bird in an event by hand. False if there is no such subject."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE detection_subjects
            SET manual_name = ?, manual_sci = ?, id_status = 'manual'
            WHERE detection_id = ? AND idx = ? AND is_primary = 0
            """,
            (common_name, scientific_name, detection_id, idx),
        )
        return cur.rowcount > 0


def reject_subject(detection_id: int, idx: int, species: str) -> bool:
    """"That other bird is not a <species>": remember it and clear the subject's name.

    No service call — the subject's own candidates are already stored, so the UI can
    offer the next one. Returns False if there is no such subject.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM detection_subjects WHERE detection_id = ? AND idx = ? "
            "AND is_primary = 0",
            (detection_id, idx),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            """
            INSERT OR IGNORE INTO detection_subject_rejections
                (detection_id, idx, species, rejected_at)
            VALUES (?, ?, ?, ?)
            """,
            (detection_id, idx, species, time.time()),
        )
        conn.execute(
            """
            UPDATE detection_subjects
            SET common_name = NULL, scientific_name = NULL, species_code = NULL,
                manual_name = NULL, manual_sci = NULL, id_status = 'rejected'
            WHERE detection_id = ? AND idx = ?
            """,
            (detection_id, idx),
        )
        return True


def subject_rejections(detection_id: int) -> dict[int, list[str]]:
    """{idx: [rejected species...]} for one detection's subjects."""
    out: dict[int, list[str]] = {}
    with _connect() as conn:
        for r in conn.execute(
            "SELECT idx, species FROM detection_subject_rejections WHERE detection_id = ?",
            (detection_id,),
        ):
            out.setdefault(r["idx"], []).append(r["species"])
    return out


def _delete_subjects(conn: sqlite3.Connection, det_ids: list[int]) -> None:
    if not det_ids:
        return
    ids = [(i,) for i in det_ids]
    conn.executemany("DELETE FROM detection_subjects WHERE detection_id = ?", ids)
    conn.executemany("DELETE FROM detection_subject_rejections WHERE detection_id = ?", ids)


def species_present(detection_ids: list[int]) -> dict[int, list[dict]]:
    """Every species present in each detection's event: the primary (the detections row
    itself) plus any OTHER bird that was confidently identified or labelled by hand.

    {detection_id: [{common_name, scientific_name, score, idx, source}]} with the primary
    first. One query for many detections, because the visit card asks for a whole visit.
    """
    if not detection_ids:
        return {}
    marks = ",".join("?" for _ in detection_ids)
    out: dict[int, list[dict]] = {i: [] for i in detection_ids}
    with _connect() as conn:
        for r in conn.execute(
            f"""
            SELECT id, common_name, scientific_name, id_score AS score
            FROM detections WHERE id IN ({marks}) {_named_clause()}
            """,
            detection_ids,
        ):
            out[r["id"]].append({"common_name": r["common_name"],
                                 "scientific_name": r["scientific_name"],
                                 "score": r["score"], "idx": 0, "source": "primary"})
        for r in conn.execute(
            f"""
            SELECT detection_id, idx, COALESCE(manual_name, common_name) AS name,
                   COALESCE(manual_sci, scientific_name) AS sci, score
            FROM detection_subjects
            WHERE detection_id IN ({marks}) AND is_primary = 0
              AND id_status IN ('ok', 'manual')
              AND COALESCE(manual_name, common_name) IS NOT NULL
            ORDER BY detection_id, idx
            """,
            detection_ids,
        ):
            out[r["detection_id"]].append({"common_name": r["name"],
                                           "scientific_name": r["sci"],
                                           "score": r["score"], "idx": r["idx"],
                                           "source": "subject"})
    return out


def detections_with_unidentified_subjects(limit: int = 100,
                                          before: Optional[float] = None) -> list[dict]:
    """Detections whose PRIMARY has a name but some other bird in view does not."""
    params: list = []
    where = ("WHERE EXISTS (SELECT 1 FROM detection_subjects s WHERE s.detection_id = "
             "detections.id AND s.is_primary = 0 AND s.id_status IN "
             "('low_confidence', 'rejected'))")
    if before is not None:
        where += " AND start_time < ?"
        params.append(before)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM detections {where} ORDER BY start_time DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def unidentified_subject_count() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM detection_subjects WHERE is_primary = 0 "
            "AND id_status IN ('low_confidence', 'rejected')"
        ).fetchone()
    return int(row["c"] or 0) if row else 0


def confirmed_subject_embeddings(model: str) -> list[tuple[str, str]]:
    """(species, embedding) for every OTHER-bird subject the probe may learn from.

    Confident answers and human labels only, and only for species already confirmed
    into the registry — the same bar ``confirmed_embeddings`` holds primaries to.
    Primaries are never read from this table (they live in identification_embeddings),
    so the two lists never double count.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(s.manual_name, s.common_name) AS name, s.embedding AS embedding
            FROM detection_subjects s
            JOIN species_confirmed sc
              ON sc.common_name = COALESCE(s.manual_name, s.common_name) COLLATE NOCASE
            WHERE s.is_primary = 0 AND s.id_status IN ('ok', 'manual')
              AND s.embedding_model = ? AND s.embedding IS NOT NULL
              AND COALESCE(s.manual_name, s.common_name) IS NOT NULL
            """,
            (model,),
        ).fetchall()
    return [(r["name"], r["embedding"]) for r in rows]


# A "seen" is one (visit, species) pair, not one Frigate event: a bird that Frigate
# tracked as fifteen short objects was one bird, once. Events with no visit (no retained
# review item, or history from before review items) count as their own unit via -id, so
# old numbers stay exactly what they were. BirdNET rows are never grouped.
#
# Two spellings because the species-grouped queries already partition by common_name and
# need only the visit part, while ungrouped totals must keep species apart themselves.
_SEEN_UNIT_IN_SPECIES = "CASE WHEN source = 'frigate' THEN COALESCE(visit_id, -id) END"
_SEEN_UNIT = ("CASE WHEN source = 'frigate' THEN COALESCE(visit_id, -id) || '|' || "
              "LOWER(common_name) END")
SEEN_COUNT = f"COUNT(DISTINCT {_SEEN_UNIT})"
SEEN_COUNT_BY_SPECIES = f"COUNT(DISTINCT {_SEEN_UNIT_IN_SPECIES})"
HEARD_COUNT = "COALESCE(SUM(source = 'birdnet'), 0)"


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
    zone: Optional[str] = None,
) -> list[dict]:
    """Newest-first detections. ``before`` is a start_time cursor for pagination."""
    params: list = []
    where = "WHERE 1=1"
    where += _source_clause(source, params)
    if species:
        where += " AND common_name = ?"
        params.append(species)
    if zone:
        # zone is comma-joined for multi-zone events ("bird_bath, porch"); match one
        # whole element, delimiter-wrapped, so "bath" cannot match "bird_bath".
        where += " AND ',' || REPLACE(zone, ' ', '') || ',' LIKE '%,' || ? || ',%'"
        params.append(zone.replace(" ", ""))
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


def distinct_zones() -> list[str]:
    """Individual zone names seen so far, for the Recent page's filter dropdown.

    Rows store comma-joined combinations ("bird_bath, porch"); the dropdown wants the
    individual zones, so split and dedupe here rather than in SQL.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT zone FROM detections WHERE zone IS NOT NULL AND zone != ''"
        ).fetchall()
    zones = {z.strip() for (combo,) in rows for z in combo.split(",") if z.strip()}
    return sorted(zones)


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
            f"""
            SELECT
                {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS total,
                MIN(start_time)         AS first_seen,
                MAX(start_time)         AS last_seen,
                MAX(confidence)         AS best_confidence,
                {SEEN_COUNT_BY_SPECIES} AS frigate_total,
                {HEARD_COUNT}           AS birdnet_total,
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
               {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS count,
               MIN(start_time)      AS first_seen,
               MAX(start_time)      AS last_seen,
               {SEEN_COUNT_BY_SPECIES} AS frigate_total,
               {HEARD_COUNT}           AS birdnet_total,
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


def daily_recap(day_start: float, day_end: float,
                only_confirmed: bool = False) -> list[dict]:
    """Per-species activity within [day_start, day_end) — the Recap page's day view.

    The only aggregate query with BOTH time bounds: every other stat is "since", but a
    recap for last Tuesday needs Tuesday alone. ``is_first_ever`` marks species whose
    first detection ever falls inside the window ("new!" on the page) — computed from
    the species' all-time minimum, not the window's, so revisiting an old day doesn't
    re-badge everything that was merely new-to-that-window.
    """
    params: list = [day_start, day_end]
    where = ("WHERE start_time >= ? AND start_time < ?"
             + _named_clause() + _confirmed_clause(only_confirmed))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   MAX(scientific_name) AS scientific_name,
                   {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS count,
                   {SEEN_COUNT_BY_SPECIES} AS seen,
                   {HEARD_COUNT}           AS heard,
                   MIN(start_time)      AS first_time,
                   MAX(start_time)      AS last_time,
                   -- Distinct local days with activity: the multi-day recap's
                   -- "how regular a visitor" signal.
                   COUNT(DISTINCT date(start_time, 'unixepoch', 'localtime')) AS days_active,
                   -- A representative image for the row: the latest snapshot-bearing
                   -- Frigate event of the day (MAX pairs ref with time lexically well
                   -- enough here; refs are same-day event ids).
                   MAX(CASE WHEN source = 'frigate' AND has_snapshot = 1
                            THEN snapshot_ref END) AS snapshot_ref
            FROM detections {where}
            GROUP BY common_name
            ORDER BY count DESC, last_time DESC
            """,
            params,
        ).fetchall()
        recap = [dict(r) for r in rows]
        if recap:
            marks = ",".join("?" for _ in recap)
            firsts = conn.execute(
                f"""
                SELECT common_name, MIN(start_time) AS first_ever
                FROM detections
                WHERE common_name IN ({marks}) COLLATE NOCASE
                GROUP BY common_name
                """,
                [r["common_name"] for r in recap],
            ).fetchall()
            first_ever = {r["common_name"].lower(): r["first_ever"] for r in firsts}
            for row in recap:
                ever = first_ever.get(row["common_name"].lower())
                row["is_first_ever"] = bool(
                    ever is not None and day_start <= ever < day_end)
    return recap


def recap_ticks(day_start: float, day_end: float,
                only_confirmed: bool = False) -> dict[str, list[dict]]:
    """Every seen/heard unit in [day_start, day_end) as a timeline tick, per species.

    Backs the recap's day ribbons. One tick per *visit* (or per Frigate event without a
    visit) and one per BirdNET row — the same units ``daily_recap`` counts, collapsed in
    SQL so a busy birdbath visit of twenty tracked objects is one tick, not twenty. ``n``
    is how many detections the tick stands for (a visit's clip count); ``t`` is the
    unit's first detection. Bounded by one day's rows, so no cap here — the view collapses
    dense rows to density bins.
    """
    where = ("WHERE start_time >= ? AND start_time < ?"
             + _named_clause() + _confirmed_clause(only_confirmed))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name, source, MIN(start_time) AS t, COUNT(*) AS n
            FROM detections {where}
            GROUP BY common_name, source,
                     CASE WHEN source = 'frigate' THEN COALESCE(visit_id, -id) ELSE id END
            ORDER BY common_name, t
            """,
            [day_start, day_end],
        ).fetchall()
    ticks: dict[str, list[dict]] = {}
    for r in rows:
        ticks.setdefault(r["common_name"], []).append(
            {"t": float(r["t"]), "source": r["source"], "n": int(r["n"])})
    return ticks


def hourly_by_species(start: float, end: float,
                      only_confirmed: bool = False) -> dict[str, list[dict]]:
    """Hour-of-day histogram per species over [start, end) — the multi-day recap's ribbon.

    24 zero-filled buckets per species, seen counting visits and heard counting rows,
    like everything else on the page. Local hours, like ``hourly_activity``.
    """
    where = ("WHERE start_time >= ? AND start_time < ?"
             + _named_clause() + _confirmed_clause(only_confirmed))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   CAST(strftime('%H', start_time, 'unixepoch', 'localtime') AS INTEGER) AS hour,
                   {SEEN_COUNT_BY_SPECIES} AS seen,
                   {HEARD_COUNT}           AS heard
            FROM detections {where}
            GROUP BY common_name, hour
            """,
            [start, end],
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        buckets = out.setdefault(
            r["common_name"], [{"hour": h, "seen": 0, "heard": 0} for h in range(24)])
        h = int(r["hour"]) % 24
        buckets[h]["seen"] += int(r["seen"] or 0)
        buckets[h]["heard"] += int(r["heard"] or 0)
    return out


# species_regularity() scans the whole table (a date() per row); regularity moves by
# days, so a couple of minutes of staleness is invisible and saves the scan on every
# recap step and species-index load.
_REGULARITY_TTL_S = 120.0
_regularity_cache: dict[tuple, tuple[float, dict]] = {}


def species_regularity(before: Optional[float] = None,
                       only_confirmed: bool = False) -> dict[str, dict]:
    """All-time attendance per species, keyed by lower-cased common name.

    ``first_seen``/``last_seen`` (epochs), ``days_active`` (distinct local days with a
    detection) and — when ``before`` is given — ``prev_before``: the last detection
    strictly before that instant, which is what "back after N days" on a recap window
    compares against. The regularity tiers (daily / regular / occasional / rare) are
    derived from days_active over the span since first_seen by ``recap.tier``.
    """
    key = (int(before or 0), bool(only_confirmed))
    now = time.time()
    hit = _regularity_cache.get(key)
    if hit and now - hit[0] < _REGULARITY_TTL_S:
        return hit[1]
    where = "WHERE 1=1" + _named_clause() + _confirmed_clause(only_confirmed)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   MIN(start_time) AS first_seen,
                   MAX(start_time) AS last_seen,
                   COUNT(DISTINCT date(start_time, 'unixepoch', 'localtime')) AS days_active,
                   MAX(CASE WHEN start_time < ? THEN start_time END) AS prev_before
            FROM detections {where}
            GROUP BY common_name
            """,
            [before if before is not None else 0.0],
        ).fetchall()
    out = {
        r["common_name"].lower(): {
            "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            "days_active": int(r["days_active"] or 0), "prev_before": r["prev_before"],
        }
        for r in rows
    }
    _regularity_cache[key] = (now, out)
    return out


def daily_counts_by_species(since: float,
                            only_confirmed: bool = False) -> dict[str, dict[str, int]]:
    """{species: {YYYY-MM-DD: count}} from ``since`` — the species index's sparklines.

    Same units as the tiles' counts (visits + heard rows); local days; only days with
    activity are present, so the caller zero-fills its window.
    """
    where = "WHERE start_time >= ?" + _named_clause() + _confirmed_clause(only_confirmed)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT common_name,
                   date(start_time, 'unixepoch', 'localtime') AS day,
                   {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS count
            FROM detections {where}
            GROUP BY common_name, day
            """,
            [since],
        ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["common_name"], {})[r["day"]] = int(r["count"] or 0)
    return out


def retained_detections() -> list[dict]:
    """Every kept-forever detection, for the Kept catalogue.

    Ordered species A→Z then newest first, so the caller can group by walking the list
    once. No confirmation gating on purpose: the user pinned this exact footage, which
    outranks whether the species has been approved into the registry yet.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM detections
            WHERE retained_at IS NOT NULL
            ORDER BY common_name COLLATE NOCASE ASC, start_time DESC
            """
        ).fetchall()
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
    display flourish for the Dex theme, not an identifier anything keys on.

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
    """Species counts for the Dex completion readout.

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
        visit_ids = _visit_ids_of(conn, [det_id])
        conn.execute("DELETE FROM detections WHERE id = ?", (det_id,))
        # Explicit rather than relying on ON DELETE CASCADE: SQLite enforces foreign keys
        # only when PRAGMA foreign_keys is on, and it is off by default on every new
        # connection. Orphaned embeddings would otherwise accumulate silently forever.
        conn.execute("DELETE FROM identification_embeddings WHERE detection_id = ?", (det_id,))
        conn.execute("DELETE FROM identification_rejections WHERE detection_id = ?", (det_id,))
        _delete_subjects(conn, [det_id])
        conn.execute(
            "INSERT OR REPLACE INTO deleted_refs (source, source_ref, deleted_at) VALUES (?, ?, ?)",
            (row["source"], row["source_ref"], time.time()),
        )
        _settle_visits(conn, visit_ids)
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
            visit_ids = _visit_ids_of(conn, [r["id"] for r in rows])
            conn.execute(
                "DELETE FROM detections WHERE common_name = ? COLLATE NOCASE",
                (common_name,),
            )
            _settle_visits(conn, visit_ids)
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
            _delete_subjects(conn, [r["id"] for r in rows])
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
    embedding_model: Optional[str] = None,
    probe_weight: Optional[float] = None,
    probe_examples: Optional[int] = None,
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

    ``model`` (the full model_version) is provenance for the RESULT — after a model or
    vocabulary change it tells you which rows are stale. ``embedding_model`` keys the
    stored EMBEDDING and must be the service's embedding_key, which survives vocabulary
    changes. Defaults to ``model`` reduced via ``embedding_key_from`` for callers that
    only have the old-style combined string.
    """
    if embedding_model is None and model:
        embedding_model = embedding_key_from(model)
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE detections SET
                id_status = ?, id_score = ?, id_margin = ?, id_model = ?, id_at = ?,
                -- COALESCE so a later status-only update (a retry that failed, say) does
                -- not wipe a shortlist we already have to show the user.
                id_candidates = COALESCE(?, id_candidates),
                -- NOT coalesced: NULL means "the probe abstained on this attempt", and a
                -- reroll where it abstained must not inherit the previous run's numbers.
                id_probe_weight = ?, id_probe_examples = ?
                {", confidence = ?" if set_confidence else ""}
            WHERE source = ? AND source_ref = ?
            """,
            (status, score, margin, model, time.time(), candidates,
             probe_weight, probe_examples)
            + ((score,) if set_confidence else ())
            + (source, source_ref),
        )
        if embedding and embedding_model:
            row = conn.execute(
                "SELECT id FROM detections WHERE source = ? AND source_ref = ?",
                (source, source_ref),
            ).fetchone()
            if row:
                _upsert_detection_embedding(conn, row["id"], embedding_model, embedding)


def _upsert_detection_embedding(conn, detection_id: int, model: str, embedding: str) -> None:
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
        (detection_id, model, embedding, time.time()),
    )


def put_detection_embedding(detection_id: int, model: str, embedding: str) -> None:
    """Store an embedding for a detection outside the identification flow.

    Backs the manual-label backfill: a detection whose identification failed has no
    embedding row, so a manual label on it would teach the probe nothing without this.
    """
    with _connect() as conn:
        _upsert_detection_embedding(conn, detection_id, model, embedding)


def has_embedding(detection_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM identification_embeddings WHERE detection_id = ?",
            (detection_id,),
        ).fetchone()
    return row is not None


def manual_rows_missing_embeddings(limit: int = 50) -> list[dict]:
    """Manually-labelled Frigate detections that never got an embedding stored.

    These are labels that currently teach the probe nothing: the identification failed
    (or predates embeddings), so there is no vector for the confirmed name to train.
    Newest first — recent events are the ones whose media Frigate still has.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.* FROM detections d
            LEFT JOIN identification_embeddings e ON e.detection_id = d.id
            WHERE d.id_status = 'manual' AND d.source = 'frigate'
              AND e.detection_id IS NULL
            ORDER BY d.start_time DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


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
        visit_ids = _visit_ids_of(conn, [row["id"]])
        conn.execute("DELETE FROM detections WHERE id = ?", (row["id"],))
        conn.execute(
            "DELETE FROM identification_embeddings WHERE detection_id = ?", (row["id"],)
        )
        conn.execute(
            "DELETE FROM identification_rejections WHERE detection_id = ?", (row["id"],)
        )
        _delete_subjects(conn, [row["id"]])
        _settle_visits(conn, visit_ids)


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
    # Plus the other birds found in events, each on its own embedding (see
    # detection_subjects). Primaries never come from that table, so no double counting.
    return [(r["name"], r["embedding"]) for r in rows] + confirmed_subject_embeddings(model)


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
        # Other birds in view that still need a name — a separate queue, because the
        # detection itself already has a species and must not re-enter the main one.
        "subjects": unidentified_subject_count(),
    }


def unidentified_count() -> int:
    return unidentified_counts()["actionable"]


def purge_unidentified(older_than: float) -> list[str]:
    """Drop stale unidentifiable rows. Returns the removed rows' source_refs.

    These are kept deliberately (with Frigate's own classifier off, dropping them would
    mean no record a bird was ever there) but they must not grow without bound on a busy
    feeder. No tombstone is written: this is housekeeping, not a user deletion, and
    tombstoning would stop a future backfill from re-importing a row that a better model
    could since identify.

    Refs, not a count: the caller owns per-detection files (stored crops) that must be
    removed alongside the rows.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, source_ref FROM detections
            WHERE id_status IN ('failed', 'low_confidence') AND start_time < ?
              -- A clip the user pinned as kept-forever must keep its row too: the
              -- video would survive at Frigate while the card vanished here.
              AND retained_at IS NULL
            """,
            (older_than,),
        ).fetchall()
        if not rows:
            return []
        ids = [(r["id"],) for r in rows]
        visit_ids = _visit_ids_of(conn, [r["id"] for r in rows])
        conn.executemany("DELETE FROM detections WHERE id = ?", ids)
        conn.executemany(
            "DELETE FROM identification_embeddings WHERE detection_id = ?", ids
        )
        conn.executemany(
            "DELETE FROM identification_rejections WHERE detection_id = ?", ids
        )
        _delete_subjects(conn, [r["id"] for r in rows])
        _settle_visits(conn, visit_ids)
    return [r["source_ref"] for r in rows]


def overlapping_detections(source_ref: str, start: float, end: float) -> list[dict]:
    """Frigate detections whose time window overlaps [start, end], excluding one ref.

    Backs the zoom gate: another bird on camera during this event means the PTZ may have
    been filming it instead. A row with no end_time is an event still in progress and
    counts as overlapping.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT source_ref, zone, start_time, end_time FROM detections
            WHERE source = 'frigate' AND source_ref != ?
              AND start_time < ? AND (end_time IS NULL OR end_time > ?)
            """,
            (source_ref, end, start),
        ).fetchall()
    return [dict(r) for r in rows]


def set_retained(detection_id: int, retained: bool) -> None:
    """Record whether this event's clip is pinned kept-forever at Frigate."""
    with _connect() as conn:
        conn.execute(
            "UPDATE detections SET retained_at = ? WHERE id = ?",
            (time.time() if retained else None, detection_id),
        )


def set_kept_export(detection_id: int, export_id: Optional[str],
                    file: Optional[str] = None) -> None:
    """Record (or clear, with both None) the Frigate export of the zoomed window."""
    with _connect() as conn:
        conn.execute(
            "UPDATE detections SET kept_export_id = ?, kept_export_file = ? WHERE id = ?",
            (export_id, file, detection_id),
        )


def set_kept_export_file(detection_id: int, file: str) -> None:
    """Fill in the export's served filename once the async export has finished."""
    with _connect() as conn:
        conn.execute(
            "UPDATE detections SET kept_export_file = ? WHERE id = ?",
            (file, detection_id),
        )


def retained_missing_export() -> list[dict]:
    """Kept Frigate rows with no zoomed export yet — the one-time backfill's worklist.

    The pair check (does this camera have a partner?) is the caller's: camera pairing
    is configuration, not data.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM detections
            WHERE retained_at IS NOT NULL AND source = 'frigate'
              AND kept_export_id IS NULL
              AND start_time IS NOT NULL AND end_time IS NOT NULL
            """
        ).fetchall()
    return [dict(r) for r in rows]


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


def species_last_times(common_name: str, source: str, source_ref: str,
                       visit_id: Optional[int] = None) -> dict:
    """The species' most recent detection time — overall and per source — excluding
    one row (already upserted) and, when given, every other member of its visit.

    Feeds the notification blueprint's per-species cooldown: 'how long has this
    species been quiet before this detection?', split by source so a camera cooldown
    isn't fed by audio detections (and vice versa). Siblings in the same visit are the
    same stay, not a previous one — without excluding them, the second tracked object of
    one visit would report the species as "last seen 4 seconds ago".
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
              AND (? IS NULL OR visit_id IS NULL OR visit_id != ?)
            """,
            (common_name, source, source_ref, visit_id, visit_id),
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
                {SEEN_COUNT} + {HEARD_COUNT} AS total,
                -- Species count excludes unidentified rows, but `total` deliberately
                -- does NOT: a bird nobody could name was still a bird that showed up,
                -- and dropping it would understate the detection count for the day.
                COUNT(DISTINCT CASE WHEN common_name != 'bird' COLLATE NOCASE
                                    THEN common_name END) AS species,
                {SEEN_COUNT}                 AS frigate_total,
                {HEARD_COUNT}                AS birdnet_total
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
                   {SEEN_COUNT_BY_SPECIES} + {HEARD_COUNT} AS count,
                   MAX(start_time) AS last_seen,
                   {HEARD_COUNT}           AS heard,
                   {SEEN_COUNT_BY_SPECIES} AS seen,
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
                   {SEEN_COUNT} + {HEARD_COUNT} AS count
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
                   {SEEN_COUNT} + {HEARD_COUNT} AS count
            FROM detections {where}
            GROUP BY hour
            ORDER BY hour
            """,
            params,
        ).fetchall()
    counts = {int(r["hour"]): r["count"] for r in rows}
    return [{"hour": h, "count": counts.get(h, 0)} for h in range(24)]
