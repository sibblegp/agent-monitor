"""Anthropic client for changeset annotation.

Design constraints that shaped this:

- **Opt-in.** Nothing here runs unless the user turns AI insights on.
- **Batched**, never one request per symbol: a changeset goes out in blocks of
  `SYMBOLS_PER_BATCH`, sized so the model answers for every symbol in the block.
- **Results cached by (node id, body_hash)**, so an unchanged symbol is never
  re-analyzed even as the changeset around it churns — but only once the model
  has actually described it.
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

MAX_SYMBOLS_PER_CALL = 60
MAX_DIFF_LINES = 60
MAX_REQUESTS_PER_SESSION = 200
#: Symbols described per request. Large batches come back partly unanswered.
SYMBOLS_PER_BATCH = 15
#: How many times to re-ask about a symbol the model declined to describe.
MAX_RETRIES_PER_SYMBOL = 2
#: Themes are drawn as labelled hulls; past a handful they just overlap.
MAX_THEMES = 5


def _resolver(refs: dict[str, Any]):
    """Map an id the model returned back to a real node id.

    Exact token first. Failing that, fall back to matching whatever it said
    against the node ids themselves — it sometimes answers with the qualname or
    a prefix-stripped node id, and a recognisable answer shouldn't be thrown
    away over its formatting.
    """
    by_node = {change.node_id: change.node_id for change in refs.values()}
    loose = {}
    for node_id in by_node:
        _, _, tail = node_id.partition(":")
        loose[tail] = node_id
        loose[node_id.rpartition("::")[2] or node_id] = node_id

    def resolve(raw: Any) -> str | None:
        if not isinstance(raw, str):
            return None
        ref = refs.get(raw.strip())
        if ref is not None:
            return ref.node_id
        text = raw.strip()
        return by_node.get(text) or loose.get(text)

    return resolve


def _merge_themes(themes: list[dict]) -> list[dict]:
    """Fold per-batch themes into one set, unioning members of same-named ones.

    Each batch names its own themes, so a large changeset yields a dozen
    near-duplicates. They're drawn as labelled hulls behind their members, and a
    dozen overlapping labels is worse than none — keep the biggest few.
    """
    merged: dict[str, dict] = {}
    for theme in themes:
        name = theme.get("name")
        if not name:
            continue
        entry = merged.setdefault(name, {"name": name, "members": []})
        seen = set(entry["members"])
        for member in theme.get("members", []):
            if member not in seen:
                seen.add(member)
                entry["members"].append(member)
    ranked = sorted(merged.values(), key=lambda t: len(t["members"]), reverse=True)
    return [t for t in ranked if t["members"]][:MAX_THEMES]


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
        #: How often the model has declined to describe a given symbol.
        self._misses: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._inflight = False
        self._pending_args = None
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
        if not self.settings.ai_enabled:
            return

        with self._lock:
            if self._inflight:
                # A call takes seconds and agents write in bursts, so most edits
                # arrive mid-flight. Dropping them left those symbols with no
                # summary and nothing to retry them — the annotation simply
                # never caught up. Remember the latest request and run it when
                # the current one finishes.
                self._pending_args = (target, changeset, langs, files, read_side, symbol_hashes)
                return
            self._inflight = True

        thread = threading.Thread(
            target=self._run,
            args=(target, changeset, langs, files, read_side, symbol_hashes),
            daemon=True,
        )
        thread.start()

    def _run(self, target, changeset, langs, files, read_side, symbol_hashes) -> None:
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
            with self._lock:
                self._inflight = False
                queued = self._pending_args
                self._pending_args = None
            if queued is not None and self.settings.ai_enabled:
                # Anything that changed while we were busy still needs covering.
                self.annotate_async(*queued)

    # ------------------------------------------------------------------

    def annotate(
        self, target, changeset, langs, files, read_side, symbol_hashes
    ) -> dict[str, Any] | None:
        changed = changeset.units()
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
            key = (change.node_id, symbol_hashes.get(change.node_id, change.status))
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

        # Largest changes first, so when the cap bites it drops trivia rather
        # than whatever happened to sort last by path.
        pending.sort(key=lambda item: item[0].added + item[0].removed, reverse=True)

        def entry_for(change, ref: str) -> dict[str, Any]:
            file_change = next((f for f in changeset.files if f.path == change.path), None)
            old_text = read_side(file_change.old_ref if file_change else None, change.path)
            new_text = read_side(file_change.new_ref if file_change else None, change.path)
            return {
                "id": ref,
                "status": change.status,
                "kind": change.kind,
                "qualname": change.qualname,
                "path": change.path,
                "diff": _slice_diff(old_text, new_text, change),
            }

        truncated = max(0, len(pending) - MAX_SYMBOLS_PER_CALL)
        analysing = pending[:MAX_SYMBOLS_PER_CALL]

        # Ask about a bounded number of symbols at a time. Handed fifty at once
        # the model reliably answers for a subset and drops the rest, which used
        # to leave those symbols with no synopsis at all; smaller batches come
        # back complete. The cached system prefix makes the extra calls cheap.
        summaries: list[dict[str, Any]] = []
        risk: list[dict[str, Any]] = []
        themes: list[dict[str, Any]] = []
        note = ""
        for start in range(0, len(analysing), SYMBOLS_PER_BATCH):
            batch = analysing[start : start + SYMBOLS_PER_BATCH]

            # Refer to symbols by a short opaque token rather than by node id.
            # Given `sym:path/to/f.py::Klass.method` the model helpfully
            # "normalises" the `sym:` prefix away, and every annotation then
            # keyed a node that doesn't exist — so nothing showed on hover.
            # A token it has no opinion about comes back verbatim.
            refs = {f"s{start + i + 1}": change for i, (change, _k) in enumerate(batch)}
            entries = [entry_for(change, ref) for ref, change in refs.items()]
            part = self._request(
                target, langs, files, entries, truncated if start == 0 else 0
            )
            resolve = _resolver(refs)

            for item in part.get("summaries", []):
                node_id = resolve(item.get("id"))
                if node_id:
                    summaries.append({"id": node_id, "text": item.get("text")})
            for item in part.get("risk", []):
                node_id = resolve(item.get("id"))
                if node_id:
                    risk.append({**item, "id": node_id})
            for theme in part.get("themes", []):
                members = [resolve(m) for m in theme.get("members", [])]
                themes.append({**theme, "members": [m for m in members if m]})
            note = note or part.get("review_note", "")

            # Cache only what actually came back. Marking a skipped symbol as
            # analysed burns it permanently: it never matches again, so it can
            # never be re-requested, and it has no summary to serve either.
            by_id = {s["id"]: s.get("text") for s in summaries}
            risk_by_id = {r["id"]: r for r in risk}
            for change, key in batch:
                described = by_id.get(change.node_id)
                if not described and change.node_id not in risk_by_id:
                    misses = self._misses.get(key, 0) + 1
                    self._misses[key] = misses
                    # After a few attempts, accept that the model has nothing to
                    # say about it rather than paying for it on every rescan.
                    if misses < MAX_RETRIES_PER_SYMBOL:
                        continue
                self._cache[key] = {
                    "summary": described,
                    "risk": {
                        "level": risk_by_id.get(change.node_id, {}).get("level"),
                        "reason": risk_by_id.get(change.node_id, {}).get("reason"),
                    }
                    if change.node_id in risk_by_id
                    else None,
                }

        result: dict[str, Any] = {
            "summaries": cached_summaries + summaries,
            "risk": cached_risk + risk,
            # Themes span the whole changeset, so merge them across batches.
            "themes": _merge_themes(themes),
            "review_note": note,
            "usage": self.usage,
        }
        if truncated:
            result["truncated"] = truncated
        self._last_themes = result["themes"]
        self._last_note = note
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
    if change.line is None:
        # A whole-file unit (a shell script, a README, a stylesheet) has no span
        # to window on. Windowing from the top compared the first 60 lines of
        # each side and found nothing whenever the edit was further down, so the
        # model was asked to describe a change it had been shown no trace of —
        # and duly reported that the diff was unavailable.
        return _unified(old_text, new_text, change.qualname)
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
