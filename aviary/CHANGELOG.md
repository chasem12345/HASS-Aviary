# Changelog

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
