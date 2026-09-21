"""The bundled notification blueprint's templates, rendered with plain Jinja2.

Home Assistant's template engine is Jinja2 with extra filters; the expressions checked
here use only standard ones (``default``, ``int``, ``trim``), so this stays an honest
approximation of what HA renders.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import jinja2
import yaml

BLUEPRINT = os.path.join(os.path.dirname(__file__), "..", "blueprints",
                         "new_species_notification.yaml")


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Home Assistant's ``!input`` tag (as the input's name)."""


_Loader.add_constructor("!input", lambda loader, node: loader.construct_scalar(node))


def render(name: str, data: dict, **inputs) -> str:
    with open(BLUEPRINT, encoding="utf-8") as f:
        bp = yaml.load(f, Loader=_Loader)
    env = jinja2.Environment()
    ctx = {"trigger": SimpleNamespace(event=SimpleNamespace(data=data)),
           "verb": data.get("verb", "seen"), "seen_image": "event_preview.gif",
           "frigate_proxy_base": "/api/frigate/notifications"}
    ctx.update(inputs)
    return env.from_string(bp["variables"][name]).render(**ctx).strip()


def test_tracked_bird_notification_uses_the_live_frigate_preview():
    data = {"verb": "seen", "source_ref": "ev1", "image": "/local/aviary/wren.jpg",
            "subject_idx": None}
    assert render("image", data) == "/api/frigate/notifications/ev1/event_preview.gif"
    assert render("image", data, seen_image="aviary") == "/local/aviary/wren.jpg"


def test_other_bird_notification_uses_its_own_crop_not_the_tracked_birds_preview():
    """'New species! Carolina Wren' over a GIF of the cardinal it sat beside was worse
    than no picture; an other bird (subject_idx > 0) always shows its own staged crop."""
    data = {"verb": "seen", "source_ref": "ev1", "image": "/local/aviary/wren.jpg",
            "subject_idx": 1}
    assert render("image", data) == "/local/aviary/wren.jpg"
    # Payloads from add-ons before 0.35.0 carry no subject_idx at all.
    del data["subject_idx"]
    assert render("image", data) == "/api/frigate/notifications/ev1/event_preview.gif"


def test_heard_bird_uses_the_staged_image():
    data = {"verb": "heard", "source_ref": "n1", "image": "/local/aviary/wren.jpg"}
    assert render("image", data) == "/local/aviary/wren.jpg"
