"""Anthropic client for changeset annotation.

Design constraints that shaped this:

- **Opt-in.** Nothing here runs unless the user turns AI insights on.
- **One request per changeset**, never per symbol.
- **Results cached by (qualname, body_hash)**, so an unchanged symbol is never
  re-analyzed even as the changeset around it churns.
- **Never load-bearing.** Any failure is reported and swallowed; the
  visualization is fully functional without it.
"""

from __future__ import annotations

import difflib
import threading
import time
from typing import Any, Callable

from ..model import CHANGED_STATUSES, ChangeSet, RepoTarget
from .prompts import ANNOTATE_TOOL, SYSTEM, build_changes, build_context

#: Per-million-token list prices, for the cost meter. Kept small and explicit;
#: unknown models simply report token counts without a dollar figure.
PRICING = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
}

MAX_SYMBOLS_PER_CALL = 40
MAX_DIFF_LINES = 60
MAX_REQUESTS_PER_SESSION = 200


class AiUnavailable(RuntimeError):
    """The AI layer can't run — missing SDK, missing key, or disabled."""


def _unified(old: str | None, new: str | None, label: str) -> str:
    old_lines = (old or "").splitlines()
    new_lines = (new or "").splitlines()
    diff = list(
        difflib.unified_diff(old_lines, new_lines, lineterm="", n=2, fromfile="before", tofile="after")
    )
    if len(diff) > MAX_DIFF_LINES:
        diff = diff[:MAX_DIFF_LINES] + [f"... (truncated, {len(diff) - MAX_DIFF_LINES} more lines)"]
    return "\n".join(diff)


class AiAnnotator:
    """Turns a ChangeSet into summaries, risk flags, themes, and a review note."""

    def __init__(self, settings, on_result: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.settings = settings
        self.on_result = on_result
        self.usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0, "cost_usd": 0.0}
        self.last_error: str | None = None
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._inflight = False
        self._disabled_reason: str | None = None

    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._disabled_reason is None and self.settings.api_key is not None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.ai_enabled,
            "available": self.available,
            "busy": self._inflight,
            "error": self.last_error,
            "disabled_reason": self._disabled_reason,
            "usage": self.usage,
            "model": self.settings.model,
        }

    def _client(self):
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise AiUnavailable(
                "the `anthropic` package isn't installed — pip install 'agent-monitor[ai]'"
            ) from exc
        key = self.settings.api_key
        if not key:
            raise AiUnavailable("no API key configured")
        return anthropic.Anthropic(api_key=key)

    # ------------------------------------------------------------------

    def annotate_async(
        self,
        target: RepoTarget,
        changeset: ChangeSet,
        langs: dict[str, int],
        files: list[str],
        read_side: Callable[[str | None, str], str | None],
        symbol_hashes: dict[str, str],
    ) -> None:
        """Kick off annotation on a worker thread. Safe to call on every update."""
        if not self.settings.ai_enabled or self._inflight:
            return
        thread = threading.Thread(
            target=self._run,
            args=(target, changeset, langs, files, read_side, symbol_hashes),
            daemon=True,
        )
        thread.start()

    def _run(self, target, changeset, langs, files, read_side, symbol_hashes) -> None:
        with self._lock:
            if self._inflight:
                return
            self._inflight = True
        try:
            result = self.annotate(target, changeset, langs, files, read_side, symbol_hashes)
            if result and self.on_result:
                self.on_result(result)
        except AiUnavailable as exc:
            self.last_error = str(exc)
            self._disabled_reason = str(exc)
        except Exception as exc:  # never let the AI layer break the app
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._inflight = False

    # ------------------------------------------------------------------

    def annotate(
        self, target, changeset, langs, files, read_side, symbol_hashes
    ) -> dict[str, Any] | None:
        changed = [s for s in changeset.symbols if s.status in CHANGED_STATUSES]
        if not changed:
            return None

        if self.usage["requests"] >= MAX_REQUESTS_PER_SESSION:
            raise AiUnavailable(
                f"per-session request cap ({MAX_REQUESTS_PER_SESSION}) reached"
            )

        # Split into what we already know and what needs a request.
        pending = []
        cached_summaries: list[dict[str, Any]] = []
        cached_risk: list[dict[str, Any]] = []
        for change in changed:
            key = (change.qualname, symbol_hashes.get(change.node_id, change.status))
            hit = self._cache.get(key)
            if hit is not None:
                if hit.get("summary"):
                    cached_summaries.append({"id": change.node_id, "text": hit["summary"]})
                if hit.get("risk"):
                    cached_risk.append({"id": change.node_id, **hit["risk"]})
            else:
                pending.append((change, key))

        if not pending:
            # Everything was already analyzed; still return so themes/notes show.
            return {
                "summaries": cached_summaries,
                "risk": cached_risk,
                "themes": self._last_themes,
                "review_note": self._last_note,
                "usage": self.usage,
                "cached": True,
            }

        entries = []
        for change, _key in pending[:MAX_SYMBOLS_PER_CALL]:
            file_change = next((f for f in changeset.files if f.path == change.path), None)
            old_text = read_side(file_change.old_ref if file_change else None, change.path)
            new_text = read_side(file_change.new_ref if file_change else None, change.path)
            entries.append(
                {
                    "id": change.node_id,
                    "status": change.status,
                    "kind": change.kind,
                    "qualname": change.qualname,
                    "path": change.path,
                    "diff": _slice_diff(old_text, new_text, change),
                }
            )

        truncated = max(0, len(pending) - MAX_SYMBOLS_PER_CALL)
        result = self._request(target, langs, files, entries, truncated)

        # Cache per symbol so the next changeset reuses this work.
        by_id = {s["id"]: s.get("text") for s in result.get("summaries", [])}
        risk_by_id = {r["id"]: r for r in result.get("risk", [])}
        for change, key in pending[:MAX_SYMBOLS_PER_CALL]:
            self._cache[key] = {
                "summary": by_id.get(change.node_id),
                "risk": {
                    "level": risk_by_id.get(change.node_id, {}).get("level"),
                    "reason": risk_by_id.get(change.node_id, {}).get("reason"),
                }
                if change.node_id in risk_by_id
                else None,
            }

        result["summaries"] = cached_summaries + result.get("summaries", [])
        result["risk"] = cached_risk + result.get("risk", [])
        result["usage"] = self.usage
        if truncated:
            result["truncated"] = truncated
        self._last_themes = result.get("themes", [])
        self._last_note = result.get("review_note", "")
        return result

    _last_themes: list[dict[str, Any]] = []
    _last_note: str = ""

    # ------------------------------------------------------------------

    def _request(self, target, langs, files, entries, truncated) -> dict[str, Any]:
        import anthropic  # noqa: PLC0415

        client = self._client()
        context = build_context(target.to_dict()["name"], langs, files)
        changes = build_changes(entries)
        if truncated:
            changes += f"\n\n(Additionally {truncated} more changed symbols were omitted.)"

        started = time.perf_counter()
        try:
            response = client.messages.create(
                model=self.settings.model,
                max_tokens=4096,
                system=[
                    {"type": "text", "text": SYSTEM},
                    # The repo listing is stable between calls, so cache it and
                    # let only the diff vary.
                    {
                        "type": "text",
                        "text": context,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                # Latency matters more than depth here — this annotates a diff
                # the user is already looking at. Sonnet 4.6 defaults to `high`
                # effort, which is explicitly not what we want.
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
                tools=[ANNOTATE_TOOL],
                tool_choice={"type": "tool", "name": "annotate_changeset"},
                messages=[{"role": "user", "content": f"Changed symbols:\n\n{changes}"}],
            )
        except anthropic.RateLimitError as exc:
            raise AiUnavailable(f"rate limited — {exc}") from exc
        except anthropic.AuthenticationError as exc:
            raise AiUnavailable("API key rejected") from exc
        except anthropic.APIStatusError as exc:
            raise AiUnavailable(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AiUnavailable(f"could not reach the API: {exc}") from exc

        self._record_usage(response)
        self.last_error = None

        for block in response.content:
            if block.type == "tool_use" and block.name == "annotate_changeset":
                out = dict(block.input)
                out["latency_ms"] = int((time.perf_counter() - started) * 1000)
                return out

        raise AiUnavailable("model did not return the expected tool call")

    def _record_usage(self, response) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.usage["requests"] += 1
        self.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        self.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        price = PRICING.get(self.settings.model)
        if price:
            self.usage["cost_usd"] = round(
                self.usage["input_tokens"] / 1_000_000 * price[0]
                + self.usage["output_tokens"] / 1_000_000 * price[1],
                4,
            )


def _slice_diff(old_text: str | None, new_text: str | None, change) -> str:
    """A unified diff limited to the symbol's own lines.

    The span is taken from the symbol's measured line delta rather than a fixed
    window — otherwise an added symbol's "diff" runs past its own closing line
    and into whatever is defined next, which reads as though far more changed
    than actually did.
    """
    if change.status == "added":
        return _window(new_text, change.line, change.added, "+")
    if change.status == "removed":
        return _window(old_text, change.line, change.removed, "-")
    span = max(change.added, change.removed, 12)
    return _unified(
        _window(old_text, change.line, span, ""),
        _window(new_text, change.line, span, ""),
        change.qualname,
    )


def _window(text: str | None, line: int | None, span: int, marker: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if line is None:
        return "\n".join(lines[:MAX_DIFF_LINES])
    start = max(0, line - 1)
    length = min(max(1, span or MAX_DIFF_LINES), MAX_DIFF_LINES)
    body = lines[start : min(len(lines), start + length)]
    return "\n".join(f"{marker}{ln}" if marker else ln for ln in body)
