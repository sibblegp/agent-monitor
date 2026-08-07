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
        #: node id -> {summary, risk, risk_reason, theme}. Survives graph rebuilds.
        self._annotations: dict[str, dict[str, Any]] = {}
        self._head: str | None = None

        #: Called (from a worker thread) after a watcher-triggered rescan.
        self.on_update = on_update
        #: Called after AI annotations arrive; set by the server.
        self.on_ai_update: Callable[[], None] | None = None
        self._watcher: RepoWatcher | None = None
        self._lock = threading.RLock()
        self.ai = None  # set by attach_ai()
        self.narrator = None

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
            self._annotations = {}
            if self.ai is not None:
                self.ai.reset()
            if self.narrator is not None:
                self.narrator.reset()
            self.mode, self.ref = "live", None
            self._head = gitutil.head_info(target.root)[0] if target.is_git else None
            self.rescan()
        self._start_watcher()
        return target

    def set_mode(self, mode: str, ref: str | None = None) -> None:
        with self._lock:
            self.mode = mode
            self.ref = ref
            self._ai = None
            self._annotations = {}
            if self.ai is not None:
                self.ai.reset()
            if self.narrator is not None:
                self.narrator.reset()
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

    def attach_ai(self, annotator, narrator=None) -> None:
        self.ai = annotator
        self.narrator = narrator

    def _symbol_hashes(self) -> dict[str, str]:
        """node id -> content fingerprint, so AI work caches per unit."""
        import hashlib

        from .model import sym_id  # local import keeps module import cheap

        out: dict[str, str] = {}
        for path, pf in self.parsed.items():
            for symbol in pf.symbols.values():
                out[sym_id(path, symbol.qualname)] = symbol.body_hash

        # Whole-file units need a fingerprint too, or an unparsed file would be
        # re-described on every rescan (no hash) or never again (stale hash).
        # Only changed files are hashed, so this stays cheap.
        #
        # Read the file from the side being *shown*, not the worktree: viewing a
        # commit, the worktree holds some later version, and fingerprinting that
        # would key the cache to content nobody is looking at.
        for change in self.changeset.files:
            raw = (
                gitutil.read_side(self.target.root, change.new_ref, change.path)
                if self.target
                else None
            )
            if raw is None:
                out[file_id(change.path)] = f"gone:{change.status}"
            else:
                out[file_id(change.path)] = hashlib.sha1(raw).hexdigest()[:16]
        return out

    def _read_side(self, ref: str | None, path: str) -> str | None:
        return _decode(gitutil.read_side(self.target.root, ref, path)) if self.target else None

    def request_ai(self) -> None:
        """Kick off annotation and narration for the current changeset."""
        if self.ai is None or self.target is None:
            return
        if not self.ai.settings.ai_enabled:
            return

        live = self.mode == "live"

        # A commit is a chapter break: HEAD moved, so the diff everything is
        # measured against moved with it.
        if live and self.narrator is not None and self.target.is_git:
            head, subject = gitutil.head_info(self.target.root)
            if head and self._head and head != self._head:
                self.narrator.note_commit(head, subject)
                self._annotations = {}
            self._head = head or self._head
        langs: dict[str, int] = {}
        for pf in self.parsed.values():
            langs[pf.lang] = langs.get(pf.lang, 0) + 1
        hashes = self._symbol_hashes()
        args = (self.target, self.changeset, langs, sorted(self.parsed), self._read_side, hashes)
        self.ai.annotate_async(*args)
        if self.narrator is not None and live:
            # Separate call on a coarser cadence: the transcript narrates the
            # delta since its last entry, which is a different job from
            # re-describing the whole changeset.
            #
            # Only while live: the transcript is a play-by-play of work
            # happening now. Browsing a commit is reading history, and feeding
            # it through the narrator would report a year-old commit as though
            # the agent had just written it.
            self.narrator.observe(*args)

    def apply_ai(self, result: dict[str, Any]) -> None:
        """Record annotations and merge them onto the current graph.

        They're kept in `_annotations` rather than only on the graph because
        `rescan()` rebuilds the graph from scratch — writing them straight onto
        nodes meant every annotation vanished the next time anything changed,
        so a synopsis would appear and then silently disappear.
        """
        # Batches now report in as they land, so most of these are partial:
        # summaries only, no themes or review note yet. Keeping the last full
        # payload means the themes and review note already on screen survive
        # instead of blinking out every time a batch arrives.
        if not result.get("partial"):
            self._ai = result

        for item in result.get("summaries", []):
            node_id = item.get("id")
            if node_id:
                self._annotations.setdefault(node_id, {})["summary"] = item.get("text")
        for item in result.get("risk", []):
            node_id = item.get("id")
            if node_id:
                entry = self._annotations.setdefault(node_id, {})
                entry["risk"] = item.get("level")
                entry["risk_reason"] = item.get("reason")
        for theme in result.get("themes", []):
            for node_id in theme.get("members", []):
                if node_id:
                    self._annotations.setdefault(node_id, {})["theme"] = theme.get("name")

        self._reapply_annotations()

    def _reapply_annotations(self) -> None:
        """Re-attach stored annotations after a graph rebuild, then roll up."""
        if not self._annotations:
            return
        for node_id, values in self._annotations.items():
            node = self.graph.nodes.get(node_id)
            if node is None:
                continue
            if values.get("summary"):
                node.summary = values["summary"]
            if values.get("risk"):
                node.risk = values["risk"]
                node.risk_reason = values.get("risk_reason")
            if values.get("theme"):
                node.theme = values["theme"]
        self._derive_container_summaries()

    def _derive_container_summaries(self) -> None:
        """Give changed files, classes, and directories a synopsis too.

        The model annotates individual symbols, but hovering a changed *file*
        should say something as well. Composing the file's synopsis from its
        changed children keeps it accurate and costs nothing extra.
        """
        children: dict[str, list[Node]] = {}
        for node in self.graph.nodes.values():
            if node.parent:
                children.setdefault(node.parent, []).append(node)

        _RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

        def visit(node: Node) -> tuple[list[str], str | None]:
            """Return (summaries at or below this node, worst risk)."""
            summaries: list[str] = []
            worst: str | None = None

            if node.summary:
                summaries.append(node.summary)
            if node.risk:
                worst = node.risk

            for child in children.get(node.id, []):
                child_summaries, child_risk = visit(child)
                summaries.extend(child_summaries)
                if child_risk and (
                    worst is None or _RISK_ORDER.get(child_risk, 0) > _RISK_ORDER.get(worst, 0)
                ):
                    worst = child_risk

            is_container = node.kind in ("root", "dir", "file", "class")
            if is_container and node.status in CHANGED_STATUSES:
                own = [s for s in summaries if s and s != node.summary]
                if not node.summary and own:
                    shown = own[:3]
                    text = "; ".join(shown)
                    if len(own) > len(shown):
                        text += f"; +{len(own) - len(shown)} more"
                    node.summary = text
                if not node.risk and worst:
                    node.risk = worst
                    node.risk_reason = "highest risk among the changes inside"

            return summaries, worst

        root = self.graph.nodes.get(ROOT_ID)
        if root is not None:
            visit(root)

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
        # The graph is derived state; annotations are not. Re-attach them.
        self._reapply_annotations()

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

    # ------------------------------------------------------------------
    # review notes
    # ------------------------------------------------------------------

    def build_changeset(self, mode: str, ref: str | None) -> ChangeSet:
        """Compute a changeset for any comparison without disturbing the view.

        `rescan` parses the whole tree because it has to build a graph. A review
        only needs the diff, so this parses just the files that differ — which
        is what makes reviewing a long branch affordable.
        """
        if self.target is None or not self.target.is_git:
            return ChangeSet(mode=mode)
        root = self.target.root
        changes, base = gitutil.changed_files(self.target, mode, ref)

        symbol_changes = []
        for change in changes:
            if detect_lang(change.path) not in PARSEABLE:
                continue
            old_path = change.old_path or change.path

            new_pf = new_raw = None
            if change.status != "removed":
                new_pf = self._parse_one_side(root, change.new_ref, change.path)
                new_raw = gitutil.read_side(root, change.new_ref, change.path)

            old_pf = old_raw = None
            if change.status != "added" and change.old_ref:
                old_pf = self._parse_one_side(root, change.old_ref, old_path)
                old_raw = gitutil.read_blob(root, change.old_ref, old_path)

            symbol_changes.extend(
                diff_file(
                    change.path, old_pf, new_pf,
                    old_text=_decode(old_raw), new_text=_decode(new_raw),
                )
            )
        return ChangeSet(mode=mode, base=base, files=changes, symbols=symbol_changes)

    def review(self, against: str | None = None) -> dict[str, Any]:
        """Everything the AI has said about a comparison, grouped by file.

        `against` names a branch to compare this one with. Without it the
        subject is whatever the graph panes are showing — the uncommitted work
        while live, or the selected commit or branch. Reviewing the working tree
        while looking at a year-old commit would answer a question nobody asked.
        """
        if self.target is None:
            return {"error": "no repository open"}

        mode, ref = ("against", against) if against else (self.mode, self.ref)
        try:
            changeset = self.build_changeset(mode, ref)
        except gitutil.GitError as exc:
            return {"error": str(exc)}

        units = changeset.units()
        annotations: dict[str, dict[str, Any]] = {}
        note, themes, error = "", [], None
        pending = 0

        if self.ai is not None and self.ai.settings.ai_enabled and self.ai.available and units:
            langs: dict[str, int] = {}
            for parsed in self.parsed.values():
                langs[parsed.lang] = langs.get(parsed.lang, 0) + 1
            try:
                result = self.ai.annotate(
                    self.target, changeset, langs, sorted(self.parsed),
                    self._read_side, self._changeset_hashes(changeset),
                )
            except Exception as exc:  # additive only — never break the review
                result, error = None, f"{type(exc).__name__}: {exc}"
            if result:
                for item in result.get("summaries", []):
                    annotations.setdefault(item["id"], {})["text"] = item.get("text")
                for item in result.get("risk", []):
                    entry = annotations.setdefault(item["id"], {})
                    entry["risk"] = item.get("level")
                    entry["reason"] = item.get("reason")
                note = result.get("review_note", "")
                themes = result.get("themes", [])
                # Per-call symbol cap. Say so rather than quietly describing
                # less than the diff contains; refreshing picks up the rest,
                # and everything already described is cached.
                pending = result.get("truncated", 0)
        elif units:
            error = "AI insights are off — showing the diff without commentary."

        by_path: dict[str, dict[str, Any]] = {}
        for change in changeset.files:
            by_path[change.path] = {
                "path": change.path, "status": change.status,
                "added": change.added, "removed": change.removed, "items": [],
            }
        for unit in units:
            group = by_path.get(unit.path)
            if group is None:
                continue
            note_for = annotations.get(unit.node_id, {})
            if unit.kind == "file":
                # A whole-file unit *is* its group — don't list it as a child.
                group["text"] = note_for.get("text")
                group["risk"] = note_for.get("risk")
                group["reason"] = note_for.get("reason")
                continue
            group["items"].append({
                "id": unit.node_id, "name": unit.qualname, "kind": unit.kind,
                "status": unit.status, "added": unit.added, "removed": unit.removed,
                "text": note_for.get("text"),
                "risk": note_for.get("risk"), "reason": note_for.get("reason"),
            })

        groups = sorted(by_path.values(), key=lambda g: g["path"])
        return {
            "against": against,
            "mode": mode,
            "ref": ref,
            "pending": pending,
            "base": changeset.base,
            "branch": self.target.branch,
            "note": note,
            "themes": themes,
            "error": error,
            "counts": {
                "files": len(changeset.files),
                "symbols": sum(len(g["items"]) for g in groups),
                "added": sum(c.added for c in changeset.files),
                "removed": sum(c.removed for c in changeset.files),
                "described": sum(1 for a in annotations.values() if a.get("text")),
            },
            "groups": groups,
        }

    def _changeset_hashes(self, changeset: ChangeSet) -> dict[str, str]:
        """Cache keys for an arbitrary changeset, mirroring `_symbol_hashes`."""
        import hashlib  # noqa: PLC0415

        out: dict[str, str] = {}
        refs = {c.path: c.new_ref for c in changeset.files}
        for unit in changeset.units():
            if unit.kind == "file":
                raw = (
                    gitutil.read_side(self.target.root, refs.get(unit.path), unit.path)
                    if self.target
                    else None
                )
                out[unit.node_id] = (
                    f"gone:{unit.status}" if raw is None
                    else hashlib.sha1(raw).hexdigest()[:16]
                )
            else:
                out[unit.node_id] = f"{unit.status}:{unit.added}:{unit.removed}"
        return out

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

    def focus(self, depth: int = 0) -> dict[str, list[str]]:
        """Node/edge ids the 'changes only' view should show.

        Strictly what changed, plus the directories and files needed to place
        those nodes in the tree. Deliberately *no* call-hop expansion: pulling
        in callers and callees two hops out meant the Changes view was mostly
        unchanged code, which is the opposite of what it is for. The flow pane
        adds that context back on demand via the depth control, client-side.
        """
        changed = self.changed_node_ids()
        nodes = set(changed)
        if depth > 0:
            reachable, _ = focus_subgraph(self.graph, changed, depth)
            nodes |= reachable
        nodes |= self.ancestors_of(nodes)
        nodes.add(ROOT_ID)
        edges = {
            e.id
            for e in self.graph.edges.values()
            if e.src in nodes and e.dst in nodes
        }
        return {"nodes": sorted(nodes), "edges": sorted(edges)}

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
            "narration": self.narrator.status() if self.narrator is not None else None,
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
