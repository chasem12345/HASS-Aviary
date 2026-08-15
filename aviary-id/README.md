# aviary-id

A GPU bird identification service for Frigate events. Give it a Frigate event id; it pulls
that event's clip and snapshot from Frigate, finds the bird, and returns a species.

It exists because Frigate's built-in bird classification (a quantized MobileNet over ~964
species, on CPU) is not accurate enough to be useful. This replaces it with BioCLIP 2 —
a ViT-L/14 vision-language model trained on 214M biological images covering 952K taxa —
scored against **only the species that occur in your region**.

It is designed to run on a **separate host** from Home Assistant. It needs no access to
Home Assistant, MQTT, the Supervisor, or Aviary's database. It is stateless: event id in,
species out.

## Why the regional species list matters more than the model

BioCLIP is zero-shot, meaning the candidate species set is *data*, not weights. Given the
whole world's birds it must separate ~11,000 taxa; given your county's eBird list it
separates a few hundred. That single narrowing is the largest accuracy lever available, and
it costs one free API key. Reported zero-shot accuracy on NABirds (555 species) is 74.9%
top-1 — a county list is usually smaller than that, and correspondingly easier.

Set `EBIRD_API_KEY` and `EBIRD_REGION`. Without both, the service falls back to a bundled
list of ~150 common North American yard birds, which works but is not tailored to you.

## How it decides

Frigate has already located the bird — that is its entire job — so the service uses
Frigate's own crops rather than re-deriving them:

- **`thumbnail.jpg`** is cropped to the object on an ended event. Used as-is, no detector.
- **`GET /api/events/{id}`** reports the snapshot's bounding box in pixels. The
  full-resolution snapshot is cropped to it.
- The COCO detector is used only for **clip frames**, where Frigate cannot give a box for an
  arbitrary timestamp.

If nothing can be localized, the service answers `no_bird`. It deliberately does **not**
fall back to classifying the whole frame: on a 1080p frame that leaves a feeder-distance
bird about ten pixels across, and it only ever produced confidently-wrong answers.

### Escalation

Effort scales with how hard the bird is. Most events finish on the first rung:

1. Classify the best crops, one per source frame where possible.
2. Still below the caller's thresholds -> classify crops the detector already found but did
   not use. One more forward pass, **zero** extra I/O.
3. Still unsure -> extract another pass of clip frames at timestamps *interleaved* with the
   first, so they are new views rather than neighbours of frames already seen.

Bounded by `MAX_FRAMES` (default 8) and one extra extraction pass. The response reports
`rounds`, and `timings` gives per-stage milliseconds so a slow event says which stage was
slow.

The thresholds come from the caller on each request — this service never gates on them, it
only uses them to decide whether to look harder.

## Requirements

- Docker with **nvidia-container-toolkit** installed on the host.
- An NVIDIA GPU with ≥4 GB VRAM. Developed against a Quadro P1000 (Pascal).
- Network reachability **to Frigate** from this host.
- ~6 GB disk: ~4 GB image, ~2 GB of model weights on the `/models` volume.

### Pascal / older-GPU warning

If your card is Maxwell, Pascal or Volta (Quadro P-series, GTX 9xx/10xx, Titan V):

- The Dockerfile pins **`torch==2.8.0+cu126`** deliberately. PyTorch removed sm_61 kernels
  from its cu128 and cu129 builds — installing torch from the default index gives you a
  container that silently runs on CPU or dies at the first kernel launch. If you bump the
  torch version, confirm a `+cu126` wheel still exists.
- Everything runs in **FP32**. Pascal has no tensor cores and roughly 1/64 FP16
  throughput, so half precision would be both slower and less accurate.
- NVIDIA driver branch **580 is the last** to support these architectures (security
  updates through October 2028), and **CUDA 13.0 drops them entirely**. This container
  will keep working, but it cannot follow CUDA forward on this hardware.

## Quick start

```bash
cp docker-compose.yml.example docker-compose.yml   # then edit FRIGATE_URL, EBIRD_*, AVIARY_ID_TOKEN
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` is gitignored — it holds your eBird key and shared secret. Keep the
`.example` as the committed template.

First start downloads ~2 GB of weights and encodes the species vocabulary (a minute or
two on a P1000). Both are cached on the `/models` volume; later starts take seconds.

Verify it came up on the GPU — if `cuda` is `false` on a GPU host, the toolkit or the
torch wheel is wrong, and nothing else you check will matter:

```bash
curl -s localhost:8100/healthz | python3 -m json.tool
```

```json
{
  "ok": true,
  "cuda": true,
  "device": "Quadro P1000",
  "model_version": "hf-hub:imageomics/bioclip-2/common@a1b2c3d4e5f60718",
  "embedding_key": "hf-hub:imageomics/bioclip-2",
  "trained_classifier": "aiy",
  "trained_coverage": 297,
  "species_count": 312,
  "species_source": "eBird US-CO-013"
}
```

`trained_coverage` is how many of the regional species the supervised model was trained
on; the rest are identified zero-shot only. `0` with `trained_classifier: "aiy"` means
the label mapping failed and is worth investigating.

## How identification is layered

1. **Supervised primary** — the AIY iNaturalist bird classifier (965 species, the same
   network behind Frigate's native bird classification). Trained on labelled photos, so
   it knows what a female cardinal looks like on day one. Masked to the regional list;
   carries `TRAINED_WEIGHT` of each frame's probability mix.
2. **Zero-shot coverage** — BioCLIP scores every regional species from its name, which
   is what identifies species the trained model has never seen, and contributes the
   remaining share everywhere else.
3. **Your corrections** (in the Aviary add-on) — BioCLIP's image embedding is matched
   against detections you have confirmed, refining answers toward *your* birds. This
   layer is optional polish; it is never required for common species.

## Configuration

All configuration is environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `TRAINED_CLASSIFIER` | `aiy` | Supervised primary classifier: `aiy` (Google's iNaturalist bird MobileNet — the model behind Frigate's native classification; CPU, ~5 ms/frame, no VRAM) or `none` (zero-shot only, for A/B measurement). |
| `TRAINED_WEIGHT` | `0.75` | The supervised model's share of the probability mix for species it was trained on. The rest is BioCLIP zero-shot, which alone covers species outside the trained set. |
| `FRIGATE_URL` | — | Frigate base URL. Requests may override it per-call. |
| `FRIGATE_HEADERS` | — | Extra headers for Frigate calls, `"Name: value, Name2: value2"`. Values may not contain a comma. |
| `AVIARY_ID_TOKEN` | — | Shared secret, checked as `Authorization: Bearer …`. Blank disables auth. |
| `EBIRD_API_KEY` | — | Free key from <https://ebird.org/api/keygen>. |
| `EBIRD_REGION` | — | e.g. `US-CO-013` (county), `US-CO`, `US`. County is best. |
| `EBIRD_REFRESH_DAYS` | `30` | How often to refresh the regional list. |
| `EXTRA_SPECIES` | — | Comma-separated names to add. |
| `EXCLUDE_SPECIES` | — | Comma-separated names to remove. |
| `SAMPLE_FRAMES` | `8` | Frames decoded from the clip before filtering. |
| `CLASSIFY_FRAMES` | `3` | Best frames classified per round. |
| `MAX_FRAMES` | `8` | Ceiling on frames classified across all escalation rounds. |
| `DETECTOR_BATCH` | `2` | Frames through the detector at once. Lower = lower VRAM peak. |
| `DETECTOR_CPU` | — | Detector on CPU, classifier on the GPU. |
| `NO_THUMBNAIL` | — | Don't use Frigate's cropped thumbnail. |
| `NO_EVENT_BOX` | — | Don't crop the snapshot to Frigate's box. |
| `DETECTOR_BACKEND` | `yolo` | Bird localizer for clip frames: `yolo` (YOLO11n — better on small/shaded birds, AGPL-3.0) or `frcnn` (torchvision Faster R-CNN, BSD-3). See the licensing note below. |
| `DETECTOR_THRESHOLD` | `0.3` | Minimum detector score for a usable bird box. Permissive on purpose: ranking, score-weighted fusion and the consensus vote suppress junk boxes downstream. |
| `CROP_PADDING` | `0.15` | Context added around the bird before cropping. |
| `CPU_ONLY` | — | Force CPU. ~5 s/event instead of ~0.3 s; useful for testing without a GPU. |
| `LABEL_FORMAT` | `common` | How species are described to the model: `common`, `binomial`, `binomial_common`, `taxonomy`. See below. |
| `NO_PROMPT_ENSEMBLE` | — | Single prompt instead of averaging 80 templates. |
| `MODEL_NAME` | `hf-hub:imageomics/bioclip-2` | Any open_clip-loadable model. |
| `CACHE_DIR` | `/models` | Weights, eBird caches, text-embedding cache. |
| `LOG_LEVEL` | `info` | |

`EXTRA_SPECIES` entries must be exact eBird common or scientific names — an unrecognised
name is skipped with a warning rather than guessed at, because a fabricated taxonomy
string would degrade that species' embedding.

## API

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /identify` | yes | `{event_id, frigate_url?, priors?}` → species |
| `POST /identify/image` | yes | multipart upload of a single image |
| `GET /species` | yes | the active candidate list |
| `GET /healthz` | no | liveness + device + vocabulary facts |

`/healthz` is deliberately unauthenticated so container healthchecks and Aviary's status
pill work without provisioning the token. It exposes no secrets.

`priors` is `{species name: multiplier}`. Aviary populates it from BirdNET-Go audio
detections near the same timestamp — a species *heard* on the microphone two minutes ago
is genuinely more likely to be the one in the picture. `3.0` means "treat as three times
more likely a priori".

```bash
curl -s -X POST localhost:8100/identify \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $AVIARY_ID_TOKEN" \
  -d '{"event_id": "1718900000.123456-abcdef"}' | python3 -m json.tool
```

```json
{
  "status": "ok",
  "common_name": "Black-capped Chickadee",
  "scientific_name": "Poecile atricapillus",
  "score": 0.71,
  "margin": 0.44,
  "runner_up": "Mountain Chickadee",
  "localized": true,
  "frames_used": 3,
  "per_frame": [
    {"origin": "snapshot", "det_score": 0.94, "top1": "Black-capped Chickadee",
     "top1_score": 0.78, "top2": "Mountain Chickadee", "top2_score": 0.11}
  ],
  "consensus": {"votes": 3, "supporting": 3, "fraction": 1.0, "agreed": true, "score": 0.74},
  "trained": true,
  "elapsed_ms": 1840
}
```

`status` is `ok`, `no_media` (Frigate had neither clip nor snapshot), `no_bird` (nothing in
the event could be localized, even after escalating), `out_of_memory`, `error`, or
`not_ready`.

`rounds` is how many classification passes it took: `1` means it was confident immediately,
more means it escalated. `timings` breaks the elapsed time down by stage.

`consensus` is the per-frame vote about the winner: each usable frame votes for its own
top-1, clip frames closer than 250 ms collapse into one vote, and `agreed` requires at
least 2 supporting votes covering ≥60% of all votes. `null` when fewer than two frames
could vote — "no data" is deliberately distinct from "frames disagreed". Aviary uses this
to accept a modest-but-unanimous answer and to hold back a high-scoring one the frames
actively disagreed about.

## Tuning

Run 20–30 events you can identify by eye and look at the score/margin distribution before
setting Aviary's `identify_min_score` and `identify_min_margin`. The defaults in the
add-on are starting points, not recommendations — the right values depend on your cameras
and how many confusable species share your region.

`per_frame` is the field to read while tuning. Three frames that independently agree is a
different kind of confident from three frames that each picked something different and
averaged into a winner, and the aggregate score alone cannot tell those apart. A low
`margin` almost always means two genuinely confusable species (Downy vs. Hairy Woodpecker,
the chickadees, the empidonax flycatchers) — that is a review-queue case, not a
notification.

## If it confuses similar species

Getting the family right but the species wrong — a Northern Cardinal read as a Summer
Tanager, say — usually means the **label format**, not the model or the crop.

BioCLIP scores an image against text, so how each species is described decides how
distinguishable they are. Under `taxonomy`, those two species read as:

```
Animalia Chordata Aves Passeriformes Cardinalidae Cardinalis cardinalis northern cardinal
Animalia Chordata Aves Passeriformes Cardinalidae Piranga rubra summer tanager
```

Five of eight words are identical, so the part that actually separates them is a small
fraction of the prompt. Under `common` ("Northern Cardinal" vs "Summer Tanager") they share
nothing. That is why `common` is the default — and it matches pybioclip's
`CustomLabelsClassifier`, the closest analogue to scoring a curated regional list.

`LABEL_FORMAT` accepts `common`, `binomial`, `binomial_common`, `taxonomy`. Worth trying
against birds you can identify yourself:

```bash
LABEL_FORMAT=binomial_common docker compose up -d
python3 tools/smoke_test.py --limit 30
```

Each format caches its own text embeddings, so switching back and forth costs the encode
once per format, not every time. `model_version` includes the format, so results recorded
under different formats are distinguishable in Aviary.

If a *specific* pair keeps getting confused and only one of them occurs where you are, the
cleanest fix is `EXCLUDE_SPECIES` — a species that cannot be there should not be a candidate.

## Out of memory on a shared GPU

The service needs roughly **1.3 GB of free VRAM** in steady state. That is not the same as
having a 4 GB card: if anything else on the box touches the GPU — a transcoder, another
detector, a desktop session — you are sharing.

`GET /healthz` reports the split, and `vram_other_mb` is the number to look at:

```json
{ "vram_total_mb": 4034, "vram_free_mb": 1890, "vram_ours_mb": 1290, "vram_other_mb": 854 }
```

`vram_other_mb` is memory held by processes that are not this container. `nvidia-smi` names
them. A CUDA out-of-memory error also lists every process on the card, which is usually
enough to identify the culprit on its own.

When it does run out, `/identify` answers `{"status": "out_of_memory"}` rather than failing
with a 500, and Aviary records the detection as unidentified and leaves it in the review
queue — so nothing is lost, and a **↻ re-identify** picks it up once there is room.

To fit in less, in escalating order of what you give up:

| Setting | Effect |
|---|---|
| `DETECTOR_BATCH: "1"` | Halves the detector's peak. Costs a few ms. |
| `DETECTOR_CPU: "1"` | Detector on CPU, classifier stays on the GPU. Costs a few hundred ms and frees its weights and activations entirely. |
| `CLASSIFY_FRAMES: "2"` | One fewer crop per event. Slightly less robust to a bad frame. |
| `SAMPLE_FRAMES: "5"` | Fewer frames decoded, so fewer chances of catching a good pose. |
| `MODEL_NAME: "hf-hub:imageomics/bioclip"` | BioCLIP v1, ViT-B/16 — about a quarter the size. Noticeably less accurate than BioCLIP 2, still well ahead of Frigate's built-in classifier. Delete the `text_*.npy` cache after switching. |

The classifier already drops its **text encoder** once the species embeddings are built
(~495 MB on ViT-L/14), and the detector pre-resizes frames to the size the model would have
resized them to anyway — a 1080p frame costs 2 MB of input tensor instead of 24 MB. Both are
automatic; the settings above are for when that is still not enough.

## Detector backends and licensing

`app/detector.py` ships two backends behind one interface, selected by
`DETECTOR_BACKEND`:

* **`yolo` (default)** — Ultralytics YOLO11n, running at 640px. Measurably better at the
  small, shaded, partly-occluded birds that clip frames actually contain. **Ultralytics
  is AGPL-3.0**: with this backend enabled, the combined aviary-id container includes
  AGPL software, which matters if you redistribute it or offer it as a network service.
  For purely personal use it changes nothing in practice.
* **`frcnn`** — torchvision's COCO Faster R-CNN (BSD-3, no extra dependency), running at
  320px. The original backend; set `DETECTOR_BACKEND: "frcnn"` if AGPL doesn't work for
  your deployment. Everything else behaves identically.

The backend only affects clip frames and boxless snapshots — Frigate's own thumbnail and
event-box crops bypass the detector entirely.
