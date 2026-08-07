"""Behaviour the packaged builds depend on.

Plain asserts — run with `pytest`, or directly with `python tests/run_tests.py`.

These cover the seams between the app and the things that wrap it. They are
cheap, and the failure they guard against is expensive: it only shows up in a
built artifact, where the feedback loop is a ten-minute container build rather
than a test run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_monitor import config, fsbrowse  # noqa: E402


# ── snap confinement ──────────────────────────────────────────────────
#
# snapd repoints $HOME at the snap's own private data directory. Everything
# below is about that one fact.


def test_home_prefers_snap_real_home(monkeypatch):
    """The picker must open on the user's home, not the snap's private one."""
    monkeypatch.setenv("SNAP_REAL_HOME", "/home/someone")
    assert fsbrowse.home() == "/home/someone"


def test_home_falls_back_when_not_confined(monkeypatch):
    """Unset everywhere except inside a snap, so this must stay a no-op."""
    monkeypatch.delenv("SNAP_REAL_HOME", raising=False)
    assert fsbrowse.home() == str(Path.home())


def test_listing_defaults_to_the_real_home(monkeypatch, tmp_path):
    """A bare listing() call starts at the real home too, not just home()."""
    (tmp_path / "a-project").mkdir()
    monkeypatch.setenv("SNAP_REAL_HOME", str(tmp_path))
    result = fsbrowse.listing()
    assert result["path"] == str(tmp_path)
    assert [e["name"] for e in result["entries"]] == ["a-project"]


def test_config_stays_in_the_confined_home(monkeypatch, tmp_path):
    """The mirror image: config must *not* follow SNAP_REAL_HOME.

    $HOME inside the snap is the one place that is actually writable, and the
    real home is read-only-ish by policy. Writing config to SNAP_REAL_HOME
    would fail on a machine where the user has no home interface connected.
    """
    monkeypatch.setenv("SNAP_REAL_HOME", "/home/someone")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_dir() == tmp_path / "agent-monitor"
