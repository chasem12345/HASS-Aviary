"""BioCLIP-2 zero-shot species classifier.

Zero-shot means the species vocabulary is data, not weights: the text encoder turns each
candidate species into an embedding once at startup, and classification is then a matrix
multiply against the image embedding. Swapping regions or adding a species is a restart,
not a retraining run.

Hardware note: everything here runs in FP32 on purpose. The intended GPU is a Quadro
P1000 (Pascal), which has no tensor cores and roughly 1/64 FP16 throughput — half
precision would be slower *and* less accurate. FP32 ViT-L/14 is ~1.2 GB of weights, which
fits the card's 4 GB with room for a small batch.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import open_clip
import torch
from PIL import Image

from .prompts import OPENAI_IMAGENET_TEMPLATES
from .settings import Settings
from .species import Species

log = logging.getLogger("aviary_id.model")

# Text-encoder batch size. Small enough to stay well inside 4 GB alongside the image
# tower, large enough that 32k prompts don't take all day.
_TEXT_BATCH = 256

# How many species to report back. Enough to be useful when the top answer is wrong,
# short enough to be a glance rather than a list to read.
_TOP_N = 5


@dataclass
class FrameResult:
    origin: str
    det_score: float
    top1: str
    top1_score: float
    top2: str
    top2_score: float


@dataclass
class ClassifyResult:
    species: Species
    score: float
    margin: float
    runner_up: Optional[Species]
    # Best few species with their fused probabilities, best first. Surfaced to the user
    # when nothing clears the threshold so they can pick the right one by hand.
    candidates: list
    per_frame: list[FrameResult]
    embedding: str  # base64 float16 of the best crop's image embedding
    excluded: int = 0  # how many species were ruled out for this call


class Classifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() and not settings.cpu_only else "cpu"
        )
        if settings.cpu_only:
            log.info("CPU_ONLY set; running on CPU.")
        elif self.device.type == "cpu":
            # Loud, because the usual cause is a torch wheel built without sm_61 kernels
            # (the cu128/cu129 builds), which otherwise fails completely silently.
            log.warning(
                "CUDA is not available — running on CPU. If this host has an NVIDIA GPU, "
                "check nvidia-container-toolkit and that torch was installed from the "
                "cu126 index (cu128/cu129 wheels have no Pascal support)."
            )

        log.info("Loading %s ...", settings.model_name)
        started = time.monotonic()
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            settings.model_name, cache_dir=settings.cache_dir,
        )
        self.tokenizer = open_clip.get_tokenizer(settings.model_name)
        self.model.eval().to(self.device)
        log.info(
            "Model loaded in %.1fs on %s.", time.monotonic() - started,
            torch.cuda.get_device_name(self.device) if self.device.type == "cuda" else "cpu",
        )

        self.species: list[Species] = []
        self._text_features: Optional[torch.Tensor] = None
        # Identifies the (model, vocabulary) pair that produced a result. Stored by
        # Aviary alongside each detection so that after a model or region change you can
        # tell which rows are stale and worth re-identifying.
        self.vocab_digest: str = ""

    # ------------------------------------------------------------------ vocabulary

    def _digest(self, species: list[Species]) -> str:
        """Fingerprint of the model plus the exact label set.

        Any change to the species list — a new region, an added override, an eBird
        taxonomy update that renames a family — changes the digest, which both invalidates
        the embedding cache and marks previously-stored results as coming from a different
        configuration. That beats a manual version counter nobody remembers to bump.
        """
        digest = hashlib.sha256()
        digest.update(self.settings.model_name.encode())
        # The label format and the prompt ensemble both change the embeddings, so both are
        # part of the fingerprint — otherwise switching LABEL_FORMAT would silently reuse
        # a cache built for the previous one.
        digest.update(self.settings.label_format.encode())
        digest.update(str(self.settings.prompt_ensemble).encode())
        for s in species:
            digest.update(s.label(self.settings.label_format).encode())
            digest.update(b"\n")
        return digest.hexdigest()[:16]

    @property
    def model_version(self) -> str:
        # The label format is part of the identity: the same model and species list under a
        # different format is a different classifier, and results are not comparable.
        return (f"{self.settings.model_name}"
                f"/{self.settings.label_format}@{self.vocab_digest}")

    def _free_text_tower(self) -> None:
        """Drop the text encoder once the species embeddings exist.

        Worth roughly 495 MB on ViT-L/14 in FP32 (~124M of the model's ~428M parameters),
        and it is pure dead weight: the vocabulary is fixed at startup, so after
        ``set_species`` nothing ever calls ``encode_text`` again. Changing the species list
        requires a restart regardless, which rebuilds the model.

        On a 4 GB card that is shared with other workloads this is the difference between
        fitting and not. Defensive throughout — open_clip exposes the text tower
        differently across model classes (CLIP vs CustomTextCLIP), and failing to free it
        costs memory but must never cost correctness.
        """
        freed = False
        try:
            # CustomTextCLIP keeps the whole tower in one submodule.
            if hasattr(self.model, "text"):
                del self.model.text
                freed = True
            else:
                for attr in ("transformer", "token_embedding", "ln_final",
                             "positional_embedding", "text_projection", "attn_mask"):
                    if hasattr(self.model, attr):
                        delattr(self.model, attr)
                        freed = True
        except (AttributeError, TypeError) as exc:
            log.debug("Could not free the text tower (harmless, just uses more VRAM): %s", exc)
            return
        if freed and self.device.type == "cuda":
            torch.cuda.empty_cache()
            log.info("Released the text encoder; %s", self.memory_summary())

    def set_species(self, species: list[Species]) -> None:
        self.species = species
        self.vocab_digest = self._digest(species)
        path = os.path.join(self.settings.cache_dir, f"text_{self.vocab_digest}.npy")
        if os.path.exists(path):
            try:
                features = np.load(path)
                if features.shape[0] == len(species):
                    self._text_features = torch.from_numpy(features).to(self.device)
                    log.info("Loaded cached text embeddings for %d species.", len(species))
                    self._free_text_tower()
                    return
                log.warning("Cached text embeddings had %d rows, expected %d; recomputing.",
                            features.shape[0], len(species))
            except (OSError, ValueError) as exc:
                log.warning("Could not load text embedding cache: %s", exc)

        log.info(
            "Encoding %d species x %d prompt template(s), label format %r. This takes a "
            "minute or two on first run and is then cached to %s.",
            len(species),
            len(OPENAI_IMAGENET_TEMPLATES) if self.settings.prompt_ensemble else 1,
            self.settings.label_format, self.settings.cache_dir,
        )
        started = time.monotonic()
        features = self._encode_species(species)
        self._text_features = features
        log.info("Text embeddings ready in %.1fs.", time.monotonic() - started)

        try:
            os.makedirs(self.settings.cache_dir, exist_ok=True)
            np.save(path, features.cpu().numpy())
        except OSError as exc:
            log.warning("Could not cache text embeddings: %s", exc)

        self._free_text_tower()

    @torch.inference_mode()
    def _encode_species(self, species: list[Species]) -> torch.Tensor:
        """One L2-normalized embedding per species, averaged over the prompt ensemble.

        Accumulated into a list and stacked rather than pre-allocated: the embedding
        width differs between open_clip's CLIP and CustomTextCLIP wrappers, and reaching
        into the model to find it is exactly the sort of thing that breaks on an
        open_clip upgrade.
        """
        fmt = self.settings.label_format
        templates = (OPENAI_IMAGENET_TEMPLATES if self.settings.prompt_ensemble
                     else ("a photo of a {}.",))
        rows: list[torch.Tensor] = []
        for i, sp in enumerate(species):
            prompts = [t.format(sp.label(fmt)) for t in templates]
            chunks = []
            for start in range(0, len(prompts), _TEXT_BATCH):
                tokens = self.tokenizer(prompts[start:start + _TEXT_BATCH]).to(self.device)
                feats = self.model.encode_text(tokens)
                # Normalize each phrasing before averaging so a long prompt with a larger
                # norm can't dominate the class embedding.
                chunks.append(feats / feats.norm(dim=-1, keepdim=True))
            mean = torch.cat(chunks).mean(dim=0)
            rows.append(mean / mean.norm())
            if i and i % 100 == 0:
                log.info("  encoded %d/%d species", i, len(species))
        return torch.stack(rows)

    # ------------------------------------------------------------------ inference

    @torch.inference_mode()
    def _indices_for(self, names: list[str]) -> list[int]:
        """Vocabulary positions for a list of common or scientific names."""
        wanted = {n.strip().lower() for n in names if n and n.strip()}
        if not wanted:
            return []
        return [
            i for i, sp in enumerate(self.species)
            if sp.com_name.lower() in wanted or sp.sci_name.lower() in wanted
        ]

    @torch.inference_mode()
    def classify(
        self,
        crops: list[Image.Image],
        det_scores: list[float],
        origins: list[str],
        priors: Optional[dict[str, float]] = None,
        exclude: Optional[list[str]] = None,
    ) -> Optional[ClassifyResult]:
        """Classify one event's crops and fuse them into a single answer.

        ``exclude`` removes species from consideration for this call only — a user
        rejecting a wrong answer, or a species they never want suggested. Masked in logit
        space *before* the softmax rather than zeroed afterwards, so the probability the
        excluded species would have taken is redistributed across the remaining
        candidates. Suppressing it after the fact would leave the runner-up looking
        artificially weak and make every reroll read as low confidence.
        """
        if self._text_features is None or not self.species or not crops:
            return None

        batch = torch.stack([self.preprocess(img) for img in crops]).to(self.device)
        image_features = self.model.encode_image(batch)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)

        logit_scale = self.model.logit_scale.exp()
        logits = logit_scale * image_features @ self._text_features.T

        excluded = self._indices_for(exclude or [])
        if excluded:
            # Refuse to mask the entire vocabulary: an empty candidate set makes softmax
            # produce NaN, and "everything is wrong" is not an answer we can give anyway.
            if len(excluded) >= len(self.species):
                log.warning("Exclusions cover every species in the vocabulary; ignoring them.")
            else:
                logits[:, excluded] = float("-inf")
                log.debug("Excluded %d species from consideration.", len(excluded))

        probs = logits.softmax(dim=-1)  # [frames, species]

        if priors:
            probs = self._apply_priors(probs, priors)

        # Fuse frames by detector confidence: a crisp, confidently-detected bird should
        # count for more than a blurry one caught mid-wingbeat.
        weights = torch.tensor(det_scores, device=self.device, dtype=probs.dtype)
        weights = weights / weights.sum() if weights.sum() > 0 else torch.full_like(
            weights, 1.0 / len(det_scores)
        )
        fused = (probs * weights.unsqueeze(1)).sum(dim=0)

        # Keep a short list, not just the winner. When the answer lands below the caller's
        # thresholds, "here is what it considered" is far more useful than "it failed" —
        # it shows the model tried, and the right bird is often sitting at number two.
        top = torch.topk(fused, k=min(_TOP_N, len(self.species)))
        best_idx = int(top.indices[0])
        best_score = float(top.values[0])
        runner_up = self.species[int(top.indices[1])] if len(top.indices) > 1 else None
        second_score = float(top.values[1]) if len(top.values) > 1 else 0.0

        return ClassifyResult(
            species=self.species[best_idx],
            score=best_score,
            margin=best_score - second_score,
            runner_up=runner_up,
            candidates=[
                (self.species[int(i)], float(v))
                for i, v in zip(top.indices.tolist(), top.values.tolist())
            ],
            per_frame=self._per_frame(probs, det_scores, origins),
            embedding=self._encode_embedding(image_features, probs, best_idx),
            excluded=len(excluded),
        )

    def _apply_priors(self, probs: torch.Tensor, priors: dict[str, float]) -> torch.Tensor:
        """Reweight in probability space, then renormalize.

        Priors arrive as {species name: multiplier} — e.g. a species BirdNET-Go heard on
        the microphone minutes ago is treated as N times more likely a priori. Doing this
        multiplicatively on probabilities (rather than additively on logits) keeps the
        knob interpretable: 3.0 means "three times more likely", at any confidence level.
        """
        multipliers = torch.ones(len(self.species), device=probs.device, dtype=probs.dtype)
        lookup = {name.lower(): mult for name, mult in priors.items()}
        applied = 0
        for i, sp in enumerate(self.species):
            mult = lookup.get(sp.com_name.lower()) or lookup.get(sp.sci_name.lower())
            if mult:
                multipliers[i] = mult
                applied += 1
        if not applied:
            return probs
        log.debug("Applied %d audio priors.", applied)
        adjusted = probs * multipliers.unsqueeze(0)
        return adjusted / adjusted.sum(dim=-1, keepdim=True)

    def _per_frame(self, probs: torch.Tensor, det_scores: list[float],
                   origins: list[str]) -> list[FrameResult]:
        """Top-2 per frame — the output that makes threshold tuning tractable.

        A run where every frame agrees is a different kind of confident from one where
        three frames each pick a different species and the winner emerged from averaging.
        The aggregate score alone can't distinguish those.
        """
        results = []
        k = min(2, probs.shape[1])
        top = torch.topk(probs, k=k, dim=-1)
        for row in range(probs.shape[0]):
            idx = top.indices[row].tolist()
            vals = top.values[row].tolist()
            results.append(FrameResult(
                origin=origins[row],
                det_score=det_scores[row],
                top1=self.species[idx[0]].com_name,
                top1_score=float(vals[0]),
                top2=self.species[idx[1]].com_name if k > 1 else "",
                top2_score=float(vals[1]) if k > 1 else 0.0,
            ))
        return results

    def _encode_embedding(self, image_features: torch.Tensor, probs: torch.Tensor,
                          best_idx: int) -> str:
        """Base64 float16 of the embedding from whichever frame backed the winner best.

        Stored by Aviary against the detection. Nothing reads it yet — it exists so that
        once enough species are human-confirmed, a nearest-centroid classifier over your
        own birds can be built without re-running the GPU over the entire history.
        """
        frame = int(torch.argmax(probs[:, best_idx]))
        vector = image_features[frame].to(torch.float16).cpu().numpy()
        return base64.b64encode(vector.tobytes()).decode("ascii")

    # ------------------------------------------------------------------ diagnostics

    def device_name(self) -> str:
        if self.device.type == "cuda":
            return torch.cuda.get_device_name(self.device)
        return "cpu"

    def memory(self) -> dict:
        """VRAM facts, including what OTHER processes are holding.

        ``mem_get_info`` reports the driver's view of the whole device, not just this
        process — which is the number that actually matters on a card shared with
        transcoders or another detector. torch's own counters can look perfectly healthy
        while the device is full.
        """
        if self.device.type != "cuda":
            return {}
        try:
            free, total = torch.cuda.mem_get_info(self.device)
        except (RuntimeError, AssertionError):
            return {}
        ours = torch.cuda.memory_reserved(self.device)
        return {
            "vram_total_mb": round(total / 1048576),
            "vram_free_mb": round(free / 1048576),
            "vram_ours_mb": round(ours / 1048576),
            # total - free - ours. Anything here is another process on the same GPU, and
            # it is the first thing to check when identification starts failing.
            "vram_other_mb": max(0, round((total - free - ours) / 1048576)),
        }

    def memory_summary(self) -> str:
        m = self.memory()
        if not m:
            return "VRAM: n/a"
        return (f"VRAM {m['vram_free_mb']} MB free of {m['vram_total_mb']} MB "
                f"(ours {m['vram_ours_mb']} MB, other processes {m['vram_other_mb']} MB)")
