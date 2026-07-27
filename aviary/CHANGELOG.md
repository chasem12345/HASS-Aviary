# Changelog

## 0.5.0

- **Species blacklist**: "Blacklist — remove and never record again" on the
  **Remove species…** menu deletes every detection of a species and then refuses it at
  ingest permanently — live MQTT *and* startup backfill. Blacklisted species produce no
  rows at all, so they never reach stats, charts, or notifications. Removing a species
  used to be retroactive only; a classifier that was reliably wrong brought it back on
  the next detection.
  - Matches the **scientific name** as well as the common name. Frigate's classifier can
    emit scientific names, and once a species' rows are deleted there's nothing left to
    map such a label onto a common name from — so the scientific name is captured before
    the purge.
  - Review and undo under **Settings → Blacklisted species**. *Allow again* re-opens
    ingest but does not restore the deleted detections.
  - Distinct from the notification blueprint's `blacklist`, which only silences alerts
    while still recording the species.
- **Reference calls**: species pages lazily load a community-confirmed (research-grade)
  recording of the species from iNaturalist, so an audio classification can be compared
  against a known example. Recordist and licence are always shown, with a link to the
  original observation. No API key or configuration needed; metadata is cached for 30
  days and the audio is streamed on demand rather than stored.
- **Pokédex mode** (**Settings → Theme**): reskins Aviary as a field registry, reusing the
  heard/seen split it already tracks — a bird BirdNET-Go has only **heard** stays a
  darkened silhouette until a camera **sees** it and completes the entry. Adds registry
  numbers (by order of first detection) and a `REGISTRY seen/total` completion readout.
  Stored server-side, so it applies on every device and pages render already themed.
- **New Settings page** for preferences that apply immediately, unlike add-on options
  which need a restart.
- Charts, confidence bars and menu accents now read their colours from the active theme
  instead of hardcoded values, so they follow both the light/dark and Pokédex themes.
- iNaturalist taxon lookups are constrained to birds, so a bird's common name can no
  longer match an unrelated non-bird taxon.

## 0.4.5

- **Shareable video downloads**: video cards get a "⬇ video" link that remuxes the
  Frigate clip through ffmpeg (`-c copy`, lossless) into a standard MP4 with a real
  duration header. Clips saved from the media player are streaming MP4s that report
  00:00 length and fail upload validation (e.g. Discord treats them as empty);
  remuxed downloads pass. Files are named `<species>-<timestamp>.mp4`.

## 0.4.4

- **Remove misclassifications**: every detection card gets a × button and species
  pages a "Remove species…" button. Options per removal: Aviary only, also **clear
  the species label in Frigate** (keeps the video), or also **delete the
  event/detection at the source** (Frigate event or BirdNET-Go detection).
- Removed detections are **tombstoned**, so the startup backfill can't re-import
  them from the source's history. If a species' last detection is removed, a
  genuine future detection announces as a new species again.

## 0.4.3

- **One bird, one species**: species names are now unified across sources at ingest.
  A label matching another species' scientific name (Frigate's classifier emits
  scientific names, e.g. "Cardinalis cardinalis") is mapped to the species' common
  name, and case differences adopt the stored spelling. New-species matching is
  case-insensitive. Fixes duplicate notifications where the same cardinal fired
  once as "Cardinalis cardinalis" (seen) and once as a "new" "Northern Cardinal"
  (heard). A startup migration remaps existing scientific-named rows, so split
  species pages merge after the update.
- **Heard-bird image fallback**: when BirdNET-Go's species photo can't be fetched,
  the notification falls back to the species' most recent camera snapshot instead
  of going imageless.

## 0.4.2

- **Cooldown no longer mixes camera and audio**: the per-species cooldown now counts
  only detections of the same kind — a bird singing near BirdNET-Go can't suppress
  its camera notifications (this was why a Frigate notification could arrive with no
  Aviary one). Event payload adds `seconds_since_species_last_seen` and
  `seconds_since_species_last_heard` alongside the existing any-source field.

## 0.4.1

- **Current images, not cached ones**: seen-bird notifications now use the Frigate
  integration's live image proxy (like the Frigate blueprint) with a new image-style
  choice — animated **GIF** (default), snapshot, or thumbnail. The add-on's staged
  `/local` images (heard birds) get a cache-buster so phones stop showing a
  days-old cached copy.
- **Frigate notifications fire at event end**: the species/score are final and the
  clip exists, so the tap action no longer lands on "event not found".
- **Cleaner message, no title**: "Northern Cardinal detected on bird camera" /
  "Wood Thrush heard at yard", with a "New species! " prefix for first-ever species.

## 0.4.0

- **Notifications for every bird, not just new species**: the add-on now fires an
  `aviary_detection` event for every classified detection (deduplicated across
  Frigate's repeated MQTT messages), carrying `is_new_species`,
  `seconds_since_species_last_detected`, the `source_ref` (Frigate event id), and
  the Aviary panel path. New species still ALSO fire the legacy
  `aviary_new_species` event, so old automations keep working.
- **Blueprint v2 — "Aviary: bird notifications"** (same file, refreshed on update;
  remember to *Reload Automations*): toggles for new-species / all-seen / all-heard
  notifications (defaults: on / on / off), a species **blacklist**, and a
  **per-species cooldown** (default 10 min) — back-to-back cardinals stay silent,
  a sparrow in between still notifies; new species bypass it.
- **Tap actions**: tapping a seen-bird notification opens the **Frigate clip**
  (via the Frigate integration's notification proxy, configurable); a heard-bird
  notification opens the **Aviary panel**.
- Requires `hassio_api` (self slug lookup for the panel link) — expect a
  permission prompt on update. **Behavior change**: existing automations made from
  the v1 blueprint inherit the new defaults after a blueprint reload and will start
  notifying on every seen bird — turn "Notify on every seen bird" off to keep the
  old behavior.

## 0.3.5

- **Blueprint fix**: treat a cleared device picker (`null`) the same as no device, so
  group-only automations can't hit a template error. Note: after an add-on update
  refreshes the blueprint file, Home Assistant only re-reads it on **Reload
  Automations** (Developer Tools → YAML) or a Core restart — "Missing input
  notify_device" means the old cached definition is still active.

## 0.3.4

- **Blueprint: device or notify group**: the device input is now optional, so an
  automation can target a single companion-app device, a notify group/action
  (e.g. `notify.all_phones`), or both. Existing automations keep working; the
  updated blueprint is refreshed automatically on add-on start.

## 0.3.3

- **Fix**: the Home Assistant config folder is mounted at `/homeassistant`, not
  `/homeassistant_config` — blueprint auto-install and notification images now work.
  The mount point is pinned explicitly via `path:` in the add-on config.

## 0.3.2

- **New-species notifications**: when a species is detected for the first time ever,
  Aviary fires an `aviary_new_species` event on the Home Assistant event bus with the
  species name, seen/heard verb, and an image (Frigate snapshot, else BirdNET-Go's
  generic species photo) staged at `/local/aviary/<species>.jpg`.
- **Bundled blueprint, installed automatically**: an automation blueprint
  ("Aviary: new species notification") is copied into
  `config/blueprints/automation/aviary/` at startup — create an automation from it,
  pick your phone, done. See DOCS for details.
- **Test button**: "Test notification" on the dashboard fires a test event through the
  full pipeline (image included) so you can verify the automation without waiting for
  a new bird.
- New option `notify_new_species` (default `true`); the add-on now requests
  `homeassistant_api` and a writable `homeassistant_config` mapping for the above.
  Note: images under `config/www` are served unauthenticated at `/local/…`.

## 0.3.1

- **Versioned static path**: assets are now served from `/static-<build>/…` instead of
  `/static/…?v=<build>`. A reverse proxy that caches by path and ignores the query string
  (which was serving a stale `app.js` and hiding the species About card) can't have the
  new path cached, so add-on updates always take effect.

## 0.3.0

- **Species page video/audio filter**: when a species has both Frigate (video) and
  BirdNET-Go (audio) detections, a "Show" dropdown filters the list by source and
  defaults to **Video**. Pagination keeps the selected filter. Single-source species
  show no dropdown.

## 0.2.9

- **Static assets sent with `Cache-Control: no-cache`** (ETag kept for cheap 304
  revalidation), so a reverse proxy (e.g. nginx) in front of Home Assistant stops
  serving a stale `app.js`/`app.css` and picks up add-on updates immediately.

## 0.2.8

- **Cache-bust static assets**: `app.css`/`app.js`/`chart.umd.js` now carry a `?v=`
  token tied to their build mtime, so browsers load the current version after an add-on
  update instead of a stale cached copy (which was hiding the new About card).

## 0.2.7

- **Species "About" blurb**: species pages now show a short description from Wikipedia
  plus taxonomy (order/family) and conservation status from iNaturalist, with a
  "Read more" link. Fetched lazily and cached in SQLite (refreshed monthly); both
  sources are free and need no API key. Falls back silently if nothing is found.

## 0.2.6

- Added add-on `icon.png` and `logo.png` (bird glyph on brand green).

## 0.2.5

- **Top species**: split the single Count column into separate **Heard** (BirdNET-Go)
  and **Seen** (Frigate) counts.

## 0.2.4

- **"Heard" vs "seen" wording**: audio detections now say *heard* instead of *seen*
  throughout. Species stats pick the verb from whichever source produced the first/last
  detection, the dashboard leaderboard shows "seen/heard X ago" per species, species
  tiles carry colored `seen`/`heard` chips, and detection cards are badged
  "Seen · Frigate" / "Heard · BirdNET-Go".

## 0.2.3

- **Generic species photos**: wherever there's no Frigate snapshot (audio-only species,
  detections without media), Aviary now shows a photo of the species pulled from
  BirdNET-Go's image cache (Wikipedia/AviCommons). Applies to the species pages,
  dashboard leaderboard, and detection cards. Falls back to the placeholder icon on
  older BirdNET-Go builds without the species-image endpoint.
- **Panel visible to all users**: the sidebar entry is no longer admin-only
  (`panel_admin: false`).
- **MQTT diagnostics**: an unreachable broker is now logged (with a hint about
  `localhost` pointing at the add-on container itself) instead of failing silently.
