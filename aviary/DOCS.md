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
- The **⤢ scrub / still** button on video cards opens the **full player** — see
  [Scrubbing clips and saving stills](#scrubbing-clips-and-saving-stills).
- The **timestamp** on any card links to that **detection's own page** — its clip or
  audio plus the full timestamps, camera/zone and event references. This is the page a
  notification tap opens.
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

## Scrubbing clips and saving stills

The inline video on a card plays straight from Frigate. That's fast, but those clips are
**fragmented MP4s with a zero-duration header**: the browser never learns how long the clip
is, so the progress bar grows as it plays and there is nothing to drag against. On a phone,
with a scrubber only as wide as the card, it's worse still.

**⤢ scrub / still** opens a full-width player that fixes both:

- Aviary remuxes the clip to a proper seekable MP4 (ffmpeg `-c copy` — lossless, no
  re-encode) and the player downloads it **whole** before playing. That costs a moment on
  open, and buys **instant scrubbing**: every seek afterwards happens in your browser
  against video it already has, with no further requests.
- Large transport buttons — ⏪ 1s, single-frame stepping both ways, ⏩ 1s — sized for a
  thumb. Arrow keys seek, `,` / `.` step a frame, Escape closes.
- **⬇ save still** writes the frame on screen to a PNG at the clip's **native encoded
  resolution**, not the size it's drawn at. PNG is lossless for that frame, so it's the
  best still the clip can give. It's named like the video download:
  `blue-jay-20260811-142233-4.20s.png`.

Nothing is cached server-side, so each open re-fetches and re-remuxes — the player stays
black for a moment before the clip appears. If ffmpeg is unavailable it falls back to
direct playback and the title reads *seeking unavailable*: watchable, but not scrubbable.

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

- **Remove everywhere** — deletes it from Aviary *and* from the source: the Frigate event
  (clip and all) or the BirdNET-Go entry. This is the first option, because a
  misclassification you're deleting is usually one you want gone from the source too.
- **Remove from Aviary only** — deletes it here and leaves the source untouched.
- **Remove + clear species label in Frigate** — also blanks the event's `sub_label`
  at the source, so the video stays in Frigate as a plain "bird" event.
- **Blacklist — remove everywhere, never record again** — see below.

Removed detections are tombstoned: the startup backfill will not re-import them.
Source-side actions need `frigate_url` / `birdnet_url` to be reachable, and BirdNET-Go
deletion needs the detection's BirdNET-Go id (recorded for anything ingested from current
builds). Aviary handles BirdNET-Go's CSRF protection automatically — it fetches a token
before deleting and retries once if BirdNET-Go has since rotated it.

> If a source deletion fails, Aviary still removes its own copy and reports the error, so
> the two can end up out of step. The message tells you what the source said.

**A caveat on "stop BirdNET-Go learning from it":** deleting detections cleans up the
history, but BirdNET-Go keeps its learned per-species **dynamic thresholds** in separate
storage, and its maintainer describes user deletion as *soft rejection* that only tags.
So deleting is unlikely to reset a threshold it has already learned. To stop a species
influencing anything, also add it to BirdNET-Go's own **excluded species** list in its
settings — Aviary has no API to reach that list.

### Blacklisting a species

Removing is retroactive: if a classifier is *reliably* wrong about a species, it comes
straight back on the next detection. Blacklisting stops that permanently.

**Blacklist — remove everywhere, never record again** (on the **Remove species…** menu)
deletes every detection of the species **from Aviary and from the source**, then refuses
it at ingest from that point on — for
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
  "location": "backyard",                  // the camera (Frigate) or node (BirdNET-Go)
  "zone": "bird_bath",                     // Frigate zone(s) entered, comma-joined; null if none
  "image": "/local/aviary/blue-jay.jpg",   // or null when no image was available
  "detected_at": "2026-07-02T09:15:00-05:00",
  "is_new_species": false,                 // first time Aviary has ever recorded it, any source
  "is_first_seen": true,                   // first time on camera (may be an old friend on audio)
  "is_first_heard": false,                 // first time on audio
  "seconds_since_species_last_detected": 5400.0,  // any source; null = first ever
  "seconds_since_species_last_seen": 5400.0,      // Frigate only; null = never seen
  "seconds_since_species_last_heard": 120.0,      // BirdNET-Go only; null = never heard
  "panel_path": "/<addon_slug>/detection/1234"    // this detection's Aviary page, for tap actions
}
```

`is_new_species` means **never detected at all**, from any source. `is_first_seen` and
`is_first_heard` mean **never recorded by that kind of source before** — so a bird you have
been hearing for months finally turning up on camera sets `is_first_seen` while
`is_new_species` stays false. A genuinely new species sets both. A detection never claims
the flag for the other source: a *heard* detection of a bird no camera has caught has
`is_first_seen: false`, because it isn't a sighting.

The blueprint's notification prefers the zone over the camera when one is present —
"Blue Jay detected at bird bath" rather than "…on backyard camera" — with underscores
rendered as spaces.

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
   - **Notify on first sighting** (default on) — the first time a species is caught on
     camera, even if you have been hearing it for months. **Notify on first recording**
     (default off) is the audio equivalent. Both fire once per species and bypass the
     cooldown; a brand-new species notifies once, not twice.
   - **Notify on every seen bird** (default on) / **every heard bird** (default off).
   - **Blacklist** — species that never notify, even as new species.
   - **Cameras to notify on** (default: all) — only these Frigate cameras notify. See
     [Filtering by camera](#filtering-by-camera).
   - **Returning species** (default 2 days, 0 disables) — a species absent for at
     least this many days announces "Back after N days!". Milestone-like: it fires
     even when the every-seen/every-heard toggles are off and bypasses the cooldown —
     a long-absent bird showing up is worth knowing about on any configuration.
     **Returning species counts…** picks what "absent" means: *Camera sightings only*
     (default) announces a return only when a bird is seen again, measured against its
     last camera sighting — audio detections neither count as a return nor reset the
     clock; *Seen or heard* counts any detection on both sides.
   - **Per-species cooldown** (default 10 min) — a species re-notifies only after it
     has been quiet that long, counting only detections of the same kind (camera
     cooldown ignores audio detections and vice versa); other species are
     unaffected. New species bypass it.
   - **Frigate notification proxy base** (advanced) — powers the live seen-bird
     image; needs the Frigate integration. Clear to use the Aviary staged image.
3. **Tap behavior**: tapping any notification — seen or heard — opens that
   **detection's own page** in Aviary, showing its clip or audio alongside the
   timestamps and metadata. The deep link needs **Home Assistant 2026.2 or newer** —
   that release moved add-on panels from `/hassio/ingress/<slug>` to `/<addon_slug>`
   and added the iframe routing Aviary uses to land on the right page.
4. Press **"Test notification"** on the Aviary dashboard — it fires a test event
   through the full pipeline (image included) and reports errors inline.

Notes:

- Detections already in Aviary's database (including backfill imports) never fire
  events — only live detections do, once each.
- Files under `config/www` are served **without authentication** at `/local/…`; only
  bird images are stored there.
- Set `notify_new_species: false` in the add-on options to turn all detection events
  off.

## Daily recap

The **Recap** page (top navigation) is one local day at a glance: every identified
species active that day with seen/heard counts, first and last detection times, a
thumbnail from the day's own footage, and a **new!** badge on species whose first-ever
detection happened that day. Navigate with the previous/next links or the date picker;
"today" uses the same local-midnight boundary as the dashboard stats. Species without a
confirmed identification (review-queue rows) are not counted.

## Viewing the same moment on the other camera

When `identify_zoom_map` pairs two cameras, every Frigate detection card gains a
**⇄ view on <camera>** button that plays the *paired* camera's continuous recordings
for the event's exact time window — both directions, wide→zoomed and zoomed→wide. It
uses Frigate's recordings API, so the paired camera must be recording continuously
(which the cross-camera zoom setup already requires). Windows are the event padded by
`clip_pad_seconds` (default 10 s) on each side — birds arrive before tracking starts
and linger after it drops. The same padding applies to the **primary** ⤢ player (which
plays the event camera's own recordings for the padded window, falling back to the
bare event clip when those recordings have expired) and to the kept zoomed export.
Windows are capped at 15 minutes.

## The Kept catalogue

Everything pinned with **📌 keep** is collected on the **Kept** page (top navigation),
grouped by species with the newest clips first. Cards there show the full date and time
of the detection rather than "2h ago" — a curated shelf is about *when it was*.
Releasing a pin (clicking **📌 kept**) removes the clip from the catalogue on the next
visit.

## Keeping a clip forever

Every Frigate detection card with a clip has a **📌 keep** button. It flips Frigate's
`retain_indefinitely` flag on the event, exempting that clip from Frigate's normal
retention expiry, and marks the card **📌 kept** (click again to release). Aviary's own
housekeeping also leaves kept rows alone, so a pinned-but-unidentified detection never
loses its card while Frigate still holds the video.

**On a paired two-camera setup** (`identify_zoom_map`), the retain flag only protects
the event's own — wide — clip; the zoomed view is the partner camera's *continuous
recordings*, which no retain flag can reach. So pinning a paired-camera event **also
creates a Frigate export** of the partner camera's recordings for the event window
(named "Aviary · <species> · <time>", visible in Frigate's own Export UI, exempt from
retention). The card gains a **▶ zoomed (kept)** button that plays it — exports process
asynchronously at Frigate, so it may take a few seconds to become playable after
pinning. Unpinning deletes the export. Requires Frigate 0.18+ (older builds don't
return the export id). On upgrade, a one-time task retro-exports the zoomed window for
already-kept events whose recordings still exist.

Requires Aviary to reach Frigate's API (`frigate_url`). If Frigate authentication is
enabled, the retain endpoint requires an admin credential — the same constraint as the
delete-at-source actions.

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

## Better bird identification

Frigate's built-in bird classification is a quantized MobileNet running on CPU over ~964
species. It is fast and free, and it is often wrong. Aviary can hand identification to
**aviary-id** instead — a companion container that runs BioCLIP 2 (a vision-language model
trained on 214M biological images) on a GPU, scoring each bird against **only the species
that occur in your region**.

That regional narrowing is the largest part of the accuracy gain. A zero-shot model given
the world's ~11,000 birds has far more ways to be confidently wrong than one given your
county's few hundred.

### How it changes the flow

**Turn Frigate's bird classification off** (`classification: bird: enabled: false`). Frigate
keeps doing what it is good at — spotting that *something bird-shaped* is there — and Aviary
takes over naming it.

Every Frigate event then arrives with no `sub_label`. Instead of being discarded by
`ignore_unclassified`, it is recorded as *pending*, sent to aviary-id, and only announced
once a species comes back. Notifications still fire exactly once per detection; they just
fire when the identification lands rather than when the event ends, so they carry a species
worth reading.

aviary-id pulls the clip and snapshot **from Frigate directly**, samples frames across the
clip, crops to the bird, and classifies the best three. Aviary only ever sends it an event
id, so no video passes through Home Assistant.

### Setting it up

1. Run aviary-id on a machine with an NVIDIA GPU — see `aviary-id/README.md` in this
   repository. That machine needs to reach Frigate; it needs no access to Home Assistant.
2. Set `identify_url` to its address and `identify_enabled` to `true`. Set `identify_token`
   to match `AVIARY_ID_TOKEN` on the service.
3. Turn off bird classification in Frigate.
4. Check the **Settings** page — it shows whether the service is up, whether it found the
   GPU, and how many species are in its vocabulary.

### When it isn't sure

A result is accepted only if it clears both `identify_min_score` and `identify_min_margin`.
The margin is the one that matters: 60% confidence in a Downy Woodpecker means very little
when Hairy Woodpecker scored 58%.

Anything that fails either test keeps the name "bird" and lands in a review queue, reachable
from the **N unidentified →** link on the Recent page. Detections are kept rather than
dropped — with Frigate's classifier off, discarding them would leave no record a bird was
ever there — and purged after `identify_retain_days`.

Every Frigate detection gains a **↻ re-identify** button. Adjust a threshold or the species
list, re-run a bird you can name yourself, and compare. That is the intended way to tune the
thresholds; the defaults are starting points, not recommendations.

### Cross-camera zoom

For a two-camera rig — a wide camera running bird detection over zones, and a record-only
PTZ camera your automation points at whichever zone is occupied — set
`identify_zoom_map: ["<wide_camera>:<ptz_camera>"]`. Detection then runs on ONE camera:
the wide camera owns the event (and its zones, which flow into the Zone filter and the
`aviary_detection` payload), while the identification service classifies the PTZ camera's
recordings for the event's time window. Requirements: aviary-id **0.8.0+**, the PTZ camera
recording **continuously** in Frigate, and the wide camera removed from `ignore_cameras`
if it was there. If the recordings are missing (gap, expired retention) the service falls
back to the event's own clip immediately — and if the zoomed footage exists but never
shows a detectable bird (the PTZ was travelling or blocked), it swaps to the event clip
as a last resort before giving up, so a PTZ miss still gets the full wide-camera video,
not just the event stills.

If your automation uses a zone priority list ("stay on the higher-priority zone until it's
empty"), mirror that list in `identify_zoom_zone_priority`: when two birds overlap in
different zones, the outranked event is classified from its own camera's media instead of
the PTZ footage — which would be showing the other bird.

Identified cards also show the **crop that backed the answer** as their preview image
(stored under `/data/crops`, removed with the detection) — with cross-camera zoom that is
the zoomed bird, not the wide-frame speck.

### When it can't decide

A detection that fails the thresholds shows the shortlist the model actually considered —
each species with the probability it gave — as clickable chips. Click one to accept it.
**✎ something else…** lets you type a species instead, checked against the identifier's
regional list so a typo doesn't quietly create a new species in your registry.

Naming a bird by hand marks it `manual` and puts it straight into the registry without
waiting for confirmation: you *are* the confirmation. Its confidence is left blank, because
that column records how sure the classifier was and a human answer has no place on that
scale.

This is also the honest answer to "did it even try?" — a shortlist of plausible birds at
11%, 7% and 6% says something very different from an empty one.

### Telling it when it's wrong

Next to it is **✗ wrong**. It rules that species out *for that detection* and returns the
next best answer — press it repeatedly to walk down the model's ranking until it lands on
the right bird or runs out of confident candidates.

Because the model is zero-shot, this is not a nudge or a re-weighting: the rejected species
is removed from the candidate set before the scores are computed, so its probability is
redistributed across the remaining birds rather than left as a hole. The runner-up gets to
be genuinely confident instead of looking weak by comparison.

Rejections are remembered per detection, so pressing ✗ twice can't bounce back to the first
guess. If they narrow things down to nonsense, `POST /api/detections/{id}/identify?reset=1`
clears them.

This is also the fastest way to get a feel for the model. Pick a bird you can name, press ✗,
and watch what it reaches for next — a sensible second guess (the other chickadee) tells you
something very different from a wild one.

Rejecting is per-detection and says nothing about whether the species belongs in your area.
The permanent, global version of that judgement is the **blacklist** — and with
`identify_exclude_blacklisted` on (the default), blacklisted species are ruled out of the
candidate set for every identification. That is a real accuracy gain when you blacklisted a
species because it does not occur here: a bird that would have been misread as one now gets
its correct name instead of being discarded. **If you blacklisted a species that genuinely
visits and you simply don't want it recorded, turn this off** — otherwise every one of its
visits gets recorded as some other species.

### Learning from your own birds

Identification starts out *zero-shot*: the image is compared against the species **name**.
That is what lets the candidate list be any set of species you like, and it is also where
most of the model's accuracy sits unused — on the published benchmark the same embeddings
score 74.9% that way and 92.4% once a classifier is trained on them.

So Aviary trains one, from your birds. Every species you confirm — and every detection you
name by hand — becomes an example it matches against directly. Nothing to configure: it
switches itself on species by species as examples accumulate, and the Settings page shows
what it has learned.

Four properties are deliberate:

- **A species with no examples is unaffected.** New birds are still found exactly as before.
  A classifier that quietly stopped discovering species would be worse than none.
- **It abstains when the match is not close.** Being the *nearest* example is not the same
  as being a good match, so a bird it has never seen does not get assigned to whichever
  species it happens to sit closest to.
- **It only learns from confirmed labels.** Learning from its own unreviewed guesses is how
  a classifier reinforces its own mistakes.
- **It matches your actual examples, not an average of them.** Many feeder species look
  wildly different by sex and age — a male Northern Cardinal is crimson, the female warm
  brown. A shaded female matches your stored female frames directly instead of being
  compared to a male/female blur that resembles neither.

New installs are not left cold: the iNaturalist reference photos already cached for each
species are embedded in the background, so every species in your registry starts with a
usable example. Those are posed photos rather than feeder frames, so they count for less
and are displaced by your own detections over time.

`GET /api/probe/evaluate` gives leave-one-out accuracy on **your** birds, which is the only
number that really matters — the benchmark figures above are someone else's dataset.

### Sound helping sight

If you also run BirdNET-Go, Aviary passes any species it *heard* within ten minutes of the
detection to the identifier as a prior, treating them as three times more likely. A Northern
Cardinal that sang on its way to the feeder is genuinely more likely to be the bird in the
picture. Turn it off with `identify_use_audio_priors` if you would rather the two sources
stay independent.

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
| `identify_url` | Base URL of the [aviary-id](#better-bird-identification) companion service, e.g. `http://10.0.0.50:8100`. Blank disables identification. |
| `identify_token` | Shared secret sent as a bearer token; must match `AVIARY_ID_TOKEN` on the service. Blank means no auth. |
| `identify_enabled` | Send unidentified Frigate detections to that service (default `false`). **Turn Frigate's own bird classification off when you enable this.** |
| `identify_min_score` | Minimum species probability to accept a result (default `0.35`). Below it, the detection goes to the review queue. |
| `identify_min_margin` | Minimum gap between the top two species (default `0.08`). A high score with a tiny margin means two confusable birds, not a confident answer. |
| `identify_workers` | Concurrent identification requests (default `2`). The service serializes GPU work anyway. |
| `identify_timeout` | Seconds to wait for an identification (default `60`). |
| `identify_retain_days` | Days to keep unidentified detections before purging them (default `14`; `0` keeps forever). |
| `identify_use_audio_priors` | Bias identification toward species BirdNET-Go heard around the same time (default `true`). |
| `identify_exclude_blacklisted` | Rule blacklisted species out of the identifier's candidate list (default `true`). Turn off if you blacklisted a species that genuinely visits. |
| `identify_zoom_map` | `"detect_camera:ptz_camera"` pairs (default empty). Events from the detect camera are classified from the PTZ camera's recordings for the event's time window instead of the event clip — see *Cross-camera zoom* below. Needs aviary-id 0.8.0+. |
| `identify_zoom_start_offset` | Seconds trimmed from the zoom window's start for PTZ travel time (default `2.0`). |
| `identify_zoom_zone_priority` | Your PTZ automation's zone priority list, highest first (default empty = no gating). With two birds in different zones at once, the lower-priority event skips the zoomed footage rather than classifying the bird the PTZ was actually filming. |
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
