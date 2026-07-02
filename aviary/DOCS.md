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
- Where there's no Frigate snapshot to show (audio-only species, detections without media),
  Aviary shows a **generic photo of the species** pulled from BirdNET-Go's image cache
  (`GET /api/v2/media/species-image`, sourced from Wikipedia/AviCommons). This needs a
  current BirdNET-Go build — on older builds (≤ v0.6.4) the endpoint doesn't exist and the
  placeholder icon is shown instead.
- The **Recent** page filters by source, date range, and species, groups results by day,
  paginates with *Load older*, and refreshes itself (~30s) as new detections arrive.
- **Species pages** show totals, first/last seen, best confidence, and per-day /
  hour-of-day activity charts for that species.

## New species notifications

When a species shows up that Aviary has **never recorded before**, it fires an
`aviary_new_species` event on the Home Assistant event bus:

```json
{
  "common_name": "Blue Jay",
  "scientific_name": "Cyanocitta cristata",
  "source": "frigate",
  "verb": "seen",                        // "seen" (camera) or "heard" (audio)
  "confidence": 0.87,
  "location": "backyard",
  "image": "/local/aviary/blue-jay.jpg", // or null when no image was available
  "detected_at": "2026-07-02T09:15:00-05:00"
}
```

The image is the Frigate snapshot when the first detection was visual (Aviary waits a
few seconds for Frigate to write it), otherwise BirdNET-Go's generic photo of the
species — saved under `config/www/aviary/` so phones can fetch it.

**To get a phone notification**, use the bundled blueprint — the add-on installs it
automatically at startup:

1. Go to **Settings → Automations & Scenes → Blueprints** and find
   **"Aviary: new species notification"** (if it isn't listed, reload blueprints from
   the ⋮ menu or restart Home Assistant once).
2. **Create automation** from it and pick the device to notify (optionally an extra
   notify action like `notify.all_phones`).
3. Press **"Test notification"** on the Aviary dashboard — it fires a test event
   through the full pipeline (image included) and reports errors inline, so you can
   troubleshoot without waiting for a real new bird.

Notes:

- Species already in Aviary's database (including anything imported by backfill) never
  trigger the event — only genuinely first-time species do.
- Files under `config/www` are served **without authentication** at `/local/…`; only
  bird images are stored there.
- Set `notify_new_species: false` in the add-on options to turn the event off.

## Configuration

| Option | Description |
|---|---|
| `frigate_url` | Base URL of Frigate, e.g. `http://ccab4aaf-frigate:5000`. Used to proxy clips/snapshots. |
| `birdnet_url` | Base URL of BirdNET-Go, e.g. `http://a0d7b954-birdnet-go:8080`. Used to proxy audio. |
| `frigate_topic` | MQTT topic Frigate publishes to (default `frigate/events`). |
| `birdnet_topic` | MQTT topic BirdNET-Go publishes to (default `birdnet`). |
| `backfill_on_start` | Import existing detections from Frigate/BirdNET-Go HTTP APIs on startup (default `true`). Idempotent. |
| `ignore_unclassified` | Skip detections with no species — i.e. Frigate `bird` objects with no `sub_label` (default `true`). Set `false` to also record generic "bird" sightings. |
| `notify_new_species` | Fire an `aviary_new_species` HA event the first time a species is ever detected (default `true`). See *New species notifications*. |
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
