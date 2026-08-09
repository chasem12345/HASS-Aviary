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
- The **⬇ video** link on video cards downloads the clip **remuxed into a standard
  MP4** (ffmpeg `-c copy`, lossless) with a correct duration header — clips saved
  straight from the media player are streaming MP4s that report 00:00 length and can
  fail upload validation elsewhere (e.g. Discord).
- Where there's no Frigate snapshot to show (audio-only species, detections without media),
  Aviary shows a **generic photo of the species** pulled from BirdNET-Go's image cache
  (`GET /api/v2/media/species-image`, sourced from Wikipedia/AviCommons). This needs a
  current BirdNET-Go build — on older builds (≤ v0.6.4) the endpoint doesn't exist and the
  placeholder icon is shown instead.
- The **Recent** page filters by source, date range, and species, groups results by day,
  paginates with *Load older*, and refreshes itself (~30s) as new detections arrive.
- **Species pages** show totals, first/last seen, best confidence, and per-day /
  hour-of-day activity charts for that species.
- Species pages also offer **reference photos and recordings** — known pictures of the
  bird plus its song and call — so you can see and hear what it actually looks and sounds
  like, and judge a classification for yourself. See
  [Reference photos and recordings](#reference-photos-and-recordings).
- **New species wait for your approval** before joining the registry, so a single
  misclassification can't inflate your species count. See
  [Confirming new species](#confirming-new-species).

## Confirming new species

By default (`require_species_confirmation`, on) a newly detected species does **not** join
the registry automatically. It waits for you to approve it — so one bad classification
can't permanently inflate your species count or take a dex number.

An unconfirmed species:

- is **kept out of** the registry/species list, dex numbering, the species and new-species
  counts, and the top-species leaderboard;
- **still records detections normally** — they appear on Recent and on the species' own
  page, which is exactly the evidence you need to judge it;
- **still fires its new-species notification**, because that's what tells you there's
  something to review.

To review: open **Awaiting review** (a dashboard tile and a button on the Species page,
both shown only when something is pending). The species page gives you its detections,
clips and spectrograms, plus [reference photos](#reference-photos) and recordings of
what the bird should look and sound like. Then either:

- **Confirm species** — it joins the registry and takes the next dex number, or
- **Reject…** — the same menu as *Remove species…*, including the blacklist option that
  stops it being recorded ever again. See [Removing misclassifications](#removing-misclassifications).

Notes:

- **Upgrading doesn't create a backlog.** Every species already in your database is marked
  confirmed once, on the first start after updating.
- Approving is reversible (the species returns to the queue); rejecting deletes detections
  and is not.
- Rejecting a species also clears its approval, so if it genuinely turns up later it
  queues for review again rather than silently rejoining the registry.
- Set `require_species_confirmation: false` to turn the whole thing off — every query goes
  back to counting all species immediately, and nothing is left stranded in a queue.

## Removing misclassifications

Classifiers get it wrong sometimes (a sparrow labeled as a heron). Hover a detection
card and click **×**, or use **Remove species…** on a species page, then pick:

- **Remove from Aviary** — deletes it here only.
- **Remove + clear species label in Frigate** — also blanks the event's `sub_label`
  at the source, so the video stays in Frigate as a plain "bird" event.
- **Remove + delete at the source** — also deletes the Frigate event (clip and all)
  or the BirdNET-Go detection.
- **Blacklist — remove and never record again** — see below.

Removed detections are tombstoned: the startup backfill will not re-import them.
Source-side actions need `frigate_url` / `birdnet_url` to be reachable; BirdNET-Go
deletion requires its API to allow it (and the detection's BirdNET-Go id to be
known, which is the case for detections ingested from current builds).

### Blacklisting a species

Removing is retroactive: if a classifier is *reliably* wrong about a species, it comes
straight back on the next detection. Blacklisting stops that permanently.

**Blacklist — remove and never record again** (on the **Remove species…** menu) deletes
every detection of the species and then refuses it at ingest from that point on — for
**both** live MQTT and the startup backfill. A blacklisted species produces no rows, so
it never appears in stats or charts, and no `aviary_detection` event fires for it, so it
can't notify either.

Blacklisting captures the species' **scientific name** as well as its common name, and
matches on both. This matters because Frigate's classifier can emit scientific names —
and once the species' rows are deleted, Aviary has nothing left to map that label onto a
common name from.

Review and undo the list under **Settings → Blacklisted species**. *Allow again* re-opens
ingest for the species; it does **not** restore the detections that were deleted when you
blacklisted it — those are gone.

> The notification blueprint has its own `blacklist` option (see below). That one only
> silences notifications while still recording the species. Use it when you want the data
> but not the alerts; use the blacklist here when you want neither.

## Reference photos and recordings

Each species page lazily loads reference material — the quickest way to sanity-check a
classification against a known example of the species, and what makes the
[review queue](#confirming-new-species) usable.

### Reference photos

Up to three licensed photos from [iNaturalist](https://www.inaturalist.org), shown in their
own card **below** the hero image rather than replacing it: the hero is whatever your
camera caught, these are what the bird is supposed to look like, and having both on screen
is the point. Only reusably-licensed photos are used — iNaturalist marks all-rights-reserved
photos with no licence at all, and those are skipped — and the photographer and licence are
always shown. Needs no configuration and works on a Frigate-only install.

### Reference recordings

### Get better recordings: set `xeno_canto_api_key`

By default recordings come from [iNaturalist](https://www.inaturalist.org) observation
sounds. Those are incidental field recordings of a *sighting*: the only quality signal
available is whether the identification was confirmed, so clips often carry background
birds, barking dogs and other noise.

[xeno-canto](https://xeno-canto.org) is a dedicated bird-sound archive whose recordings
carry a quality rating, a sound type and a list of other species audible in the clip.
Aviary uses that metadata to pick clean, single-species audio. Setting the key gives you:

- **Separate SONG and CALL buttons**, since they're different sounds worth hearing.
- Only **quality A** recordings (falling back to B), 3–30 seconds long, strongly
  preferring clips with **no other species audible**.

To enable it, register a free account at [xeno-canto.org](https://xeno-canto.org), then
copy the API key from [your account page](https://xeno-canto.org/account) into the
`xeno_canto_api_key` add-on option and restart. The key is stored in your add-on config
only — it is never sent to the browser, and audio is fetched server-side.

### Behaviour

- Aviary tries **xeno-canto quality A**, then **quality B**, then falls back to
  **iNaturalist** research-grade observation sounds. Without a key — or for a species with
  no scientific name on record — it goes straight to iNaturalist and behaves exactly as it
  did before, with a single **CRY** button.
- Only recordings under a **reusable Creative Commons licence** are used, and the licence
  is re-checked on every result rather than trusted to the provider's filter. The recordist
  and licence are always shown, with a link through to the original recording — a condition
  of those licences, so a recording with nothing to credit is never played.
- Results are cached in Aviary's database for 30 days, so each species hits the APIs at
  most once a month. The audio itself is not cached — it's streamed from the provider on
  demand, the same way Frigate and BirdNET-Go media are.
- If no provider has a suitable recording, the card is simply not shown.

## Diet and habitat

Species pages show what the bird eats, where it forages and its primary habitat — as data
rows in Pokédex mode (`EATS · SEEDS & GRAIN`) and as chips on the About card otherwise.

- Data comes from **AVONET** (Tobias et al. 2022), a published dataset covering every
  extant bird species, bundled with the add-on as an ~88 KB gzipped subset of 10,661
  species. There is **no API call** — it's a local lookup, so it works offline, on a fresh
  install, and without any key or rate limit.
- It's keyed on **eBird scientific names**, which is the taxonomy BirdNET-Go emits, so
  lookups match without synonym juggling. All 17 species in the author's own BirdNET-Go
  history resolve.
- Frigate-only species sometimes have no scientific name recorded; Aviary uses the one
  iNaturalist resolves for the About card, and simply omits the fields if there's still no
  match.
- `Eats` shows a plain-English description with AVONET's own term (e.g. *Granivore*) as the
  tooltip. Values are AVONET's trophic niche, so they describe a species' predominant diet
  rather than an exhaustive list.

Regenerate the bundled table with `python scripts/build_diet_table.py` (needs `openpyxl`;
the add-on itself does not). AVONET is CC BY 4.0 and is credited in the UI and in
`app/data/AVONET-CITATION.txt`.

## Pokédex mode

**Settings → Theme → Pokédex mode** turns Aviary into a field registry. It reuses the
distinction Aviary already tracks — BirdNET-Go **heard** it, Frigate **saw** it — as the
two states of a dex entry:

- **HEARD** — detected by audio only. The entry stays a darkened silhouette, like an
  encountered-but-uncaught species.
- **SEEN** — a camera has caught it, so the entry is complete and shows its photo.

Two pages are rebuilt rather than recoloured:

- **Registry** (the Species page) becomes a numbered list in order of first detection,
  with a `SEEN n/total` completion readout. Each row shows its entry number, a photo, a
  detection gauge, and its HEARD/SEEN state. Row photos are **full colour once a camera
  has seen the species, and darkened while it has only been heard**. Source/range/"new
  only" filters still apply — entry numbers belong to the whole registry, so a filtered
  list is legitimately non-contiguous.
- **Entry** (a species page) becomes a dex readout: the photo in one screen, and
  order/family/conservation status, [diet](#diet-and-habitat), first/last detection, best
  confidence and seen/heard gauges in another, with **SONG** and **CALL** buttons that
  play the species' reference recordings — a single **CRY** button when only the
  iNaturalist fallback is available (see [Reference recordings](#reference-recordings)).
  The entry photo
  is **always full colour**, even for a species you've only heard — the entry is where you
  go to see what the bird actually looks like.

Dashboard, Recent and Settings keep their normal layout in the dex palette.

**Navigating like a dex.** On the registry, `↑`/`↓` move the cursor and `↵` opens the
entry (`Home`/`End` jump to either end); on an entry, `←`/`→` step to the neighbouring
entries. Everything is built from ordinary links, so it all works by clicking — and with
JavaScript disabled — too.

The theme is stored in Aviary's database rather than the browser, so it applies to every
device that opens the panel and pages arrive already themed (no flash of the wrong
colours). Switched off, Aviary follows your system's light/dark preference as before.

Aviary has no regional species checklist, so the registry only contains birds you've
actually detected — there are no blank "not yet encountered" entries to fill in.

The pixel typeface is [Silkscreen](https://github.com/googlefonts/silkscreen), bundled
with the add-on under the SIL Open Font License (`app/static/fonts/OFL.txt`) and served
locally, so the theme needs no internet access.

## Bird notifications

Aviary fires an `aviary_detection` event on the Home Assistant event bus for **every
classified detection** (once per detection — Frigate's repeated event messages are
deduplicated):

```json
{
  "common_name": "Blue Jay",
  "scientific_name": "Cyanocitta cristata",
  "source": "frigate",
  "source_ref": "1719854321.123-abc123",  // Frigate event id / BirdNET detection ref
  "verb": "seen",                          // "seen" (camera) or "heard" (audio)
  "confidence": 0.87,
  "location": "backyard",
  "image": "/local/aviary/blue-jay.jpg",   // or null when no image was available
  "detected_at": "2026-07-02T09:15:00-05:00",
  "is_new_species": false,                 // first time Aviary has ever recorded it
  "seconds_since_species_last_detected": 5400.0,  // any source; null = first ever
  "seconds_since_species_last_seen": 5400.0,      // Frigate only; null = never seen
  "seconds_since_species_last_heard": 120.0,      // BirdNET-Go only; null = never heard
  "panel_path": "/<addon_slug>/species/Blue%20Jay"  // this species' Aviary page, for tap actions
}
```

First-ever species also fire the legacy `aviary_new_species` event (same payload) for
older automations. The image is the Frigate snapshot for visual detections (Aviary
waits a few seconds for Frigate to write it), otherwise BirdNET-Go's generic photo of
the species — saved under `config/www/aviary/` so phones can fetch it.

**To get phone notifications**, use the bundled **"Aviary: bird notifications"**
blueprint — the add-on installs/refreshes it automatically at startup (after an
update, run *Developer Tools → YAML → Reload Automations* so HA re-reads it):

1. **Settings → Automations & Scenes → Blueprints** → "Aviary: bird notifications" →
   **Create automation**; pick a companion-app device and/or a notify group action.
2. Configure the filters:
   - **Always notify on new species** (default on) — seen *or* heard.
   - **Notify on every seen bird** (default on) / **every heard bird** (default off).
   - **Blacklist** — species that never notify, even as new species.
   - **Cameras to notify on** (default: all) — only these Frigate cameras notify. See
     [Filtering by camera](#filtering-by-camera).
   - **Per-species cooldown** (default 10 min) — a species re-notifies only after it
     has been quiet that long, counting only detections of the same kind (camera
     cooldown ignores audio detections and vice versa); other species are
     unaffected. New species bypass it.
   - **Frigate notification proxy base** (advanced) — powers the seen-bird tap
     action; needs the Frigate integration. Clear to disable.
3. **Tap behavior**: seen-bird notifications open the Frigate clip; heard-bird
   notifications open that species' Aviary page. The species deep link needs **Home
   Assistant 2026.2 or newer** — that release moved add-on panels from
   `/hassio/ingress/<slug>` to `/<addon_slug>` and added the iframe routing Aviary
   uses to land on the right page.
4. Press **"Test notification"** on the Aviary dashboard — it fires a test event
   through the full pipeline (image included) and reports errors inline.

Notes:

- Detections already in Aviary's database (including backfill imports) never fire
  events — only live detections do, once each.
- Files under `config/www` are served **without authentication** at `/local/…`; only
  bird images are stored there.
- Set `notify_new_species: false` in the add-on options to turn all detection events
  off.

## Filtering by camera

A common setup is two cameras on one feeder: a wide one for zone detection, and a zoomed
one that produces the classifications actually worth trusting. There are two independent
controls, and which you want depends on whether you still care about the wide camera's
detections at all.

| | Where | Effect |
|---|---|---|
| **Cameras to notify on** | blueprint input | Only the listed cameras notify. Detections from every camera are still recorded and still show on the dashboard. Empty = all cameras. |
| `ignore_cameras` | add-on option | The listed cameras are **never recorded** — no detections, no species stats, no dex entries. Empty = record everything. |

Use the blueprint input to stop the pings; use `ignore_cameras` as well if the camera's
classifications are wrong often enough to be polluting your species registry.

Both take the camera's **Frigate name** (the key from your Frigate config, e.g.
`bird_pole_zoom`), matched case-insensitively. The easiest way to get it exactly right is
to look at a detection from that camera on Aviary's Recent page — the location shown is
the string being matched.

Neither affects **BirdNET-Go audio detections**. Audio events carry a node name in the
same field, so a camera filter that applied to them would silence every audio
notification.

> `ignore_cameras` only applies to detections ingested *after* you set it. Anything that
> camera already recorded stays in the database — there's no purge-by-camera. To clean up
> a species it wrongly added, use **Remove species…** on that species' page (which removes
> it across all cameras).

## Configuration

| Option | Description |
|---|---|
| `frigate_url` | Base URL of Frigate, e.g. `http://ccab4aaf-frigate:5000`. Used to proxy clips/snapshots. |
| `birdnet_url` | Base URL of BirdNET-Go, e.g. `http://a0d7b954-birdnet-go:8080`. Used to proxy audio. |
| `frigate_topic` | MQTT topic Frigate publishes to (default `frigate/events`). |
| `birdnet_topic` | MQTT topic BirdNET-Go publishes to (default `birdnet`). |
| `backfill_on_start` | Import existing detections from Frigate/BirdNET-Go HTTP APIs on startup (default `true`). Idempotent. |
| `ignore_unclassified` | Skip detections with no species — i.e. Frigate `bird` objects with no `sub_label` (default `true`). Set `false` to also record generic "bird" sightings. |
| `require_species_confirmation` | New species wait in a review queue instead of joining the registry automatically (default `true`). See [Confirming new species](#confirming-new-species). |
| `ignore_cameras` | Frigate camera names whose detections are **never recorded** (default: none). Use it for a wide zone-detection camera whose species guesses would pollute the registry. Applies to live ingest and backfill; BirdNET-Go audio is unaffected. Only affects detections from when it's set — see *Filtering by camera*. |
| `notify_new_species` | Fire `aviary_detection` HA events for live detections (default `true`). See *Bird notifications*. |
| `mqtt_host` / `mqtt_port` / `mqtt_user` / `mqtt_password` | Optional broker overrides. Leave `mqtt_host` empty to use the HA Mosquitto broker automatically. |
| `xeno_canto_api_key` | Optional free key from [xeno-canto.org/account](https://xeno-canto.org/account). Unlocks curated song/call reference recordings; blank keeps the iNaturalist fallback. See [Reference recordings](#reference-recordings). |
| `log_level` | Logging verbosity. |

Changing any of these needs an add-on restart. The **Settings** page inside Aviary holds
the options that don't: the theme and the species blacklist both apply immediately.

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
