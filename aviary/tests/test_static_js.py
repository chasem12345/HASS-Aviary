"""Every shipped script must parse.

The browser discards a whole file on a SyntaxError, so one bad literal silently disables
every function that file defines — 0.32.0 shipped a settings page whose status boxes sat on
"Checking…" forever for exactly that reason. No node in the toolchain, so parse with esprima.
"""

from __future__ import annotations

from pathlib import Path

import esprima
import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
# Vendored bundles are not ours to police and are slow to parse.
VENDORED = ("chart", "hls")
SCRIPTS = sorted(
    p for p in STATIC.glob("*.js") if not p.name.lower().startswith(VENDORED)
)


def test_scripts_found():
    assert SCRIPTS, f"no scripts under {STATIC}"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_script_parses(path: Path):
    try:
        esprima.parseScript(path.read_text(encoding="utf-8"), tolerant=False)
    except esprima.Error as err:  # pragma: no cover - only on a real regression
        pytest.fail(f"{path.name}: {err}")
