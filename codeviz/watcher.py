"""Filesystem watching with debouncing and .gitignore filtering.

An agent writes in bursts — a dozen files in a few hundred milliseconds — so
raw filesystem events are coalesced into one batch before anything is
re-analyzed. Ignored paths are filtered through `git check-ignore` rather than
by reimplementing .gitignore semantics, and `.git/` internals are dropped early
so an index write doesn't look like source churn.

Falls back to mtime polling when watchdog isn't importable, so the tool still
works rather than silently going static.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from . import gitutil
from .model import RepoTarget

DEBOUNCE_S = 0.22
MAX_BATCH_WAIT_S = 1.5
POLL_INTERVAL_S = 1.0

try:  # pragma: no cover - import guard
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    HAVE_WATCHDOG = True
except Exception:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]
    HAVE_WATCHDOG = False


def _should_ignore_fast(relpath: str) -> bool:
    """Cheap structural filter applied before the git check."""
    parts = relpath.split("/")
    if any(part in gitutil.VENDORED for part in parts):
        return True
    name = parts[-1]
    if name.endswith((".pyc", ".pyo", ".swp", ".swx", "~")):
        return True
    # Editors write `.#foo` / `4913` style temp files; ignore the obvious ones.
    if name.startswith((".#", "#")) or name.endswith(".tmp"):
        return True
    return False


class _Batcher:
    """Collects paths and fires `flush` once the burst goes quiet."""

    def __init__(self, on_batch: Callable[[set[str]], None]) -> None:
        self._on_batch = on_batch
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._first_at: float | None = None
        self._stopped = False

    def add(self, paths: Iterable[str]) -> None:
        with self._lock:
            if self._stopped:
                return
            before = len(self._pending)
            self._pending.update(paths)
            if not self._pending:
                return
            if before == 0:
                self._first_at = time.monotonic()

            # Cap the total wait so a continuous stream of writes still
            # produces updates instead of being debounced forever.
            elapsed = time.monotonic() - (self._first_at or time.monotonic())
            if elapsed >= MAX_BATCH_WAIT_S:
                self._fire_locked()
                return

            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_S, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._fire_locked()

    def _fire_locked(self) -> None:
        batch = self._pending
        self._pending = set()
        self._first_at = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not batch or self._stopped:
            return
        # Run the callback outside the lock so a slow rescan can't block events.
        threading.Thread(target=self._on_batch, args=(batch,), daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


#: Only events that can actually change file *content*.
#:
#: This whitelist is load-bearing. watchdog also emits `opened` and
#: `closed_no_write` events, so reacting to every event type created a feedback
#: loop: a rescan reads the repo → inotify reports the reads → that triggers
#: another rescan → forever. It pegged a core and pushed ~4 snapshots/second
#: with nothing changing on disk.
_CONTENT_EVENTS = frozenset({"modified", "created", "deleted", "moved"})


class _Handler(FileSystemEventHandler):  # type: ignore[misc]
    def __init__(self, watcher: "RepoWatcher") -> None:
        self.watcher = watcher

    def on_any_event(self, event) -> None:  # pragma: no cover - event driven
        if getattr(event, "is_directory", False):
            return
        if getattr(event, "event_type", None) not in _CONTENT_EVENTS:
            return
        paths = [getattr(event, "src_path", None), getattr(event, "dest_path", None)]
        self.watcher.feed([p for p in paths if p])


class RepoWatcher:
    """Watches a repo subtree and calls `on_change(paths)` for each burst."""

    def __init__(self, target: RepoTarget, on_change: Callable[[set[str]], None]) -> None:
        self.target = target
        self.on_change = on_change
        self.batcher = _Batcher(self._handle_batch)
        self._observer = None
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.mode = "watchdog" if HAVE_WATCHDOG else "poll"

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        root = self.target.abs_scope
        if not os.path.isdir(root):
            return
        if HAVE_WATCHDOG:
            try:
                self._observer = Observer()
                self._observer.schedule(_Handler(self), root, recursive=True)
                self._observer.start()
                return
            except Exception:
                # Inotify limits, unsupported filesystems, etc. — degrade to
                # polling rather than losing live updates entirely.
                self._observer = None
                self.mode = "poll"
        self._start_polling()

    def stop(self) -> None:
        self._stop.set()
        self.batcher.stop()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
            self._observer = None

    # -- event intake ----------------------------------------------------

    def feed(self, abs_paths: Iterable[str]) -> None:
        root = self.target.root
        rels: list[str] = []
        for raw in abs_paths:
            try:
                rel = os.path.relpath(raw, root).replace(os.sep, "/")
            except ValueError:
                continue
            if rel.startswith(".."):
                continue
            if rel == ".git" or rel.startswith(".git/"):
                continue
            if _should_ignore_fast(rel):
                continue
            if not self.target.in_scope(rel):
                continue
            rels.append(rel)
        if rels:
            self.batcher.add(rels)

    def _handle_batch(self, paths: set[str]) -> None:
        if self._stop.is_set():
            return
        # One batched check-ignore call for the whole burst.
        if self.target.is_git:
            ignored = gitutil.check_ignore(self.target.root, sorted(paths))
            paths = {p for p in paths if p not in ignored}
        if paths:
            try:
                self.on_change(paths)
            except Exception:
                pass  # a failed rescan must never kill the watcher thread

    # -- polling fallback ------------------------------------------------

    def _start_polling(self) -> None:
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_loop(self) -> None:  # pragma: no cover - timing dependent
        seen: dict[str, tuple[int, int]] = {}
        first = True
        while not self._stop.is_set():
            current: dict[str, tuple[int, int]] = {}
            root = Path(self.target.root)
            for rel in gitutil.list_files(self.target):
                if _should_ignore_fast(rel):
                    continue
                try:
                    stat = (root / rel).stat()
                except OSError:
                    continue
                current[rel] = (stat.st_mtime_ns, stat.st_size)

            if not first:
                changed = {
                    rel
                    for rel, sig in current.items()
                    if seen.get(rel) != sig
                } | (seen.keys() - current.keys())
                if changed:
                    self.batcher.add(changed)
            seen = current
            first = False
            self._stop.wait(POLL_INTERVAL_S)
