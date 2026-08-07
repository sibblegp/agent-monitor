"""The orchestrator: owns current state, rebuilds the graph, emits deltas."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import gitutil
from .watcher import RepoWatcher
from .analysis import parse_source
from .analysis.registry import detect_lang, ts_unavailable_reason
from .cache import ParseCache
from .graph.diff import diff_file
from .graph.flow import build_flow, focus_subgraph
from .graph.structure import ROOT_ID, build_structure
from .model import (
    CHANGED_STATUSES,
    ChangeSet,
    Graph,
    Node,
    ParsedFile,
    RepoTarget,
    file_id,
)

#: Extensions we bother parsing at all; everything else still gets a file node.
PARSEABLE = {"python", "typescript", "javascript"}

MAX_FILES = 20000


class Engine:
    """Holds the analyzed state of one repository."""

    def __init__(self, on_update: Callable[[], None] | None = None) -> None:
        self.target: RepoTarget | None = None
        self.mode: str = "live"
        self.ref: str | None = None
        self.cache = ParseCache()
        self.graph = Graph()
        self.changeset = ChangeSet()
        self.parsed: dict[str, ParsedFile] = {}
        self.warnings: list[str] = []
        self.last_scan_ms: int = 0
        self.truncated: int = 0
        self.paused: bool = False
        self._ai: dict[str, Any] | None = None

        #: Called (from a worker thread) after a watcher-triggered rescan.
        self.on_update = on_update
        #: Called after AI annotations arrive; set by the server.
        self.on_ai_update: Callable[[], None] | None = None
        self._watcher: RepoWatcher | None = None
        self._lock = threading.RLock()
        self.ai = None  # set by attach_ai()

    # ------------------------------------------------------------------
    # opening
    # ------------------------------------------------------------------

    def open(self, path: str, scope: str | None = None) -> RepoTarget:
        target = gitutil.resolve_target(path, scope)
        with self._lock:
            self._stop_watcher()
            self.target = target
            self.cache.clear()
            self.parsed = {}
            self._ai = None
            self.mode, self.ref = "live", None
            self.rescan()
        self._start_watcher()
        return target

    def set_mode(self, mode: str, ref: str | None = None) -> None:
        with self._lock:
            self.mode = mode
            self.ref = ref
            self._ai = None
            self.rescan()
        # Only the working tree can change under us; a commit or branch diff is
        # immutable, so stop watching when we're not looking at `live`.
        if mode == "live":
            self._start_watcher()
        else:
            self._stop_watcher()

    # ------------------------------------------------------------------
    # live watching
    # ------------------------------------------------------------------

    def _start_watcher(self) -> None:
        if self.target is None or self.mode != "live":
            return
        if self._watcher is not None:
            return
        self._watcher = RepoWatcher(self.target, self._on_files_changed)
        self._watcher.start()

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    @property
    def watch_mode(self) -> str | None:
        return self._watcher.mode if self._watcher else None

    def _on_files_changed(self, paths: set[str]) -> None:
        """Watcher callback — runs on a worker thread."""
        if self.paused:
            return
        with self._lock:
            if self.target is None or self.mode != "live":
                return
            self.rescan()
        if self.on_update is not None:
            self.on_update()
        self.request_ai()

    def close(self) -> None:
        self._stop_watcher()

    # ------------------------------------------------------------------
    # optional AI annotations
    # ------------------------------------------------------------------

    def attach_ai(self, annotator) -> None:
        self.ai = annotator

    def _symbol_hashes(self) -> dict[str, str]:
        """node id -> semantic body hash, so annotations cache per symbol."""
        from .model import sym_id  # local import keeps module import cheap

        out: dict[str, str] = {}
        for path, pf in self.parsed.items():
            for symbol in pf.symbols.values():
                out[sym_id(path, symbol.qualname)] = symbol.body_hash
        return out

    def _read_side(self, ref: str | None, path: str) -> str | None:
        return _decode(gitutil.read_side(self.target.root, ref, path)) if self.target else None

    def request_ai(self) -> None:
        """Kick off annotation for the current changeset, if AI is enabled."""
        if self.ai is None or self.target is None:
            return
        if not self.ai.settings.ai_enabled:
            return
        langs: dict[str, int] = {}
        for pf in self.parsed.values():
            langs[pf.lang] = langs.get(pf.lang, 0) + 1
        self.ai.annotate_async(
            self.target,
            self.changeset,
            langs,
            sorted(self.parsed),
            self._read_side,
            self._symbol_hashes(),
        )

    def apply_ai(self, result: dict[str, Any]) -> None:
        """Merge annotations onto the graph so they ride along in snapshots."""
        self._ai = result
        for item in result.get("summaries", []):
            node = self.graph.nodes.get(item.get("id"))
            if node is not None:
                node.summary = item.get("text")
        for item in result.get("risk", []):
            node = self.graph.nodes.get(item.get("id"))
            if node is not None:
                node.risk = item.get("level")
                node.risk_reason = item.get("reason")
        for theme in result.get("themes", []):
            for node_id in theme.get("members", []):
                node = self.graph.nodes.get(node_id)
                if node is not None:
                    node.theme = theme.get("name")

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------

    def _parse_worktree_file(self, root: str, path: str) -> ParsedFile | None:
        full = Path(root) / path
        try:
            stat = full.stat()
        except OSError:
            return None
        if stat.st_size > gitutil.MAX_BLOB_BYTES:
            return ParsedFile(path=path, lang=detect_lang(path), loc=0)

        cached = self.cache.get_worktree(path, stat.st_mtime_ns, stat.st_size)
        if cached is not None:
            return cached

        raw = gitutil.read_worktree(root, path)
        if raw is None:
            return None
        parsed = parse_source(path, raw)
        self.cache.put_worktree(path, stat.st_mtime_ns, stat.st_size, parsed)
        return parsed

    def _parse_ref_files(self, root: str, ref: str, paths: list[str]) -> dict[str, ParsedFile]:
        out: dict[str, ParsedFile] = {}
        missing: list[str] = []
        for path in paths:
            cached = self.cache.get_blob(ref, path)
            if cached is not None:
                out[path] = cached
            else:
                missing.append(path)

        for i in range(0, len(missing), 500):
            chunk = missing[i : i + 500]
            blobs = gitutil.read_blobs(root, ref, chunk)
            for path in chunk:
                raw = blobs.get(path)
                if raw is None:
                    continue
                parsed = parse_source(path, raw)
                self.cache.put_blob(ref, path, parsed)
                out[path] = parsed
        return out

    def _parse_one_side(self, root: str, ref: str | None, path: str) -> ParsedFile | None:
        """Parse a single file from a ref, or the worktree when ref is None."""
        if ref is None:
            return self._parse_worktree_file(root, path)
        cached = self.cache.get_blob(ref, path)
        if cached is not None:
            return cached
        raw = gitutil.read_blob(root, ref, path)
        if raw is None:
            return None
        parsed = parse_source(path, raw)
        self.cache.put_blob(ref, path, parsed)
        return parsed

    # ------------------------------------------------------------------
    # scanning
    # ------------------------------------------------------------------

    def rescan(self) -> None:
        if self.target is None:
            return
        started = time.perf_counter()
        target = self.target
        root = target.root
        self.warnings = []
        self.truncated = 0

        changes, base = ([], None)
        if target.is_git:
            try:
                changes, base = gitutil.changed_files(target, self.mode, self.ref)
            except gitutil.GitError as exc:
                self.warnings.append(str(exc))

        head_ref = changes[0].new_ref if changes else None
        if self.mode != "live" and self.ref:
            head_ref = self.ref if self.mode != "range" else self.ref.split("..", 1)[1]

        # ---- files on the "new" side -----------------------------------
        if head_ref is None:
            paths = gitutil.list_files(target)
        else:
            paths = [
                p
                for p in gitutil.list_files_at_ref(root, head_ref)
                if target.in_scope(p)
            ]

        if len(paths) > MAX_FILES:
            self.truncated = len(paths) - MAX_FILES
            self.warnings.append(
                f"{len(paths):,} files in scope; analyzing the first {MAX_FILES:,}"
            )
            paths = paths[:MAX_FILES]

        # ---- parse them ------------------------------------------------
        parsed: dict[str, ParsedFile] = {}
        if head_ref is None:
            for path in paths:
                pf = self._parse_worktree_file(root, path)
                if pf is not None:
                    parsed[path] = pf
        else:
            parsed = self._parse_ref_files(root, head_ref, paths)

        self.parsed = parsed

        # ---- symbol-level diff for changed files -----------------------
        symbol_changes = []
        for change in changes:
            if detect_lang(change.path) not in PARSEABLE:
                continue
            old_path = change.old_path or change.path

            new_pf = parsed.get(change.path) if change.status != "removed" else None
            new_raw = None
            if change.status != "removed":
                new_raw = (
                    gitutil.read_worktree(root, change.path)
                    if change.new_ref is None
                    else gitutil.read_blob(root, change.new_ref, change.path)
                )

            old_pf = None
            old_raw = None
            if change.status not in ("added",) and change.old_ref:
                old_pf = self._parse_one_side(root, change.old_ref, old_path)
                old_raw = gitutil.read_blob(root, change.old_ref, old_path)

            symbol_changes.extend(
                diff_file(
                    change.path,
                    old_pf,
                    new_pf,
                    old_text=_decode(old_raw),
                    new_text=_decode(new_raw),
                )
            )

        self.changeset = ChangeSet(
            mode=self.mode, base=base, files=changes, symbols=symbol_changes
        )

        # ---- build the graph -------------------------------------------
        graph = build_structure(target, parsed, self.changeset)
        build_flow(target, parsed, graph)
        self.graph = graph

        reason = ts_unavailable_reason()
        if reason and any(
            p.lang in ("typescript", "javascript") for p in parsed.values()
        ):
            self.warnings.append(
                "JS/TS shown as files only — install extras: pip install 'agent-monitor[ts]'"
            )

        self.last_scan_ms = int((time.perf_counter() - started) * 1000)

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    def changed_node_ids(self) -> set[str]:
        return {
            n.id
            for n in self.graph.nodes.values()
            if n.status in CHANGED_STATUSES and n.kind not in ("root", "dir")
        }

    def ancestors_of(self, node_ids: set[str]) -> set[str]:
        out: set[str] = set()
        for node_id in node_ids:
            cursor = self.graph.nodes.get(node_id)
            while cursor is not None and cursor.parent:
                if cursor.parent in out:
                    break
                out.add(cursor.parent)
                cursor = self.graph.nodes.get(cursor.parent)
        return out

    def focus(self, depth: int = 2) -> dict[str, list[str]]:
        """Node/edge ids the 'changes only' view should show."""
        changed = self.changed_node_ids()
        nodes, edges = focus_subgraph(self.graph, changed, depth)
        nodes |= self.ancestors_of(nodes | changed)
        nodes |= changed
        nodes.add(ROOT_ID)
        contains = {
            e.id
            for e in self.graph.edges.values()
            if e.kind == "contains" and e.src in nodes and e.dst in nodes
        }
        return {"nodes": sorted(nodes), "edges": sorted(edges | contains)}

    def meta(self) -> dict[str, Any]:
        target = self.target
        counts: dict[str, int] = {}
        for node in self.graph.nodes.values():
            counts[node.kind] = counts.get(node.kind, 0) + 1
        langs: dict[str, int] = {}
        for pf in self.parsed.values():
            langs[pf.lang] = langs.get(pf.lang, 0) + 1

        return {
            "repo": target.to_dict() if target else None,
            "mode": self.mode,
            "ref": self.ref,
            "base": self.changeset.base,
            "counts": counts,
            "langs": langs,
            "warnings": self.warnings,
            "scan_ms": self.last_scan_ms,
            "watching": self.watch_mode,
            "paused": self.paused,
            "ai": self.ai.status() if self.ai is not None else None,
            "cache": self.cache.stats(),
            "changed_files": len(self.changeset.files),
            "changed_symbols": sum(
                1 for s in self.changeset.symbols if s.status in CHANGED_STATUSES
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        payload = self.graph.to_dict()
        payload["meta"] = self.meta()
        payload["changes"] = self.changeset.to_dict()
        payload["focus"] = self.focus()
        if self._ai:
            payload["ai"] = self._ai
        return payload


def _decode(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8-sig", errors="replace")
