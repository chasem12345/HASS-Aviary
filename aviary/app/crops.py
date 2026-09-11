"""Best-crop thumbnails returned by the identification service.

The service reports which crop best backed its answer (the same frame the learning
embedding comes from) as a small JPEG. Stored here, keyed by the Frigate event id, and
shown on cards in place of the wide camera's media — which matters most when the footage
that was classified is not the event's own media at all (a zoomed PTZ recording).

Since aviary-id 0.10.0 an event can carry several birds ("subjects"); each has its own
crop. Subject 0 (the primary) keeps the historical ``{event_id}.jpg`` name so nothing
that already exists moves; the others are ``{event_id}-{idx}.jpg``.

Files, not database blobs: they are served as images, a missing file degrades to the
existing Frigate thumbnail via the template fallback, and deleting one can never corrupt
anything. Callers that delete detections are responsible for calling ``remove``.
"""

from __future__ import annotations

import base64
import glob
import logging
import os
from typing import Optional

log = logging.getLogger("aviary.crops")

_dir: Optional[str] = None

# A stored crop should be tens of KB; anything bigger than this is not the thumbnail
# contract and is refused rather than written.
_MAX_BYTES = 512 * 1024


def configure(data_dir: str) -> None:
    global _dir
    _dir = os.path.join(data_dir, "crops")
    os.makedirs(_dir, exist_ok=True)


def _safe(event_id: str) -> Optional[str]:
    """The event id whitelisted to the characters Frigate actually uses (digits, dots,
    dashes, alphanumerics) — it becomes a filename, so it is never trusted raw."""
    if not event_id:
        return None
    safe = "".join(c for c in str(event_id) if c.isalnum() or c in "._-")
    if not safe or safe != str(event_id):
        return None
    return safe


def basename(event_id: str, idx: int = 0) -> Optional[str]:
    """The stored filename for an event's crop (subject ``idx``), or None for a bad id."""
    safe = _safe(event_id)
    if safe is None:
        return None
    return f"{safe}.jpg" if not idx else f"{safe}-{int(idx)}.jpg"


def _path(event_id: str, idx: int = 0) -> Optional[str]:
    """Filesystem path for an event's crop, or None for an unusable id."""
    name = basename(event_id, idx)
    if _dir is None or name is None:
        return None
    return os.path.join(_dir, name)


def save(event_id: str, b64: Optional[str], idx: int = 0) -> bool:
    """Store a base64 JPEG for this event (and subject). Best-effort: False, never an exception."""
    path = _path(event_id, idx)
    if not path or not b64:
        return False
    try:
        data = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        log.debug("Discarding an undecodable crop for %s.", event_id)
        return False
    if not data or len(data) > _MAX_BYTES:
        log.debug("Discarding a crop of %d bytes for %s.", len(data), event_id)
        return False
    try:
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("Could not store the crop for %s: %s", event_id, exc)
        return False


def exists(event_id: str, idx: int = 0) -> bool:
    """Template helper: whether a card has a stored crop to show."""
    path = _path(event_id, idx)
    return bool(path) and os.path.isfile(path)


def path_if_exists(event_id: str, idx: int = 0) -> Optional[str]:
    path = _path(event_id, idx)
    return path if path and os.path.isfile(path) else None


def remove(event_id: str) -> None:
    """Delete an event's crops — the primary's and every subject's. Best-effort."""
    safe = _safe(event_id)
    if _dir is None or safe is None:
        return
    paths = [os.path.join(_dir, f"{safe}.jpg")] + glob.glob(os.path.join(_dir, f"{safe}-*.jpg"))
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.debug("Could not remove the crop %s: %s", path, exc)


def list_primary_refs() -> list[str]:
    """Event ids that have a PRIMARY crop on disk (``{event}.jpg``; subject crops carry a
    ``-idx`` suffix and are skipped). For the one-time has_crop catch-up."""
    if _dir is None or not os.path.isdir(_dir):
        return []
    refs = []
    for name in os.listdir(_dir):
        if not name.endswith(".jpg") or name.endswith(".part"):
            continue
        stem = name[:-4]
        # A subject crop is "<event>-<n>.jpg"; Frigate ids themselves contain a dash
        # ("1718123456.123456-abc123"), so only a purely numeric suffix marks a subject.
        head, sep, tail = stem.rpartition("-")
        if sep and tail.isdigit() and head:
            continue
        refs.append(stem)
    return refs


def remove_subjects(event_id: str) -> None:
    """Delete only the OTHER birds' crops (before a re-identify replaces them)."""
    safe = _safe(event_id)
    if _dir is None or safe is None:
        return
    for path in glob.glob(os.path.join(_dir, f"{safe}-*.jpg")):
        try:
            os.remove(path)
        except OSError:
            pass
