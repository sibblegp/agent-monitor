"""Directory listing for the in-app "Open…" browser (browser-mode fallback).

Electron uses the native OS dialog instead; this exists so the browser shell
keeps full parity, and so the app works over SSH where no dialog can appear.
"""

from __future__ import annotations

import os
import string
import sys
from pathlib import Path
from typing import Any


def _drives() -> list[str]:
    """Windows drive roots; empty everywhere else."""
    if sys.platform != "win32":
        return []
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]


def home() -> str:
    """The user's real home directory.

    Inside a strictly-confined snap, snapd repoints $HOME at the snap's own
    private data directory — so `Path.home()` would open the picker on an empty
    folder the user has never heard of, with none of their repositories in it.
    `SNAP_REAL_HOME` is snapd's pointer back to the actual home, and the `home`
    interface is what makes it readable. Unset everywhere else, so this is a
    no-op outside a snap.
    """
    return os.environ.get("SNAP_REAL_HOME") or str(Path.home())


def listing(path: str | None = None, show_hidden: bool = False) -> dict[str, Any]:
    """List sub-directories of `path`, flagging which ones are git repos."""
    root = Path(home())
    target = Path(path).expanduser() if path else root
    try:
        target = target.resolve()
    except OSError:
        target = root

    if not target.is_dir():
        target = target.parent if target.parent.is_dir() else root

    entries: list[dict[str, Any]] = []
    error: str | None = None
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue  # a single unreadable entry must not blank the pane
                if not show_hidden and entry.name.startswith("."):
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "path": str(Path(entry.path)),
                        "is_repo": os.path.isdir(os.path.join(entry.path, ".git"))
                        or os.path.isfile(os.path.join(entry.path, ".git")),
                    }
                )
    except PermissionError:
        error = "Permission denied"
    except OSError as exc:
        error = str(exc)

    entries.sort(key=lambda e: (not e["is_repo"], e["name"].lower()))

    parent = str(target.parent) if target.parent != target else None
    return {
        "path": str(target),
        "parent": parent,
        "home": home(),
        "drives": _drives(),
        "is_repo": os.path.exists(os.path.join(target, ".git")),
        "entries": entries,
        "error": error,
    }
