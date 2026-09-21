"""Table definitions, the additive migrations in init_db, and the species_sightings view."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from . import _conn
from ._conn import _connect

__all__ = [
    "_SCHEMA",
    "_SCHEMA_SPECIES_AUDIO",
    "_SIGHTINGS_VIEW",
    "_SUBJECTS_NAMED_INDEX",
    "init_db",
    "embedding_key_from",
]


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
    id_status       TEXT,                            -- pending|confirming|ok|low_confidence|failed|manual|frigate|visit
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

-- Regional seasonality cache: iNaturalist observations per month of year within a radius
-- of the Home Assistant location, for the species page's Seasonality card. Pure cache
-- (TTL-refilled; nothing user-authored). lat/lon record which location the months were
-- fetched for, so a moved house refetches.
CREATE TABLE IF NOT EXISTS species_season (
    common_name TEXT PRIMARY KEY COLLATE NOCASE,
    taxon_id    INTEGER,
    lat         REAL,
    lon         REAL,
    radius_km   REAL,
    months      TEXT,               -- JSON: 12 integers, January first
    total       INTEGER,
    fetched_at  REAL NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 0
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

-- Per-example learning flags (0.32.0): "do not learn from this one" (excluded_at) and
-- the last audit's verdict (suspicious_*: it sat nearer another species' examples than
-- its own). A side table rather than columns on the rows holding the embeddings:
-- detection_subjects is replaced wholesale on re-identify (flags are remapped alongside
-- rejections), and the primary has no subject row of its own to hang a flag on —
-- subject_idx 0 means the detection itself.
CREATE TABLE IF NOT EXISTS learning_flags (
    detection_id       INTEGER NOT NULL,
    subject_idx        INTEGER NOT NULL DEFAULT 0,
    excluded_at        REAL,
    suspicious_at      REAL,
    suspicious_margin  REAL,
    suspicious_species TEXT,
    PRIMARY KEY (detection_id, subject_idx)
);

-- Species keepsakes: the FIRST and the LATEST camera sighting of each species, kept at
-- Frigate automatically (retain_indefinitely on the event plus an export of the padded
-- clip). One row per (species, role); "latest" moves to a newer sighting at most once
-- per local day and the previous one is released, so a species never holds more than
-- two. Independent of the user's own 📌 pin (detections.retained_at): Frigate's flag is
-- held while EITHER wants it, and releasing one never clears the other's hold.
-- detection_id is the parent event — for a species that was only "also in view" in
-- another bird's event, that event is what gets kept (subject_idx says which bird).
CREATE TABLE IF NOT EXISTS species_keepsakes (
    common_name      TEXT    NOT NULL COLLATE NOCASE,
    role             TEXT    NOT NULL,             -- 'first' | 'latest'
    detection_id     INTEGER NOT NULL,
    subject_idx      INTEGER NOT NULL DEFAULT 0,
    start_time       REAL    NOT NULL,             -- the sighting's time (daily cadence, display)
    kept_at          REAL    NOT NULL,             -- when Frigate accepted the retain
    export_id        TEXT,                         -- event camera's padded window
    export_file      TEXT,                         -- served basename, resolved lazily
    zoom_export_id   TEXT,                         -- paired camera's window, when paired
    zoom_export_file TEXT,
    export_status    TEXT,                         -- queued | not applicable | failed: ...
    export_tried_at  REAL,                         -- set once tried; startup retries failures
    PRIMARY KEY (common_name, role)
);
CREATE INDEX IF NOT EXISTS idx_keepsakes_detection ON species_keepsakes (detection_id);

-- Life list on iNaturalist: the ONE observation posted per species, built from its first
-- sighting. No row = not posted. A 'failed' row keeps the last error for the species page
-- and is simply overwritten by the next attempt. "Forget link" deletes the row only — the
-- observation on iNaturalist is the user's to delete there. NOCASE like every species key.
CREATE TABLE IF NOT EXISTS species_inat (
    common_name     TEXT PRIMARY KEY COLLATE NOCASE,
    observation_id  INTEGER,
    observation_url TEXT,
    taxon_id        INTEGER,
    detection_id    INTEGER,                 -- the sighting the observation was built from
    subject_idx     INTEGER NOT NULL DEFAULT 0,
    observed_at     REAL,                    -- that sighting's start_time
    media           TEXT,                    -- 'photo' | 'sound' | 'photo,sound' | ''
    status          TEXT NOT NULL,           -- 'posted' | 'failed'
    error           TEXT,
    posted_at       REAL
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


# One row per SIGHTING of a species in an event: the detections row itself (the tracked
# bird, or the BirdNET row) plus one synthetic row per OTHER bird the identifier named in
# a Frigate event (detection_subjects: is_primary = 0, ok or manual, with a name). Subject
# rows inherit the event's time, visit, camera and snapshot, so a cardinal event with an
# oriole in view is one oriole sighting at that moment, in that visit. ``id`` stays the
# PARENT detection id in both arms — that is what makes the seen unit (visit_id or -id,
# plus species) count a visit-less two-species event as one seen for EACH species, and
# dedupe a species that is both the tracked bird and a subject of the same event.
#
# Species-shaped aggregates read FROM this view, aliased ``detections`` so the shared
# clause helpers (_named_clause, _confirmed_clause) apply unchanged; row fetches, feeds
# and every mutation stay on the table. Dropped and recreated on every start: a view
# cannot be altered, and the ALTER TABLEs in init_db must have run first (zone and
# visit_id are added columns).
_SIGHTINGS_VIEW = """
DROP VIEW IF EXISTS species_sightings;
CREATE VIEW species_sightings AS
    SELECT d.id, d.id AS detection_id, 0 AS subject_idx,
           d.source, d.source_ref, d.common_name, d.scientific_name, d.species_code,
           d.confidence, d.location, d.zone, d.start_time, d.end_time,
           d.has_clip, d.has_snapshot, d.clip_ref, d.snapshot_ref, d.visit_id,
           d.id_status, d.id_score, d.retained_at
    FROM detections d
    UNION ALL
    SELECT d.id, d.id AS detection_id, s.idx AS subject_idx,
           d.source, d.source_ref,
           COALESCE(s.manual_name, s.common_name)    AS common_name,
           COALESCE(s.manual_sci, s.scientific_name) AS scientific_name,
           s.species_code,
           d.confidence, d.location, d.zone, d.start_time, d.end_time,
           d.has_clip, d.has_snapshot, d.clip_ref, d.snapshot_ref, d.visit_id,
           s.id_status, s.score AS id_score, d.retained_at
    FROM detection_subjects s
    JOIN detections d ON d.id = s.detection_id
    WHERE s.is_primary = 0 AND s.id_status IN ('ok', 'manual')
      AND COALESCE(s.manual_name, s.common_name) IS NOT NULL;
"""


# Named secondaries are a small fraction of subject rows (most events hold one bird); a
# partial index lets the view's second arm and feed_page's species test touch only them.
_SUBJECTS_NAMED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_subjects_named
    ON detection_subjects (detection_id, idx)
    WHERE is_primary = 0 AND id_status IN ('ok', 'manual')
"""


def init_db(db_path: str) -> None:
    """Configure the module and create the schema."""
    _conn.set_path(db_path)
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
            # When the media audit found Frigate no longer has ANY media for this event
            # (has_clip/has_snapshot are zeroed at the same time). NULL = believed
            # present, or never checked. The row itself stays: history is not footage.
            ("media_expired_at", "REAL"),
            # The identifier stored this event's own crop under /data/crops — an exact
            # "has a picture of its own" predicate for SQL (the feed filter), where the
            # templates' has_crop() file check can't reach.
            ("has_crop", "INTEGER NOT NULL DEFAULT 0"),
            # Frigate's own classification of the tracked object (0.33.0): the sub_label
            # it published and the classifier's score for it. Provenance, kept apart from
            # common_name so "Frigate said X, aviary-id decided Y" stays sayable after
            # a confirmation overrides the label. NULL = Frigate named nothing.
            ("frigate_label", "TEXT"), ("frigate_score", "REAL"),
            # When this row's species was announced (or silently claimed for its visit
            # by a backfill) — 0.36.0. NULL = never. The link-time visit seed keys on it:
            # a member that already HAD a name when the review item linked it is not
            # thereby announced (with Frigate's classifier on, in-progress rows carry
            # Frigate's provisional label long before their verdict), and seeding it
            # swallowed the real announcement. Legacy rows announced before the column
            # existed stay NULL; the start-up seed still covers them by status.
            ("announced_at", "REAL"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE detections ADD COLUMN {name} {decl}")
        # Same reasoning as idx_detections_id_status below: the column is added by the
        # loop above, so the index cannot live in _SCHEMA.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_detections_visit ON detections (visit_id)"
        )
        # The sightings view needs every column the loop above may have just added.
        conn.executescript(_SIGHTINGS_VIEW)
        conn.execute(_SUBJECTS_NAMED_INDEX)
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
        if "sections" not in info_cols:
            # Wikipedia section extracts (0.29.0). Expire cached rows so the next view of
            # each species page fills them in, rather than waiting out a month-long TTL.
            conn.execute("ALTER TABLE species_info ADD COLUMN sections TEXT")
            conn.execute("UPDATE species_info SET fetched_at = 0 WHERE sections IS NULL")
        # Which species an embedding's frame was chosen FOR (0.32.0): the service's winner
        # on a normal identify, the person's label on a re-embed. NULL = a legacy row,
        # chosen for whatever won at the time. This is what lets a relabel know the stored
        # frame was picked for the wrong bird and must be replaced, not renamed.
        emb_cols = {r[1] for r in conn.execute("PRAGMA table_info(identification_embeddings)")}
        if "target" not in emb_cols:
            conn.execute("ALTER TABLE identification_embeddings ADD COLUMN target TEXT")
        sub_cols = {r[1] for r in conn.execute("PRAGMA table_info(detection_subjects)")}
        if "embedding_target" not in sub_cols:
            conn.execute("ALTER TABLE detection_subjects ADD COLUMN embedding_target TEXT")
        if "purity" not in sub_cols:
            # Share of the subject's crops that agreed with its answer (aviary-id 0.11.0+).
            # 0.5 on a primary means two birds were fused into one answer. NULL = unknown.
            conn.execute("ALTER TABLE detection_subjects ADD COLUMN purity REAL")
        if "co_occurring" not in sub_cols:
            # Frames this other bird shared with the tracked bird (aviary-id 0.13.0+):
            # > 0 means the two were in view together — a second bird for certain.
            # NULL = unknown (older service).
            conn.execute("ALTER TABLE detection_subjects ADD COLUMN co_occurring INTEGER")
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
