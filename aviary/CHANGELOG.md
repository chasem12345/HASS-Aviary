# Changelog

## 0.20.0

- **Daily Recap page.** New top-nav page showing one local day at a glance: every
  identified species with seen/heard counts, first–last detection times, a thumbnail
  from the day's own footage, and a **new!** badge on first-ever species. Previous/next
  day links plus a date picker; "today" matches the dashboard's local-midnight boundary.
- **⇄ View on the other camera.** When `identify_zoom_map` pairs two cameras, Frigate
  detection cards can play the *paired* camera's continuous recordings for the event's
  exact time window — both directions — in the existing scrub-capable player. Uses
  Frigate's recordings API through two new proxied media routes (windows capped at 10
  minutes; camera names validated).
- **"Back after N days!" notifications.** New blueprint input **Returning species**
  (default 2 days, 0 disables): a species absent that long announces its return like a
  milestone — even when routine notifications are off, bypassing the cooldown. Derived
  from the payload's existing time-since fields, so it works for events from any add-on
  since 0.11. Run *Developer Tools → YAML → Reload Automations* after updating so HA
  re-reads the blueprint.

## 0.19.0

- **Cross-camera zoom** (with aviary-id **0.8.0**). New `identify_zoom_map` option
  (`"detect_camera:ptz_camera"`): events from a wide detect camera are classified from a
  record-only PTZ camera's recordings for the event's time window, pulled via Frigate's
  recordings API — detection runs on one camera, classification sees the zoomed bird. The
  event's own thumbnail/snapshot stay in the mix as the fallback when the PTZ missed, and
  the service falls back to the event clip if the recordings don't exist — or, as a last
  resort, when the zoomed footage never showed a detectable bird at all.
  `identify_zoom_start_offset` (default 2 s) trims PTZ travel off the window. Frigate-side:
  strip detect/objects from the PTZ camera and record it continuously; remove the wide
  camera from `ignore_cameras`. Zones flow in automatically (capture, per-zone filtering
  and the notification payload shipped in 0.17.0).
- **Concurrent-bird gate.** `identify_zoom_zone_priority` mirrors the PTZ automation's
  zone priority list: when another bird's event overlaps in a higher-priority zone, the
  outranked event skips the zoomed footage (the PTZ was filming the other bird) and
  classifies from its own camera's media.
- **Cards show the crop that backed the answer.** The service now returns its best crop
  as a small JPEG; Aviary stores it (`/data/crops`, deleted with the detection, purged
  with retention) and cards use it as the preview — the actual bird, zoomed, instead of a
  wide-frame speck. Applies to review-queue cards too, where seeing what the model saw
  matters most.

## 0.18.0

- **Bulk select on the Unidentified page.** A **☑ Select** toggle adds a checkbox to
  every card (plus **Select all**), with two actions: **↻ Re-identify selected** feeds
  the rows back through the normal identification queue — made for draining the backlog
  after the GPU host was down — and **× Delete selected** removes them from Aviary
  (tombstoned so backfill can't re-import them; Frigate events and clips are never
  touched — deleting at the source stays a per-card action on purpose). Re-identify is
  fire-and-forget: rows go `pending` and resolve as the GPU works through them; events
  already past Frigate's retention simply come back `no_media`.
- Pairs with (but does not require) **aviary-id 0.7.0**, which adds a GPU supervised
  classifier (`TRAINED_CLASSIFIER=inat21`, a 10,000-species iNat21 EVA02-L) and
  detector size/resolution knobs (`DETECTOR_MODEL`, `DETECTOR_IMGSZ`) for cards with
  headroom beyond the original 4 GB target.

## 0.17.1

- **Ghost events are no longer kept.** A Frigate tracked object that ends with neither a
  clip nor a snapshot (seconds-long fly-throughs, wind triggers) is an event Frigate
  itself discards — its API 404s the id moments later. Aviary used to store these,
  queue them for identification, fail with `no_media`, and flood the review queue with
  unreviewable "no media / no ID" cards. Such events are now dropped at ingest (any
  provisional row from their earlier messages is removed too). Rows already in the
  review queue from before this fix age out via `identify_retain_days`, or can be
  deleted from the card.

## 0.17.0

- **Notifications say where in the yard.** Frigate zones now flow through everything:
  the notification blueprint reads "Gray Catbird detected at bird bath" (falling back to
  the camera name when no zone applies), the `aviary_detection` event payload gains a
  `zone` field, cards show "camera · zone", and the Recent page grows a Zone filter
  (only shown once zones exist). Existing detections are backfilled from their stored
  event JSON where possible.
- **Keep a clip forever.** Frigate detection cards gain a **📌 keep** toggle that flips
  Frigate's `retain_indefinitely` flag on the event, exempting the clip from retention
  expiry. Kept rows are also exempt from Aviary's own unidentified-row purge, so the
  card can't vanish while Frigate still holds the video.
- **Two birds in frame no longer cross-contaminate** (with aviary-id 0.6.0): clip-frame
  crops are anchored to the tracked object's own path from Frigate, so the event's bird
  — not whichever bird is more photogenic — is the one classified. Update the GPU
  container together with this add-on.

## 0.16.2

**Stale rejections are no longer invisible.** A rejected answer permanently vetoes that
species for its detection — correct, but silent: re-identify would keep refusing the
right bird with nothing anywhere saying why. Now the log names any rejections in force
each time a detection is identified, and when a plain re-identify stays uncertain while
answers are banned, the UI shows what is ruled out and offers to clear the rejections and
retry in one step.

Also fixes an exclusion asymmetry: the blacklist contributes both common and scientific
names and the identification service matches either, but the probe honored only common
names — so it could re-promote a species the service had just banned, producing an
answer neither layer would defend.

## 0.16.1

Pairs with aviary-id 0.5.0, which adds a **supervised classifier as the primary
identifier** — the same trained iNaturalist bird model behind Frigate's native
classification — so common species are right on day one with zero confirmations.
Zero-shot BioCLIP stays as coverage for species outside its training set and as the
engine behind the correction/learning layer. Update the GPU container together with
this add-on.

Add-on-side fix: **the probe now survives startup ordering.** Previously the add-on
checked for the GPU service exactly once, 15 seconds after start; if the container was
still coming up (rebuilds and cold caches take minutes), the entire learning layer
silently stayed off until the next add-on restart. The startup task now waits for the
service, and each identification self-heals an empty or stale probe inline — "builds on
first use" is finally literal.

## 0.16.0

Learning-pipeline overhaul: this release fixes three ways your labels were silently
teaching nothing, and makes the learner handle species whose males, females and juveniles
look nothing alike. Pair it with aviary-id 0.4.0.

- **Your stored training examples no longer vanish on a vocabulary change.** Embeddings
  were keyed by the service's full model+vocabulary fingerprint, so a routine eBird
  regional-list refresh (every 30 days by default) invisibly orphaned every example the
  probe had learned from. They are now keyed by the model alone, and a one-time migration
  rewrites existing rows — labels you gave weeks ago come back to life on first start.
- **Labelling now always teaches, immediately.** The probe rebuild used to silently skip
  when the probe had never been built (fresh install, service down at startup) — every
  label until the next restart taught nothing. Rebuilds also now fire on un-confirming,
  deleting a detection or species, and blacklisting, so unlearning is as immediate as
  learning.
- **Labels on failed identifications teach too.** Naming a detection whose identification
  failed stored no embedding, so the label trained nothing. Aviary now harvests an
  embedding for it in the background, and on startup backfills recent manually-labelled
  detections that never got one.
- **Dimorphic species actually work now.** The learner previously averaged every example
  into one prototype per species — a male-plus-female-plus-fledgling Northern Cardinal
  average resembles none of them, which is why 30 confirmed cardinals could still leave a
  shaded fledgling at 40%. It now matches against your actual stored examples
  (top-k nearest-neighbour), so a shaded female matches the stored female frames directly.
- **Frame consensus gates the answer.** The service now reports how many independent
  frames voted for the winner. A modest score with unanimous frames is accepted; a
  high score the frames actively disagreed about goes to the review queue instead of the
  dashboard.
- **The blend now actually blends.** A shape mismatch (the service says `common_name`,
  the blender expected `name`) left the zero-shot side of the mix silently empty: when
  the probe spoke at all, its raw distribution *replaced* the zero-shot answer at any
  blend weight — the main source of flat ~40% scores on well-learned species. The
  zero-shot side also now covers the top 50 candidates rather than 5, so a species the
  probe promotes competes against honest probabilities.
- Cards show when an answer was matched against your own confirmed examples
  (**· learned** on the ID badge, details in the tooltip), and Settings gains an
  **Evaluate accuracy** button — leave-one-out accuracy over your own birds, the "is my
  labelling working?" number.

## 0.15.0

- **Aviary now learns what your birds look like.** Until now identification was purely
  zero-shot: an image was compared against the *name* of each species. That is what makes an
  arbitrary regional species list possible, and it also leaves most of the model's accuracy
  unused — on the published NABirds benchmark the same frozen embeddings score 74.9%
  zero-shot and 92.4% once a classifier is trained on them. Every species you confirm now
  becomes an example the identifier matches against directly, closing that gap with no new
  model, no new hardware, and no extra inference cost.
- **It blends rather than replaces, and abstains when unsure.** A species with no examples
  scores exactly as it did before, so new birds are still found normally. And the match has
  to be genuinely close, not merely closest — a bird it has never seen does not get assigned
  to whichever species it happens to sit nearest.
- **It only learns from labels you stand behind** — confirmed species and manual
  identifications. Training on its own unreviewed guesses is how a classifier teaches itself
  its own mistakes.
- **New species start with examples on day one.** The iNaturalist reference photos Aviary
  already caches for each species are embedded in the background, so the feature is useful
  before you have confirmed anything. They count for less than your own frames and are
  displaced as real detections accumulate.
- `GET /api/probe/evaluate` reports leave-one-out accuracy over your own confirmed birds —
  the honest local measure, rather than a benchmark number. It scores stored embeddings
  against centroids rebuilt without them, so it touches no clips and is unaffected by
  changes to the crop pipeline.
- Manually identified detections are marked **by hand** on the card, and the Settings page
  shows how many species the identifier has learned and from how many examples.
- Adds `numpy` to the add-on (centroid maths). No configuration to set: the feature turns
  itself on species by species as examples accumulate.

## 0.14.2

- **You can now name a bird yourself, and see what the model was weighing up.** When an
  identification lands below the thresholds, the detection card shows the shortlist it
  actually considered — species and score, best first. Click one to accept it, or
  **✎ something else…** to type your own. That turns "it failed" into "it tried, got this
  close, and the right bird is second in the list".
- Manually named detections are marked `manual` and confirmed straight into the registry:
  a person typing a species is a stronger signal than any classifier, so it does not also
  need to queue for review. Confidence is left empty rather than set to 100% — that field
  means "how sure was the classifier", and a human answer does not belong on that scale.
- Free-typed names are checked against the identifier's regional species list and warn
  before creating something unrecognised, since a typo would otherwise mint a new species.
  Blacklisted species are refused outright.

- **Fixed: "bird" was being treated as a species.** An unidentified detection keeps the
  placeholder name `bird`, and every species query in Aviary took that at face value — so it
  queued for confirmation as a species, **took a dex number**, counted toward the species
  total, and appeared in the Recent page's species filter. This could not happen before
  identification existed, because `ignore_unclassified` meant such rows were never stored at
  all. Every species-facing query now excludes it in one shared place, and a cleanup on
  first start removes "bird" from the review queue and the reference caches if a previous
  version already recorded it. Detection *counts* are unchanged: a bird nobody could name
  was still a bird that showed up.
- **New Unidentified tab**, with a count in the nav. Detections waiting for a species are
  not a species waiting for approval — different thing, different action — so they get their
  own page instead of being mixed into the species review queue. Rows still being identified
  are shown as a count rather than cards, since they resolve in seconds. Replaces the
  temporary "N unidentified" link on Recent.
- `/species/bird` now redirects there rather than rendering a species page for the absence
  of a species.
- Aviary now sends its confidence thresholds to the identification service, which uses them
  to decide when to sample more frames before answering. It still applies the thresholds
  itself — the service only uses them to know when to try harder — so tuning stays in one
  place and needs no redeploy of the GPU container.

## 0.14.1

- **Fixed: the identify and ✗ wrong buttons never appeared.** They were only rendered on
  detections that already had an identification status, but every detection recorded before
  0.14.0 has none — so the buttons were invisible on exactly the detections you would want
  to try them on, and there was no way to identify anything from the UI at all. They now
  show on any Frigate detection whenever identification is configured. An unidentified
  detection offers **⌕ identify**; one that has already been through the identifier offers
  **↻ re-identify**.
- This also means you can identify your **existing** Frigate history a click at a time, and
  is the quickest way to try the whole thing without waiting for a live bird — Aviary
  discarded the unclassified detections, but Frigate kept every one of them, clips and all.
- The identification actions moved to their own row on the card. They had been sharing the
  line with the camera name, timestamp, **⤢ scrub / still** and **⬇ video** — six items in a
  no-wrap flex row on cards that narrow to 260px, which squashed the video controls.
- **Fixed: the startup backfill walked Frigate's entire history for nothing.** It paged
  on event count rather than on whether anything was imported, so with Frigate's own
  classifier off — where every historical event is unclassified and therefore dropped — it
  would request up to 500 pages of events on every single start and store none of them. It
  now stops after three consecutive pages that import nothing, while still walking a
  genuinely importable history to the end.
- Added `identify_exclude_blacklisted` (default on): blacklisted species are ruled out of
  the identifier's candidate list, so a bird that would have been misread as one gets its
  correct name instead of being discarded. Turn it off if you blacklisted a species that
  genuinely visits and you simply don't want it recorded.

## 0.14.0

- **Bird identification can now run on your own GPU, and it is much better at it.** A new
  companion container, **aviary-id**, runs BioCLIP 2 — a vision-language model trained on
  214M biological images — and scores each bird against only the species that occur in your
  region. Frigate's built-in classifier is a quantized MobileNet on CPU across ~964 world
  species; narrowing the candidate list to a county's few hundred removes most of its
  opportunities to be confidently wrong.
- It runs on a **separate host**. Aviary sends it an event id; it pulls the clip from
  Frigate itself, so no video passes through Home Assistant. It needs no access to Home
  Assistant, MQTT or Aviary's database — if the GPU box is down, audio detections keep
  arriving and the visual ones queue up until it returns.
- **Turn Frigate's bird classification off when you enable this.** Frigate keeps spotting
  that something bird-shaped is there and Aviary names it. Detections then arrive with no
  `sub_label`, so instead of being discarded by `ignore_unclassified` they are held as
  *pending* and announced once a species comes back. Notifications still fire exactly once
  per detection — they just fire when the identification lands, carrying a species worth
  reading rather than "bird".
- **A result has to clear a margin, not just a score.** 60% confidence in a Downy Woodpecker
  means very little when Hairy Woodpecker scored 58%. Anything failing `identify_min_score`
  or `identify_min_margin` keeps the name "bird" and goes to a review queue reachable from
  the Recent page. Those detections are kept rather than dropped — with Frigate's classifier
  off, discarding them would leave no record a bird was ever there — and purged after
  `identify_retain_days`.
- **↻ re-identify** on every Frigate detection. Change a threshold or the species list,
  re-run a bird you can name yourself, compare. The shipped thresholds are starting points,
  not recommendations, and this is how you tune them.
- **✗ wrong tells it when it got it wrong**, and it listens. The species is ruled out for
  that detection and the next best answer comes back; press it repeatedly to walk down the
  model's ranking. Because the model is zero-shot, the rejected species is removed before
  the scores are computed rather than suppressed afterwards, so its probability goes to the
  remaining birds and the runner-up gets to be genuinely confident. Rejections are
  remembered per detection, so pressing ✗ twice can't bounce back to the first guess.
- Blacklisted species are ruled out of the candidate list for every identification
  (`identify_exclude_blacklisted`, on by default). A bird that would have been misread as a
  blacklisted species now gets its correct name instead of being discarded outright. Turn it
  off if you blacklisted something that genuinely visits and you just don't want it recorded.
- A **smoke-test script** (`aviary-id/tools/smoke_test.py`) replays real Frigate bird events
  through the identifier and prints what each threshold pair would accept. You don't have to
  wait for a bird to tune this: Aviary discarded the unclassified detections, but Frigate
  kept every one of them, clips and all.
- **What Aviary heard now helps with what it saw.** Any species BirdNET-Go picked up within
  ten minutes of a detection is passed to the identifier as a prior. A cardinal that sang on
  its way to the feeder is genuinely more likely to be the bird in the picture. No other
  bird identifier can do this; it only works because Aviary already fuses both sources.
- Detection cards show the identification margin, and the Settings page reports whether the
  service is up, whether it actually found the GPU, and how many species are in its
  vocabulary — the three things that go wrong in practice.

## 0.13.1

- **Fixed: "Preparing a seekable copy…" never went away**, sitting over the clip even once
  it was playing. The code hid it by setting the `hidden` attribute, but the stylesheet gave
  it `display: flex`, which outranks the `display: none` that `hidden` relies on — so it
  could never disappear. The overlay is gone entirely; the stage stays black until the clip
  is ready and the browser's own buffering UI takes it from there.
- When a clip can't be made seekable (ffmpeg missing, Frigate unreachable) the player still
  plays the original and now says so in the title — *"Blue Jay · seeking unavailable"* —
  rather than through an overlay on top of the video.

## 0.13.0

- **Clips can be scrubbed properly now.** Frigate serves clips as fragmented MP4s whose
  header declares zero duration, so the browser never learned the length — the progress
  bar just grew as it played and there was nothing to seek against. The new **⤢ scrub /
  still** button opens a player that downloads a losslessly remuxed, seekable copy
  (ffmpeg `-c copy -movflags +faststart`) and plays it from memory, so dragging the
  scrubber is instant in both directions and makes no further server requests.
- **One-button still capture at native resolution.** **⬇ save still** writes the current
  frame to a PNG at the clip's encoded resolution rather than its on-screen size — lossless
  for that frame, so it's the best still the clip can produce. Named to match the video
  download: `blue-jay-20260811-142233-4.20s.png`.
- The player is built for a phone: full-width video, large transport buttons (⏪ 1s,
  single-frame stepping both directions, ⏩ 1s), arrow keys to seek, `,`/`.` to step,
  Escape to close. The inline card video keeps playing straight from Frigate, so quick
  previews are as fast as before.
- Inline videos gained `playsinline`, so iOS stops hijacking them into the system player.
- The live refresh no longer replaces the card list while the player is open — previously
  it only deferred for *playing* media, so a clip paused mid-scrub could be yanked away.
- Fixed: a remux that hit its timeout left ffmpeg running, writing into a temp directory
  that was about to be deleted. It's now killed.
- Nothing is cached server-side, so each open re-fetches and re-remuxes; if ffmpeg is
  missing the player falls back to direct playback rather than failing.

## 0.12.0

- **Fixed: deleting a BirdNET-Go detection failed with `403 Invalid CSRF token`**, so the
  detection vanished from Aviary but stayed in BirdNET-Go. BirdNET-Go guards its
  state-changing API with Echo's CSRF middleware, which compares an `X-CSRF-Token` header
  against a `csrf` cookie; Aviary sent neither. It now fetches a token from
  `/api/v2/app/config` before deleting, and the shared HTTP client replays the cookie.
- The token is cached and **retried once** if BirdNET-Go rejects it — a cached token goes
  stale whenever BirdNET-Go restarts, and the first delete afterwards would otherwise fail
  for no visible reason. A 403 that survives the retry now says so explicitly, so it can
  be told apart from an authentication failure.
- **Deleting at the source is now the default.** The remove menu leads with **Remove
  everywhere (Aviary + source)**; **Remove from Aviary only** is still there, second, and
  now says plainly that the source keeps its copy.
- **Blacklisting deletes at the source too.** It previously purged only Aviary's rows,
  which rather defeated "never record again" — the BirdNET-Go entries stayed put. The
  confirmation dialog says so before you agree.
- Note: deleting cleans up history, but BirdNET-Go stores its learned per-species dynamic
  thresholds separately, so deletions are unlikely to reset one. Use BirdNET-Go's own
  excluded-species list as well — see *Removing misclassifications* in the docs.

## 0.11.0

- **First sightings now notify.** `is_new_species` has always been source-agnostic — true
  only on a species' very first detection from any source — so a bird you had been hearing
  for months could finally appear on camera in complete silence, quietly flipping the dex
  from HEARD to SEEN with no alert. The new **Notify on first sighting** toggle (default
  **on**) covers exactly that moment, with **Notify on first recording** (default off) as
  the audio equivalent.
- Messages distinguish the milestones: **"First sighting! Wood Thrush detected on feeder
  camera"** versus **"New species! …"**, so a first photo of an old friend can't be
  mistaken for a bird you have never had. A brand-new species still reads "New species!"
  and notifies once, not twice.
- Both bypass the per-species cooldown — a once-ever event should never be swallowed
  because the same bird happened to be around a few minutes ago. The camera allowlist
  still applies: a first sighting on a camera you have excluded stays silent.
- Detection events gained `is_first_seen` and `is_first_heard` for custom automations.
  A detection never claims the other source's flag — a *heard* detection of a bird no
  camera has caught reports `is_first_seen: false`.
- Older automations are unaffected, and an updated blueprint running against an older
  add-on build simply never fires the new toggles. Run *Developer Tools → YAML → Reload
  Automations* after updating to pick up the new inputs.

## 0.10.0

- **New species now wait for your approval.** A species Aviary has never recorded before
  lands in an **Awaiting review** queue instead of joining the registry automatically, so
  one bad classification can no longer take a dex number and inflate your species count
  forever. While it waits it's kept out of the registry, dex numbering, species and
  new-species counts and the leaderboard — but its detections still record normally and
  its new-species notification still fires, because that's what tells you to go look.
- **Review screen**: open the species and you get its clips and spectrograms alongside
  reference photos and recordings of what the bird should look and sound like, then
  **Confirm species** or **Reject…**. Reject is the existing *Remove species…* menu,
  blacklist option included, so a misclassification is disposed of exactly one way.
  Confirming is reversible; rejecting deletes detections and isn't.
- **Reference photos**: up to three licensed iNaturalist photos per species, in their own
  card below the hero rather than replacing it — previously, once a camera had seen a
  species, the only picture available was your own snapshot. Only reusably-licensed photos
  are used (iNaturalist leaves all-rights-reserved photos unlicensed, and those are
  skipped) and the photographer and licence are always shown. No configuration needed;
  works on a Frigate-only install.
- New `require_species_confirmation` option, default **on**. Turn it off and every count
  goes back to including all species immediately, with nothing stranded in a queue.
- **Upgrading creates no backlog** — every species already in your database is marked
  confirmed once, on the first start after updating.
- Rejecting a species clears its approval too, so if it genuinely shows up later it queues
  for review again rather than silently rejoining the registry.

## 0.9.0

- **Notify from only the cameras you choose.** New **Cameras to notify on** input in the
  notification blueprint: list the Frigate cameras that should produce notifications and
  the rest go quiet. Built for the two-camera feeder setup — a wide camera for zone
  detection, a zoomed one for the classifications you actually trust. Empty (the default)
  means every camera notifies, so existing automations are unchanged.
- **New `ignore_cameras` add-on option** for the stronger version: detections from the
  listed cameras are never recorded at all — no dashboard entries, no species stats, no
  dex entries — so a low-quality camera can't pollute the registry. Applies to live ingest
  *and* backfill, so restarting won't re-import that camera's history.
- Both match the camera's Frigate name case-insensitively, and **neither affects
  BirdNET-Go audio detections** — audio events carry a node name in the same field, so a
  camera filter reaching them would silence every audio notification.
- `ignore_cameras` only affects detections ingested after it's set; anything already
  recorded stays. See *Filtering by camera* in the docs.
- Fixed `xeno_canto_api_key` not being exported by `run.sh`. It was still picked up from
  the options file, so the feature worked, but it now follows the same path as every other
  option.

## 0.8.0

- **Much better reference recordings, from xeno-canto.** The CRY button used to play
  iNaturalist observation sounds — incidental recordings of a sighting, with no quality
  metadata at all, which is why clips so often carried other birds, barking dogs and
  creaking doors. [xeno-canto](https://xeno-canto.org) is a dedicated bird-sound archive
  where every recording is rated and lists the other species audible in it. Aviary now
  picks **quality A** (falling back to B), 3–30 seconds, strongly preferring clips with
  **nothing else audible**.
- **Separate SONG and CALL buttons** on the species page — they're different sounds, and
  both are worth hearing. Species that only resolve through the fallback keep the single
  CRY button they have today.
- Needs a **free API key**: register at xeno-canto.org, copy the key from your account
  page into the new `xeno_canto_api_key` option, and restart. xeno-canto has required a
  key since October 2025. The key never reaches the browser — audio is fetched
  server-side through Aviary's existing media proxy.
- **Leave the option blank and nothing changes**: no key, no scientific name on record, or
  no acceptable xeno-canto recording all fall back to the previous iNaturalist behaviour.
  Licence checks and the always-visible recordist/licence credit apply to both sources,
  and the credit now follows whichever clip you're playing.
- The `species_audio` cache table is rebuilt on first start to hold one row per species
  *and* sound type. It is a pure metadata cache, so entries simply refill on next view.

## 0.7.1

- **Fixed: tapping a heard-bird or new-species notification opened a broken page.** The tap
  action pointed at `/hassio/ingress/<slug>`, a route Home Assistant removed in 2026.2 when
  add-ons became "apps" — the panel now lives at `/<addon_slug>`, and the old path falls
  through to HA's *not found* page. Seen-bird notifications were unaffected: they open the
  Frigate clip directly.
- **Notification taps now open the species' own page**, not the dashboard. HA's app panel
  loads the ingress iframe at its root and passes the rest of the URL over `postMessage`, so
  Aviary now listens for that and navigates itself.
- Notification tap actions require **Home Assistant 2026.2 or newer**. Everything else in the
  add-on is unchanged on older cores.
- Requires an add-on restart to take effect — the resolved slug is cached for the process
  lifetime.

## 0.7.0

- **Diet, foraging and habitat** on every species page — "Eats: Seeds & grain",
  "Forages: On the ground" — in both themes (data rows in the dex entry, chips on the
  About card). Comes from a compact subset of the **AVONET** dataset (Tobias et al. 2022,
  CC BY 4.0) bundled with the add-on: ~88 KB gzipped covering 10,661 species, keyed on
  eBird scientific names so it matches what BirdNET-Go emits. Entirely local — no API, no
  key, no rate limit, works offline and on a fresh install. Regenerate with
  `scripts/build_diet_table.py`; see `app/data/AVONET-CITATION.txt`.
- **Photos in the Pokédex registry.** Each registry row now shows the species' photo —
  full colour once a camera has seen it, darkened while it has only been heard — so the
  list is scannable at a glance.
- **The dex entry photo is always full colour**, including for a species you've only
  heard. That's the page you open to find out what the bird looks like; its HEARD/SEEN
  state is still carried by the header chip and the seen/heard gauges.
- Fixed the dex entry's header chip rendering `HEARD` in the gold "seen" colour: the
  modifier sits on the chip itself there, not on an ancestor as it does in the registry,
  so the descendant-only rule never matched.

## 0.6.0

- **Pokédex mode is now a real dex, not a recolour.** Two pages are rebuilt rather than
  restyled, on inset LCD-style screens with a bundled pixel typeface
  ([Silkscreen](https://github.com/googlefonts/silkscreen), SIL OFL, served locally so the
  theme works offline):
  - **Registry** (Species page): a numbered list in first-detection order with a
    `SEEN n/total` completion readout, per-row detection gauges and HEARD/SEEN state.
  - **Entry** (species page): the photo in one screen and a data readout in another —
    order, family, conservation status, first/last detection, best confidence and
    seen/heard gauges — plus a **CRY** button that plays the species' reference recording.
  - **Dex navigation**: `↑`/`↓` move the registry cursor, `↵` opens an entry, `Home`/`End`
    jump to either end, and `←`/`→` step between neighbouring entries. Built from ordinary
    links, so clicking (and JavaScript being disabled) still works.
  - Dashboard, Recent and Settings keep their normal layout in the dex palette.
- **Fixed: slow startup could make Home Assistant's ingress return 502.** The
  species-name canonicalisation pass ran on every start with an unindexed
  `COLLATE NOCASE` correlated subquery — quadratic in the size of the detections table,
  and it runs before the web server binds its port. Measured on a synthetic database:
  18.5s at 20k detections (and ~2min at the sizes real installs reach). Adding a NOCASE
  index on `scientific_name` and skipping the pass when there's nothing to remap brings
  that to **0.01s**, and the one-time upgrade (including building the index) to 0.09s at
  60k rows. The same index removes a full table scan that previously ran *per ingested
  detection*, so MQTT ingest and backfill are much cheaper too.
- The top bar now wraps instead of staying one row, so four nav links plus the theme
  toggle can't widen the page on a narrow screen.
- Web fonts are served with a correct `font/woff2` content type (Python's MIME table has
  no font entries, so they were being sent as `text/plain`).

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
