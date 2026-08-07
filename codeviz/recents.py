"""Recently opened repositories."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import config_dir

LIMIT = 12


def _path() -> Path:
    return config_dir() / "recent.json"


def load() -> list[dict[str, Any]]:
    try:
        data = json.loads(_path().read_text("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict) and entry.get("path")]


def record(path: str, name: str | None = None) -> list[dict[str, Any]]:
    """Move `path` to the front of the recents list and persist."""
    path = str(Path(path).resolve())
    entries = [e for e in load() if e.get("path") != path]
    entries.insert(
        0, {"path": path, "name": name or os.path.basename(path.rstrip(os.sep)) or path}
    )
    entries = entries[:LIMIT]
    try:
        _path().write_text(json.dumps(entries, indent=2), "utf-8")
    except OSError:
        pass
    return entries


def listing() -> list[dict[str, Any]]:
    """Recents, annotated with whether each path still exists."""
    out = []
    for entry in load():
        path = entry["path"]
        out.append({**entry, "exists": os.path.isdir(path)})
    return out
