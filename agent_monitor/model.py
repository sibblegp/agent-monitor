"""Core data model.

Node ids are stable strings so the frontend can diff snapshots by key:

    dir:<relpath>              a directory
    file:<relpath>             a source file
    sym:<relpath>::<qualname>  a class / function / method
    ext:<toplevel>             an external package (os, websockets, react, ...)

Everything here is JSON-serializable via ``to_dict``; ``None`` fields are
dropped on the way out to keep websocket payloads small.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any, Literal

# --------------------------------------------------------------------------
# enums (plain strings — they cross the wire as-is)
# --------------------------------------------------------------------------

Status = Literal[
    "unchanged",
    "added",
    "modified",
    "signature_changed",
    "removed",
    "renamed",
    "moved",
]

NodeKind = Literal["root", "dir", "file", "class", "function", "method", "external"]
EdgeKind = Literal["contains", "calls", "imports"]

#: Statuses that mean "the user should look at this".
CHANGED_STATUSES: frozenset[str] = frozenset(
    {"added", "modified", "signature_changed", "removed", "renamed"}
)


# --------------------------------------------------------------------------
# id helpers
# --------------------------------------------------------------------------


def dir_id(relpath: str) -> str:
    return f"dir:{relpath}"


def file_id(relpath: str) -> str:
    return f"file:{relpath}"


def sym_id(relpath: str, qualname: str) -> str:
    return f"sym:{relpath}::{qualname}"


def ext_id(toplevel: str) -> str:
    return f"ext:{toplevel}"


def parent_dir_id(relpath: str) -> str | None:
    """Node id of the directory containing ``relpath``, or None at the root."""
    parent = posixpath.dirname(relpath)
    return dir_id(parent) if parent else None


# --------------------------------------------------------------------------
# parsing results
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Symbol:
    """One class, function, or method found in a source file."""

    qualname: str  # "Engine.rebuild" — unique within its file
    name: str  # "rebuild"
    kind: NodeKind  # class | function | method
    lineno: int
    end_lineno: int
    signature: str  # normalized param list + decorators
    body_hash: str  # sha1 of comment/whitespace-normalized body
    parent: str | None = None  # enclosing qualname, if any
    decorators: tuple[str, ...] = ()
    is_entry: bool = False  # main / route / test / CLI entry point

    @property
    def loc(self) -> int:
        return max(1, self.end_lineno - self.lineno + 1)


@dataclass(slots=True)
class RawCall:
    """An unresolved call site, recorded during parsing."""

    caller: str  # qualname of the enclosing symbol
    target: str  # dotted source text of the callee, e.g. "self.foo" or "os.path.join"
    lineno: int


@dataclass(slots=True)
class ParsedFile:
    """Everything one parser pass extracts from a single file."""

    path: str  # repo-relative, posix separators
    lang: str  # python | typescript | javascript | text | binary
    loc: int
    symbols: dict[str, Symbol] = field(default_factory=dict)
    #: local alias -> dotted module path, from import statements
    imports: dict[str, str] = field(default_factory=dict)
    #: class qualname -> tuple of base class names as written
    bases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    calls: list[RawCall] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Node:
    id: str
    kind: NodeKind
    name: str
    status: str = "unchanged"
    path: str | None = None  # repo-relative file path (None for ext:/root)
    parent: str | None = None  # containing node id
    qualname: str | None = None
    lang: str | None = None
    line: int | None = None
    end_line: int | None = None
    size: int = 1  # LOC, drives node radius
    added: int = 0  # lines added (changed nodes only)
    removed: int = 0  # lines removed
    is_entry: bool = False
    signature: str | None = None
    #: AI annotations, filled in later and merged by node id
    summary: str | None = None
    risk: str | None = None
    risk_reason: str | None = None
    theme: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "size": self.size,
        }
        for key in (
            "path",
            "parent",
            "qualname",
            "lang",
            "line",
            "end_line",
            "signature",
            "summary",
            "risk",
            "risk_reason",
            "theme",
        ):
            value = getattr(self, key)
            if value is not None:
                d[key] = value
        if self.added:
            d["added"] = self.added
        if self.removed:
            d["removed"] = self.removed
        if self.is_entry:
            d["is_entry"] = True
        return d


@dataclass(slots=True)
class Edge:
    id: str
    src: str
    dst: str
    kind: EdgeKind
    count: int = 1
    status: str = "unchanged"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "src": self.src,
            "dst": self.dst,
            "kind": self.kind,
            "status": self.status,
        }
        if self.count != 1:
            d["count"] = self.count
        return d


def edge_id(src: str, dst: str, kind: str) -> str:
    return f"{kind}|{src}|{dst}"


@dataclass(slots=True)
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is not None:
            return existing
        self.nodes[node.id] = node
        return node

    def add_edge(self, src: str, dst: str, kind: EdgeKind, count: int = 1) -> Edge:
        eid = edge_id(src, dst, kind)
        existing = self.edges.get(eid)
        if existing is not None:
            existing.count += count
            return existing
        edge = Edge(id=eid, src=src, dst=dst, kind=kind, count=count)
        self.edges[eid] = edge
        return edge

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges.values()],
        }


# --------------------------------------------------------------------------
# changes
# --------------------------------------------------------------------------


@dataclass(slots=True)
class FileChange:
    """One entry from `git diff --name-status` (or an untracked file)."""

    path: str  # repo-relative, posix separators (new path for renames)
    status: str  # added | modified | removed | renamed
    old_path: str | None = None  # populated for renames
    old_ref: str | None = None  # git ref holding the "before" blob
    new_ref: str | None = None  # git ref holding the "after" blob; None = worktree
    added: int = 0
    removed: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = {"path": self.path, "status": self.status}
        if self.old_path:
            d["old_path"] = self.old_path
        if self.added:
            d["added"] = self.added
        if self.removed:
            d["removed"] = self.removed
        return d


@dataclass(slots=True)
class SymbolChange:
    """A single symbol that differs between the two sides of a diff."""

    path: str
    qualname: str
    name: str
    kind: NodeKind
    status: str
    line: int | None = None
    added: int = 0
    removed: int = 0

    @property
    def node_id(self) -> str:
        # A whole-file unit addresses the file node, not a symbol inside it.
        if self.kind == "file":
            return file_id(self.path)
        return sym_id(self.path, self.qualname)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.node_id,
            "path": self.path,
            "qualname": self.qualname,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
        }
        if self.line is not None:
            d["line"] = self.line
        if self.added:
            d["added"] = self.added
        if self.removed:
            d["removed"] = self.removed
        return d


@dataclass(slots=True)
class ChangeSet:
    """The full set of changes for the current mode."""

    mode: str = "live"
    base: str | None = None
    files: list[FileChange] = field(default_factory=list)
    symbols: list[SymbolChange] = field(default_factory=list)

    def file_statuses(self) -> dict[str, str]:
        return {fc.path: fc.status for fc in self.files}

    def units(self) -> list[SymbolChange]:
        """Everything worth describing: changed symbols, plus whole-file changes.

        Only Python/JS/TS get parsed into symbols, so iterating `symbols` alone
        makes every other change invisible — a shell script, a README, a JSON
        config would produce no narration and no hover summary at all. Files
        that contributed no symbol change are represented here as a single
        file-level unit so they are described too.
        """
        units = [s for s in self.symbols if s.status in CHANGED_STATUSES]
        covered = {s.path for s in units}

        for fc in self.files:
            if fc.path in covered:
                continue
            name = fc.path.rsplit("/", 1)[-1]
            units.append(
                SymbolChange(
                    path=fc.path,
                    qualname=name,
                    name=name,
                    kind="file",
                    status=fc.status,
                    line=None,
                    added=fc.added,
                    removed=fc.removed,
                )
            )
        return units

    def symbol_statuses(self) -> dict[str, SymbolChange]:
        return {sc.node_id: sc for sc in self.symbols}

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "base": self.base,
            "files": [f.to_dict() for f in self.files],
            "symbols": [s.to_dict() for s in self.symbols],
        }


@dataclass(slots=True)
class RepoTarget:
    """Where we are looking: a git root plus the subtree we actually analyze."""

    root: str  # absolute path to repo root (or the plain dir in no-git mode)
    scope: str  # repo-relative subtree, "" means the whole repo
    is_git: bool
    branch: str | None = None

    @property
    def abs_scope(self) -> str:
        import os

        return os.path.join(self.root, self.scope) if self.scope else self.root

    def in_scope(self, relpath: str) -> bool:
        if not self.scope:
            return True
        return relpath == self.scope or relpath.startswith(self.scope + "/")

    def to_dict(self) -> dict[str, Any]:
        import os

        return {
            "root": self.root,
            "scope": self.scope,
            "is_git": self.is_git,
            "branch": self.branch,
            "name": os.path.basename(self.abs_scope.rstrip(os.sep)) or self.root,
        }
