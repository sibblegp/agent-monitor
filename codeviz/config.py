"""User config: where it lives, and the (optional) stored API key.

The API key is only written to disk when the user explicitly opts in, and then
with 0600 permissions. It is never sent back to the frontend — the UI only ever
sees a masked hint.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

APP = "codeviz"


def config_dir() -> Path:
    """Platform-appropriate config directory, created on demand."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / APP
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        path = Path(base) / APP
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path() -> Path:
    return config_dir() / "config.json"


def load() -> dict[str, Any]:
    try:
        return json.loads(_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict[str, Any]) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # best-effort on filesystems without POSIX modes
    tmp.replace(path)


def mask_key(key: str | None) -> str | None:
    """`sk-ant-…a1b2` — enough to recognize, useless if leaked."""
    if not key:
        return None
    tail = key[-4:] if len(key) > 4 else "????"
    return f"sk-ant-…{tail}"


class Settings:
    """In-memory settings, optionally persisted."""

    def __init__(self) -> None:
        stored = load()
        self._session_key: str | None = None
        self._stored_key: str | None = stored.get("api_key")
        self.model: str = stored.get("model", "claude-sonnet-4-6")
        self.ai_enabled: bool = False  # always starts off; opt-in per session
        self.remember_key: bool = bool(stored.get("api_key"))

    @property
    def api_key(self) -> str | None:
        """Env var wins, then a session key, then the stored one."""
        return (
            os.environ.get("ANTHROPIC_API_KEY")
            or self._session_key
            or self._stored_key
        )

    @property
    def key_source(self) -> str | None:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "env"
        if self._session_key:
            return "session"
        if self._stored_key:
            return "stored"
        return None

    def set_key(self, key: str | None, remember: bool = False) -> None:
        key = (key or "").strip() or None
        self._session_key = key
        self.remember_key = remember and key is not None
        if remember and key:
            self._stored_key = key
            data = load()
            data["api_key"] = key
            data["model"] = self.model
            save(data)
        elif not remember:
            self._stored_key = None
            data = load()
            data.pop("api_key", None)
            save(data)

    def set_model(self, model: str) -> None:
        self.model = model
        data = load()
        data["model"] = model
        save(data)

    def public(self) -> dict[str, Any]:
        """What the frontend is allowed to know — never the key itself."""
        return {
            "model": self.model,
            "ai_enabled": self.ai_enabled,
            "has_key": self.api_key is not None,
            "key_hint": mask_key(self.api_key),
            "key_source": self.key_source,
            "remember_key": self.remember_key,
        }
