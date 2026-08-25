"""Supervised bird classifier: the model that already knows what a cardinal is.

BioCLIP is zero-shot — it compares the image against species *names* — which is what
makes an arbitrary regional vocabulary possible, and also why it can look at a clean
female Northern Cardinal and answer "Tufted Titmouse": nobody ever showed it labelled
cardinals. This module adds the model that WAS shown them: Google's AIY iNaturalist bird
classifier (965 species), the exact network behind Frigate's native bird classification
and WhosAtMyFeeder. It is a quantized MobileNet — ~5 ms per frame on CPU, zero VRAM —
so it costs the GPU nothing.

Division of labour after this module:

* The supervised backend carries the primary vote (``TRAINED_WEIGHT``, default 0.75)
  for every regional species it was trained on.
* BioCLIP covers the regional species it has never seen, breaks ties, and — crucially —
  still produces the image embedding the caller's correction/learning layer runs on.

Two backends behind the same small interface (``frame_probs`` over the caller's
vocabulary), selected by TRAINED_CLASSIFIER:

* ``aiy`` (default) — the quantized MobileNet described above. CPU, ~5 ms, zero VRAM;
  the right answer on a host with no CUDA.
* ``inat21`` — a timm EVA02-L fine-tuned on iNaturalist 2021 (10,000 species, 92% top-1
  there vs. AIY's 2019-era MobileNet). GPU, ~1.2 GB VRAM, FP32. Checkpoint license is
  cc-by-nc-4.0 — see the README licensing note.
* ``auto`` — inat21 when CUDA is available, aiy otherwise.
"""

from __future__ import annotations

import logging
import os
import urllib.request
from typing import Optional

import numpy as np
import torch
from PIL import Image

from .species import Species

log = logging.getLogger("aviary_id.trained")

# Pinned to the exact commit WhosAtMyFeeder vendors, not a floating branch: the label
# order IS the model's output contract, and a silently updated file would misname every
# bird from then on.
_PINNED = ("https://raw.githubusercontent.com/google-coral/test_data/"
           "104342d2d3480b3e66203073dac24f4e2dbb4c41/")
_MODEL_URL = _PINNED + "mobilenet_v2_1.0_224_inat_bird_quant.tflite"
_LABELS_URL = _PINNED + "inat_bird_labels.txt"

_INPUT_SIZE = 224
# Letterbox fill. Gray rather than black: the value the sibling projects validated for
# this checkpoint, and neutral gray biases the quantized network less than a hard edge.
_PAD_COLOR = (128, 128, 128)


def _download(url: str, path: str) -> None:
    log.info("Downloading %s ...", os.path.basename(path))
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
        out.write(resp.read())
    os.replace(tmp, path)


class _VocabMappedClassifier:
    """Shared half of every supervised backend: label -> regional-vocabulary mapping.

    Subclasses fill ``_sci_to_index``/``_com_to_index`` from their own label source
    (a labels file, a checkpoint config); ``set_species`` and ``coverage`` are identical
    from there on, so they live here rather than being copied per backend.
    """

    _display_name = "Supervised"

    def __init__(self) -> None:
        self._sci_to_index: dict[str, int] = {}
        self._com_to_index: dict[str, int] = {}
        self._model_indices = np.zeros(0, dtype=np.int64)
        self._vocab_indices = np.zeros(0, dtype=np.int64)
        self._n_vocab = 0

    def set_species(self, species: list[Species]) -> None:
        """Map the regional vocabulary onto model output positions.

        Scientific name first — it is the stable identity across taxonomies — with the
        common name as fallback for the handful of renames since the model was trained.
        Regional species the model has never seen simply stay unmapped: BioCLIP alone
        speaks for them in the fusion.
        """
        model_idx: list[int] = []
        vocab_idx: list[int] = []
        uncovered: list[str] = []
        for v, sp in enumerate(species):
            index = self._sci_to_index.get(sp.sci_name.strip().lower())
            if index is None:
                index = self._com_to_index.get(sp.com_name.strip().lower())
            if index is not None:
                model_idx.append(index)
                vocab_idx.append(v)
            else:
                uncovered.append(sp.com_name)
        self._model_indices = np.asarray(model_idx, dtype=np.int64)
        self._vocab_indices = np.asarray(vocab_idx, dtype=np.int64)
        self._n_vocab = len(species)
        log.info("%s classifier covers %d of %d regional species.",
                 self._display_name, len(vocab_idx), len(species))
        if uncovered:
            # Named, not just counted: these are the species that will always ride the
            # zero-shot + reference-photo path, so a wrong answer on one of them is
            # expected behavior to tune around, not a mystery.
            log.info("Zero-shot-only species (not in the trained model): %s",
                     ", ".join(sorted(uncovered)))

    @property
    def coverage(self) -> int:
        return int(self._vocab_indices.size)

    @property
    def version_tag(self) -> str:
        """Short identifier for provenance strings (model_version suffixes)."""
        return self._display_name.lower()


class TrainedClassifier(_VocabMappedClassifier):
    """The AIY iNaturalist bird model, masked to the caller's regional vocabulary."""

    _display_name = "AIY"

    def __init__(self, cache_dir: str):
        super().__init__()
        # Imported here so the service still starts with TRAINED_CLASSIFIER=none even
        # if the runtime were missing from the image. ai-edge-litert is what the image
        # ships (the legacy tflite-runtime package predates numpy 2 and crashes beside
        # it); the fallback keeps this file usable in other environments.
        try:
            from ai_edge_litert.interpreter import Interpreter
        except ImportError:
            from tflite_runtime.interpreter import Interpreter

        os.makedirs(cache_dir, exist_ok=True)
        model_path = os.path.join(cache_dir, "aiy_birds_v1.tflite")
        labels_path = os.path.join(cache_dir, "aiy_birds_labels.txt")
        try:
            if not os.path.exists(model_path):
                _download(_MODEL_URL, model_path)
            if not os.path.exists(labels_path):
                _download(_LABELS_URL, labels_path)
        except OSError as exc:
            # Hard fail, loudly. Degrading to zero-shot-only silently would reintroduce
            # the exact quality problem this module exists to fix, with no visible cause.
            raise RuntimeError(
                f"Could not fetch the AIY bird classifier ({exc}). Fix connectivity to "
                f"raw.githubusercontent.com, place the files in the cache volume "
                f"yourself, or set TRAINED_CLASSIFIER=none to run zero-shot only."
            ) from exc

        self._interp = Interpreter(model_path=model_path, num_threads=4)
        self._interp.allocate_tensors()
        self._input = self._interp.get_input_details()[0]
        self._output = self._interp.get_output_details()[0]

        # Labels are "Genus species (Common Name)"; index order is the output contract.
        # Non-species rows (the trailing "background") simply never parse, which is also
        # what keeps them out of the mapping below.
        with open(labels_path, encoding="utf-8") as f:
            for index, line in enumerate(f):
                name = line.strip()
                if "(" not in name or not name.endswith(")"):
                    continue
                sci, _, common = name[:-1].partition(" (")
                if sci.strip():
                    self._sci_to_index.setdefault(sci.strip().lower(), index)
                if common.strip():
                    self._com_to_index.setdefault(common.strip().lower(), index)

        log.info("AIY bird classifier ready on CPU (%d labelled species).",
                 len(self._sci_to_index))

    # ------------------------------------------------------------------- inference

    def _preprocess(self, image: Image.Image) -> np.ndarray:
        """Letterbox to 224x224 uint8 — the contract this quantized checkpoint expects."""
        img = image.convert("RGB")
        scale = min(_INPUT_SIZE / img.width, _INPUT_SIZE / img.height)
        resized = img.resize((max(1, round(img.width * scale)),
                              max(1, round(img.height * scale))), Image.BICUBIC)
        canvas = Image.new("RGB", (_INPUT_SIZE, _INPUT_SIZE), _PAD_COLOR)
        canvas.paste(resized, ((_INPUT_SIZE - resized.width) // 2,
                               (_INPUT_SIZE - resized.height) // 2))
        return np.asarray(canvas, dtype=np.uint8)[np.newaxis, ...]

    def _infer(self, image: Image.Image) -> np.ndarray:
        self._interp.set_tensor(self._input["index"], self._preprocess(image))
        self._interp.invoke()
        raw = self._interp.get_tensor(self._output["index"])[0]
        scale, zero = self._output["quantization"]
        if scale:
            return (raw.astype(np.float32) - zero) * scale
        return raw.astype(np.float32)

    def frame_probs(self, crops: list[Image.Image]) -> Optional[np.ndarray]:
        """Per-frame probabilities over the CALLER'S vocabulary. None if unusable.

        Rows are deliberately NOT renormalized: mass on background or non-regional
        species is left missing, so a frame the model is unsure about contributes
        little to the fusion instead of having its noise inflated to full weight —
        the zero-shot side simply matters more on that frame.
        """
        if not self._vocab_indices.size or not crops:
            return None
        out = np.zeros((len(crops), self._n_vocab), dtype=np.float32)
        for row, image in enumerate(crops):
            probs = self._infer(image)
            out[row, self._vocab_indices] = probs[self._model_indices]
        return out


class Inat21Classifier(_VocabMappedClassifier):
    """A timm iNaturalist-2021 fine-tune, masked to the caller's regional vocabulary.

    FP32 on purpose — the target cards are Pascal (see model.py). The checkpoint's own
    config.json carries its 10,000 class names as scientific binomials in output-index
    order, so the label order is pinned to the exact weights the same way the AIY
    download pins its labels file.
    """

    _display_name = "iNat21"

    def __init__(self, settings, device: torch.device):
        # Imported here so aiy/none deployments never pay for (or need) timm.
        import json

        import timm
        from huggingface_hub import hf_hub_download
        from timm.data import create_transform, resolve_model_data_config

        super().__init__()
        self.device = device
        self.batch = max(1, settings.trained_batch)
        repo = settings.trained_model.removeprefix("hf_hub:").removeprefix("hf-hub:")
        try:
            # HF_HOME points at the model volume, so both downloads land beside the
            # BioCLIP snapshot and survive container rebuilds.
            self.model = timm.create_model(f"hf_hub:{repo}", pretrained=True)
            config_path = hf_hub_download(repo_id=repo, filename="config.json")
        except (OSError, ValueError, RuntimeError) as exc:
            # Hard fail, loudly — same policy as the AIY download above.
            raise RuntimeError(
                f"Could not fetch the iNat21 classifier '{repo}' ({exc}). Fix "
                f"connectivity to huggingface.co, pre-populate the HF cache on the "
                f"model volume, or set TRAINED_CLASSIFIER=aiy."
            ) from exc
        self.model.eval().to(device)
        data_config = resolve_model_data_config(self.model)
        self._transform = create_transform(**data_config, is_training=False)

        with open(config_path, encoding="utf-8") as f:
            names = json.load(f).get("label_names") or []
        if not names:
            raise RuntimeError(
                f"The checkpoint '{repo}' has no label_names in its config.json; "
                f"without them its outputs cannot be mapped to species."
            )
        for index, name in enumerate(names):
            self._sci_to_index.setdefault(str(name).strip().lower(), index)
        # iNat21 labels carry no common names; _com_to_index stays empty and the
        # common-name fallback in set_species simply never fires.
        log.info("iNat21 classifier ready on %s (%s, %d labelled taxa).",
                 device, repo, len(names))

    @torch.inference_mode()
    def frame_probs(self, crops: list[Image.Image]) -> Optional[np.ndarray]:
        """Per-frame probabilities over the CALLER'S vocabulary. None if unusable.

        Rows are deliberately NOT renormalized, same contract as the AIY backend. This
        model has no explicit background class; instead, a junk crop's softmax mass
        lands mostly on the ~9,900 non-regional taxa and is dropped by the scatter
        below, so an unsure frame still contributes little to the fusion.
        """
        if not self._vocab_indices.size or not crops:
            return None
        rows: list[np.ndarray] = []
        for start in range(0, len(crops), self.batch):
            batch = torch.stack(
                [self._transform(img.convert("RGB"))
                 for img in crops[start:start + self.batch]]
            ).to(self.device)
            probs = self.model(batch).softmax(dim=-1)
            # CPU float32 numpy is the contract: the fusion feeds this to
            # torch.from_numpy and mixes on the CPU.
            rows.append(probs.float().cpu().numpy())
        full = np.concatenate(rows, axis=0)
        out = np.zeros((len(crops), self._n_vocab), dtype=np.float32)
        out[:, self._vocab_indices] = full[:, self._model_indices]
        return out


def make_trained(settings, device: torch.device) -> Optional[_VocabMappedClassifier]:
    """Build the configured supervised backend, or None when disabled.

    ``auto`` resolves to the GPU backend only when it would actually run on CUDA —
    EVA02-L in FP32 on a CPU is seconds per frame, which is the wrong trade everywhere
    except an explicit opt-in.
    """
    choice = settings.trained_classifier
    if choice == "none":
        log.info("Supervised classifier disabled (TRAINED_CLASSIFIER=none); "
                 "identification is zero-shot only.")
        return None
    if choice == "auto":
        choice = "inat21" if device.type == "cuda" else "aiy"
        log.info("TRAINED_CLASSIFIER=auto resolved to %s (device %s).", choice, device)
    if choice == "inat21":
        if device.type != "cuda":
            log.warning("TRAINED_CLASSIFIER=inat21 on %s: EVA02-L in FP32 without CUDA "
                        "is seconds per frame. TRAINED_CLASSIFIER=aiy is the intended "
                        "CPU backend.", device)
        return Inat21Classifier(settings, device)
    return TrainedClassifier(settings.cache_dir)
