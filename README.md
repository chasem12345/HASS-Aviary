# Aviary — Home Assistant Add-on

**Aviary** aggregates already-classified bird detections from two independent sources —
[Frigate NVR](https://frigate.video) (visual) and [BirdNET-Go](https://github.com/tphakala/birdnet-go)
(audio) — by subscribing to their MQTT topics. It keeps a running SQLite database of
species / visits for analytics and presents a dashboard in the Home Assistant sidebar
(desktop + mobile app, via ingress). Recent detections play the live clip/snapshot (Frigate)
or audio with spectrogram (BirdNET-Go) proxied from the originating source, with per-species
pages (totals, first/last seen, activity charts), day-grouped browsing with filters and
pagination, and a live-refreshing Recent feed.

Species pages also play a **reference call** for the species (a community-confirmed
recording from [iNaturalist](https://www.inaturalist.org)) so you can judge a
classification by ear, and show **what it eats**, where it forages and its habitat from a
bundled subset of the [AVONET](https://doi.org/10.1111/ele.13898) dataset (no API needed). Species a classifier is reliably wrong about can be
**blacklisted** — purged and refused at ingest from then on. An optional **Pokédex mode**
rebuilds the species pages as a field registry — a numbered list of entries and a dex-style
data readout, where a bird BirdNET-Go has only *heard* stays a silhouette until a camera
*sees* it — navigable with the arrow keys.

> Aviary does **no** classification of its own. It reads the species that Frigate emits in the
> event `sub_label` and the species BirdNET-Go emits in its MQTT payload.

## Install

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Open the **⋮** menu (top right) → **Repositories**.
3. Add this repository URL: `https://github.com/chasem12345/HASS-Aviary`
4. Find **Aviary** in the store, click **Install**.
5. On the **Configuration** tab, set `frigate_url` and `birdnet_url` (and MQTT overrides if you
   are not using the Mosquitto broker add-on), then **Start** the add-on.
6. Aviary appears in the HA sidebar with a bird icon.

## Configuration

| Option | Default | Description |
|---|---|---|
| `frigate_url` | `http://ccab4aaf-frigate:5000` | Base URL of your Frigate instance (for clip/snapshot proxying) |
| `birdnet_url` | `http://a0d7b954-birdnet-go:8080` | Base URL of your BirdNET-Go instance (for audio proxying) |
| `frigate_topic` | `frigate/events` | MQTT topic Frigate publishes events to |
| `birdnet_topic` | `birdnet` | MQTT topic BirdNET-Go publishes detections to |
| `backfill_on_start` | `true` | Import existing detections from Frigate/BirdNET-Go HTTP APIs on startup (idempotent) |
| `ignore_unclassified` | `true` | Skip species-less detections (Frigate `bird` with no `sub_label`); set `false` to record generic "bird" too |
| `mqtt_host` | `""` | Override broker host (leave empty to use the HA `mqtt` service) |
| `mqtt_port` | `1883` | Override broker port |
| `mqtt_user` | `""` | Override broker username |
| `mqtt_password` | `""` | Override broker password |
| `log_level` | `info` | `debug` / `info` / `warning` / `error` |

## Development

See [aviary/DOCS.md](aviary/DOCS.md) and the local-dev instructions there for running the
FastAPI app outside Home Assistant against a local MQTT broker.
