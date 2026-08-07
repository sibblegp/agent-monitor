"""Running commentary on what the agent is doing.

This is deliberately *not* the same job as `AiAnnotator`. That one describes the
current changeset — every changed symbol, re-derived each time. This one is a
transcript: append-only, and each entry narrates only what is new since the
previous entry, with the last few entries fed back in so the commentary reads as
one continuous thread rather than a series of disconnected observations.

Consequences of that design:

- Work already narrated is tracked by `(node id, body hash)`, so a save that
  changes nothing semantic produces no entry and costs nothing.
- Editing the same symbol again *does* produce a new entry, because its hash
  moved.
- The cadence is coarser than the visual update. Agents write in bursts; one
  entry per burst is readable, one per file-save is not.
"""

from __future__ import annotations

import difflib
import threading
import time
from typing import Any, Callable

from ..model import CHANGED_STATUSES
from .prompts import NARRATE_SYSTEM, NARRATE_TOOL, build_context, build_narration

#: Minimum gap between entries. Bursts inside this window coalesce into one.
MIN_INTERVAL_S = 6.0

#: How many prior entries the model sees, for continuity.
CONTEXT_ENTRIES = 5

#: Cap per entry — enough to see the shape of a burst, not a whole refactor.
MAX_SYMBOLS_PER_ENTRY = 18

MAX_TRANSCRIPT = 300


class Narrator:
    """Maintains an append-only commentary on the changes flowing past."""

    def __init__(self, settings, annotator, on_entry: Callable[[dict[str, Any]], None] | None = None):
        self.settings = settings
        #: Reused for its client construction, pricing, and usage meter.
        self._annotator = annotator
        self.on_entry = on_entry

        self.entries: list[dict[str, Any]] = []
        self.enabled = False
        self.last_error: str | None = None

        self._seen: dict[str, str] = {}  # node id -> body hash already narrated
        #: node id -> the symbol's source as of the last entry that mentioned it.
        #: Lets a follow-up edit be diffed against what was last narrated rather
        #: than against HEAD — without it, a symbol in a new uncommitted file
        #: diffs as "added" with its whole body every time, so the transcript
        #: keeps re-introducing code it already described.
        self._prev_source: dict[str, str] = {}
        self._pending_source: dict[str, str] = {}
        self._lock = threading.Lock()
        self._inflight = False
        self._timer: threading.Timer | None = None
        self._last_at = 0.0
        self._baseline_done = False

    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "busy": self._inflight,
            "count": len(self.entries),
            "error": self.last_error,
        }

    def digest(self, limit: int = 5) -> str:
        """One-line recap of the last few entries, for logs and tooltips."""
        if not self.entries:
            return "nothing narrated yet"
        return " → ".join(e["headline"] for e in reversed(self.entries[:limit]))

    def reset(self) -> None:
        """Clear the transcript — a new repo or mode is a new story."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self.entries = []
            self._seen = {}
            self._prev_source = {}
            self._pending_source = {}
            self._baseline_done = False
            self._last_at = 0.0

    def set_enabled(self, value: bool) -> None:
        self.enabled = bool(value)

    # ------------------------------------------------------------------

    def _fresh_changes(self, changeset, symbol_hashes: dict[str, str]) -> list:
        """Changed symbols we haven't narrated yet, at their current content."""
        out = []
        for change in changeset.symbols:
            if change.status not in CHANGED_STATUSES:
                continue
            node_id = change.node_id
            # Removals have no body hash; key them by status so a delete is
            # narrated once rather than on every subsequent rescan.
            fingerprint = symbol_hashes.get(node_id) or f"gone:{change.status}"
            if self._seen.get(node_id) == fingerprint:
                continue
            out.append((change, fingerprint))
        return out

    def observe(self, target, changeset, langs, files, read_side, symbol_hashes) -> None:
        """Called after every rescan. Decides whether an entry is warranted."""
        if not self.enabled or not self.settings.ai_enabled:
            return

        # Mark the baseline on the *first observation*, not the first one that
        # happens to carry changes. Deferring it meant that opening a clean repo
        # left the baseline unset, so the agent's first real edit was swallowed
        # as "pre-existing" and never narrated.
        first_observation = not self._baseline_done
        self._baseline_done = True

        fresh = self._fresh_changes(changeset, symbol_hashes)
        if not fresh:
            return

        if first_observation:
            # Whatever was already uncommitted when the repo was opened is the
            # starting point, not news. Record it silently so the transcript
            # begins when the agent does.
            for change, fingerprint in fresh:
                self._seen[change.node_id] = fingerprint
            return

        with self._lock:
            if self._inflight:
                return
            elapsed = time.monotonic() - self._last_at
            if elapsed < MIN_INTERVAL_S:
                # Trailing debounce: let the burst finish, then narrate once.
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = threading.Timer(
                    MIN_INTERVAL_S - elapsed,
                    lambda: self.observe(target, changeset, langs, files, read_side, symbol_hashes),
                )
                self._timer.daemon = True
                self._timer.start()
                return
            self._inflight = True

        thread = threading.Thread(
            target=self._run,
            args=(target, changeset, langs, files, read_side, fresh),
            daemon=True,
        )
        thread.start()

    def _run(self, target, changeset, langs, files, read_side, fresh) -> None:
        try:
            entry = self._narrate(target, changeset, langs, files, read_side, fresh)
            if entry is None:
                return
            # Only mark work as narrated once an entry actually succeeded, so a
            # failed call doesn't silently swallow a burst.
            for change, fingerprint in fresh:
                self._seen[change.node_id] = fingerprint
            # Same reasoning as `_seen`: only advance the baseline the next diff
            # is taken against once the entry describing it actually landed.
            self._prev_source.update(getattr(self, "_pending_source", {}) or {})
            self._pending_source = {}
            self.entries.insert(0, entry)
            del self.entries[MAX_TRANSCRIPT:]
            self.last_error = None
            if self.on_entry:
                self.on_entry(entry)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._last_at = time.monotonic()
            self._inflight = False

    def _narrate(self, target, changeset, langs, files, read_side, fresh) -> dict[str, Any] | None:
        import anthropic  # noqa: PLC0415

        from .client import _slice_diff  # noqa: PLC0415

        client = self._annotator._client()

        entries = []
        current_source: dict[str, str] = {}
        for change, _fingerprint in fresh[:MAX_SYMBOLS_PER_ENTRY]:
            file_change = next((f for f in changeset.files if f.path == change.path), None)
            old_text = read_side(file_change.old_ref if file_change else None, change.path)
            new_text = read_side(file_change.new_ref if file_change else None, change.path)

            body = _symbol_source(new_text, change)
            if body:
                current_source[change.node_id] = body

            previous = self._prev_source.get(change.node_id)
            if previous is not None and body is not None:
                # Already narrated once — show only what moved since then, so a
                # follow-up edit reads as a follow-up rather than a reveal.
                status = "changed again since your last entry"
                diff = "\n".join(
                    difflib.unified_diff(
                        previous.splitlines(),
                        body.splitlines(),
                        lineterm="",
                        n=2,
                        fromfile="as you last described it",
                        tofile="now",
                    )
                ) or "(no textual change)"
            else:
                status = change.status
                diff = _slice_diff(old_text, new_text, change)

            entries.append(
                {
                    "id": change.node_id,
                    "status": status,
                    "kind": change.kind,
                    "qualname": change.qualname,
                    "path": change.path,
                    "diff": diff,
                }
            )
        self._pending_source = current_source

        overflow = max(0, len(fresh) - MAX_SYMBOLS_PER_ENTRY)
        # Oldest first, so the model reads its own commentary in order.
        recent = [
            {"headline": e["headline"], "detail": e["detail"]}
            for e in reversed(self.entries[:CONTEXT_ENTRIES])
        ]
        body = build_narration(recent, entries)
        if overflow:
            body += f"\n\n(and {overflow} further changed symbols in the same burst)"

        response = client.messages.create(
            model=self.settings.model,
            max_tokens=1024,
            system=[
                {"type": "text", "text": NARRATE_SYSTEM},
                {
                    "type": "text",
                    "text": build_context(target.to_dict()["name"], langs, files),
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            tools=[NARRATE_TOOL],
            tool_choice={"type": "tool", "name": "narrate_step"},
            messages=[{"role": "user", "content": body}],
        )

        self._annotator._record_usage(response)

        for block in response.content:
            if block.type == "tool_use" and block.name == "narrate_step":
                data = dict(block.input)
                return {
                    "at": time.time(),
                    "headline": data.get("headline", ""),
                    "detail": data.get("detail", ""),
                    "phase": data.get("phase", "unclear"),
                    "symbols": [e["id"] for e in entries],
                    "paths": sorted({e["path"] for e in entries}),
                    "count": len(fresh),
                }
        return None


def _symbol_source(text: str | None, change) -> str | None:
    """The symbol's own lines out of a file's full text."""
    if not text or change.line is None:
        return None
    lines = text.splitlines()
    start = max(0, change.line - 1)
    span = max(change.added, change.removed, 1)
    return "\n".join(lines[start : min(len(lines), start + span)])
