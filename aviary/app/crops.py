"""Best-crop thumbnails returned by the identification service.

The service reports which crop best backed its answer (the same frame the learning
embedding comes from) as a small JPEG. Stored here, keyed by the Frigate event id, and
shown on cards in place of the wide camera's media — which matters most when the footage
that was classified is not the event's own media at all (a zoomed PTZ recording).

Files, not database blobs: they are served as images, a missing file degrades to the
existing Frigate thumbnail via the template fallback, and deleting one can never corrupt
anything. Callers that delete detections are responsible for calling ``remove``.
"""

from __future__ import annotations

import base64
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


def _path(event_id: str) -> Optional[str]:
    """Filesystem path for an event's crop, or None for an unusable id.

    The event id becomes a filename, so it is whitelisted to the characters Frigate
    actually uses (digits, dots, dashes, alphanumerics) — never trusted raw.
    """
    if _dir is None or not event_id:
        return None
    safe = "".join(c for c in str(event_id) if c.isalnum() or c in "._-")
    if not safe or safe != str(event_id):
        return None
    return os.path.join(_dir, f"{safe}.jpg")


def save(event_id: str, b64: Optional[str]) -> bool:
    """Store a base64 JPEG for this event. Best-effort: False, never an exception."""
    path = _path(event_id)
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


def exists(event_id: str) -> bool:
    """Template helper: whether a card has a stored crop to show."""
    path = _path(event_id)
    return bool(path) and os.path.isfile(path)


def path_if_exists(event_id: str) -> Optional[str]:
    path = _path(event_id)
    return path if path and os.path.isfile(path) else None


def remove(event_id: str) -> None:
    """Delete an event's crop. Best-effort — a leftover file only costs disk."""
    path = _path(event_id)
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.debug("Could not remove the crop for %s: %s", event_id, exc)
