# Aviary

Aviary subscribes to the MQTT topics published by **Frigate** and **BirdNET-Go**, stores every
bird detection in a local SQLite database, and shows analytics + recent-detection previews in a
dashboard embedded in the Home Assistant sidebar.

## What it does

- **Frigate** (`frigate/events`): tracks `bird` objects and records the species from the event
  `sub_label`, the confidence (`top_score`), the camera, and the event id (used to fetch the
  clip/snapshot on demand).
- **BirdNET-Go** (`birdnet`): records each audio detection — common/scientific name, species
  code, confidence, and timestamp.
- Detections from both sources are stored side-by-side in one table, tagged by `source`. There
  is **no** cross-source correlation and **no** classification done by Aviary itself.
- On startup (when `backfill_on_start` is enabled) Aviary also **backfills existing detections**
  from each source's HTTP API — everything Frigate (`GET /api/events`) and BirdNET-Go
  (`GET /api/v2/detections`) still retain — so the database is prepopulated instead of starting
  empty. Backfill runs in the background, is idempotent (safe to re-run every start), and
  dedupes against live MQTT detections. It needs `frigate_url` / `birdnet_url` to be reachable.
- Clips (Frigate video), snapshots (Frigate image) and audio (BirdNET-Go) are **proxied
  live** from the source when you open a preview — nothing is cached, so previews expire when
  the source deletes the underlying media. BirdNET-Go audio is resolved via its v2
  by-id API when the detection id is known (with the legacy `/clips/` path as a
  fallback), and audio cards show the detection's spectrogram when available.
- The **Recent** page filters by source, date range, and species, groups results by day,
  paginates with *Load older*, and refreshes itself (~30s) as new detections arrive.
- **Species pages** show totals, first/last seen, best confidence, and per-day /
  hour-of-day activity charts for that species.

## Configuration

| Option | Description |
|---|---|
| `frigate_url` | Base URL of Frigate, e.g. `http://ccab4aaf-frigate:5000`. Used to proxy clips/snapshots. |
| `birdnet_url` | Base URL of BirdNET-Go, e.g. `http://a0d7b954-birdnet-go:8080`. Used to proxy audio. |
| `frigate_topic` | MQTT topic Frigate publishes to (default `frigate/events`). |
| `birdnet_topic` | MQTT topic BirdNET-Go publishes to (default `birdnet`). |
| `backfill_on_start` | Import existing detections from Frigate/BirdNET-Go HTTP APIs on startup (default `true`). Idempotent. |
| `ignore_unclassified` | Skip detections with no species — i.e. Frigate `bird` objects with no `sub_label` (default `true`). Set `false` to also record generic "bird" sightings. |
| `mqtt_host` / `mqtt_port` / `mqtt_user` / `mqtt_password` | Optional broker overrides. Leave `mqtt_host` empty to use the HA Mosquitto broker automatically. |
| `log_level` | Logging verbosity. |

## Local development (outside Home Assistant)

The app is a plain FastAPI application, so you can run it directly against a local MQTT broker:

```bash
cd aviary
pip install -r requirements.txt

export DATA_DIR=./_data
export MQTT_HOST=localhost MQTT_PORT=1883
export FRIGATE_URL=http://localhost:5000
export BIRDNET_URL=http://localhost:8080
export FRIGATE_TOPIC=frigate/events BIRDNET_TOPIC=birdnet
export LOG_LEVEL=debug

python -m uvicorn app.main:app --host 0.0.0.0 --port 8099
```

Then publish sample detections to confirm ingest + UI:

```bash
python scripts/publish_samples.py           # from the repo root
```

Open http://localhost:8099/ — the ingress-path middleware is a no-op when no `X-Ingress-Path`
header is present, so the UI works directly too.
