"""View-model for a visit card: what one Frigate review item looks like on screen.

No SQL here — ``db.feed_page`` hands over the visit row with its members already
attached, and this module only decides how to present it: which species were present,
which member's crop represents each, which of them leads the card on a species-filtered
feed (the *focus*), how long it lasted, and what window the play button should ask the
recordings player for. Kept out of the template so the rules (best representative, focus
ordering, fallback media, window clamping) are testable and stated once.
"""

from __future__ import annotations

import time
from typing import Optional

from . import kept
from .db import UNNAMED


def _rep_key(det: dict) -> tuple:
    """Sort key for 'the member that best represents this species': the highest
    identification score, then Frigate's own confidence, then the most recent."""
    return (
        float(det.get("id_score") or 0.0),
        float(det.get("confidence") or 0.0),
        float(det.get("start_time") or 0.0),
    )


def species_present(members: list[dict]) -> list[dict]:
    """Species seen in this visit, in order of first appearance, each with the member
    that best represents it (for the thumbnail) and how many members carried it.

    The placeholder name is skipped — unidentified members are reported separately by
    ``card_model`` — so a visit that is half cardinal, half "couldn't tell" lists the
    cardinal and a count of the unnamed rest.
    """
    seen: dict[str, dict] = {}

    def add(name: str, sci: Optional[str], det: dict, score, idx: int) -> None:
        key = name.lower()
        entry = seen.get(key)
        rep = {**det, "id_score": score if score is not None else det.get("id_score"),
               "subject_idx": idx}
        if entry is None:
            seen[key] = {"common_name": name, "scientific_name": sci, "rep": rep, "count": 1}
            return
        entry["count"] += 1
        if not entry["scientific_name"] and sci:
            entry["scientific_name"] = sci
        if _rep_key(rep) > _rep_key(entry["rep"]):
            entry["rep"] = rep

    for det in members:  # members arrive oldest first
        present = det.get("present")
        if present:
            # Every bird identified in this event: the tracked one (idx 0) plus any
            # other the identifier found and named — see db.species_present.
            for p in present:
                name = (p.get("common_name") or "").strip()
                if name and name.lower() != UNNAMED:
                    add(name, p.get("scientific_name"), det, p.get("score"), int(p.get("idx") or 0))
            continue
        name = (det.get("common_name") or "").strip()
        if not name or name.lower() == UNNAMED:
            continue
        add(name, det.get("scientific_name"), det, det.get("id_score"), 0)
    return list(seen.values())


def play_window(visit: dict, pad: float) -> Optional[dict]:
    """The recordings window the ▶ button should request, or None while the visit is open.

    Padded like every other player window, then clamped to the media route's cap so a
    marathon visit plays its first quarter-hour rather than getting a 400. ``clamped``
    tells the template to say so on the button.
    """
    start, end = visit.get("start_time"), visit.get("end_time")
    if start is None or end is None or end <= start:
        return None
    p_start, p_end = kept.padded_window(float(start), float(end), pad)
    clamped = (p_end - p_start) > kept.RECORDING_MAX_S
    if clamped:
        p_end = p_start + kept.RECORDING_MAX_S
    return {"start": p_start, "end": p_end, "clamped": clamped}


def _fallback(species: list[dict], members: list[dict]) -> Optional[dict]:
    """The member whose media stands in when the recordings window is unavailable.

    The lead species' representative when its event has a clip; else the first member
    with a clip, so ▶ does not vanish because the lead was best seen in a clip-less event;
    else the lead's own row (its snapshot still serves as the hero); else any member.
    """
    lead = species[0]["rep"] if species else None
    if lead and lead.get("has_clip"):
        return lead
    with_clip = next((m for m in members if m.get("has_clip")), None)
    return with_clip or lead or (members[0] if members else None)


def card_model(visit: dict, pad: float, now: Optional[float] = None,
               focus: Optional[str] = None) -> dict:
    """Everything the visit card renders, derived from the visit row + its members.

    ``focus`` is the species a filtered feed is about (the species page, Recent filtered
    by species): that species leads — hero, headline, play-button label — and the rest
    are "with …". The partition is stable, so the others keep first-appearance order. A
    focus the visit does not contain, or None, leaves the symmetric strip as it was.
    """
    members = list(visit.get("members") or [])
    species = species_present(members)
    focus_entry = None
    if focus:
        key = focus.strip().lower()
        focus_entry = next((s for s in species if s["common_name"].lower() == key), None)
        if focus_entry is not None:
            species = [focus_entry] + [s for s in species if s is not focus_entry]
    unidentified = [m for m in members
                    if (m.get("common_name") or "").strip().lower() in ("", UNNAMED)]
    zones: list[str] = []
    for source in [visit.get("zones")] + [m.get("zone") for m in members]:
        for z in (source or "").split(","):
            z = z.strip()
            if z and z not in zones:
                zones.append(z)
    start = float(visit.get("start_time") or 0.0)
    end = visit.get("end_time")
    in_progress = end is None
    duration = ((now or time.time()) if in_progress else float(end)) - start
    return {
        **visit,
        "kind": "visit",
        "members": members,
        "species": species,
        # The promoted species (or None) and everyone else, for the focused card layout.
        "focus": focus_entry,
        "others": [s for s in species if s is not focus_entry],
        "unidentified": unidentified,
        "zones_list": zones,
        "duration": max(0.0, duration),
        "in_progress": in_progress,
        "member_count": len(members),
        "window": None if in_progress else play_window(visit, pad),
        "fallback": _fallback(species, members),
    }
