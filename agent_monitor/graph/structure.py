"""Build the containment graph: root → directories → files → classes → functions."""

from __future__ import annotations

import posixpath

from ..model import (
    CHANGED_STATUSES,
    ChangeSet,
    Graph,
    Node,
    ParsedFile,
    RepoTarget,
    dir_id,
    file_id,
    sym_id,
)

ROOT_ID = "root"

#: Precedence when rolling a mixed set of child statuses up to a parent.
_STATUS_RANK = {
    "unchanged": 0,
    "moved": 1,
    "renamed": 2,
    "removed": 3,
    "modified": 4,
    "signature_changed": 5,
    "added": 6,
}


def _rank(status: str, default: int = 0) -> int:
    return _STATUS_RANK.get(status, default)


def _ensure_dirs(graph: Graph, relpath: str, scope: str) -> str:
    """Create every directory node above `relpath`; return the immediate parent id.

    Directories above the analyzed scope are not created, so opening a
    subdirectory of a large repo doesn't add its ancestors to the graph.
    """
    parent_rel = posixpath.dirname(relpath)
    if not parent_rel or (scope and not (parent_rel == scope or parent_rel.startswith(scope + "/"))):
        return ROOT_ID

    parts = parent_rel.split("/")
    current = ""
    parent_id = ROOT_ID
    for part in parts:
        current = f"{current}/{part}" if current else part
        if scope and not (current == scope or current.startswith(scope + "/")):
            # still descending toward the scope root; don't materialize it
            continue
        node_id = dir_id(current)
        if node_id not in graph.nodes:
            graph.add_node(
                Node(
                    id=node_id,
                    kind="dir",
                    name=part,
                    path=current,
                    parent=parent_id,
                    size=0,
                )
            )
            graph.add_edge(parent_id, node_id, "contains")
        parent_id = node_id
    return parent_id


def build_structure(
    target: RepoTarget,
    parsed: dict[str, ParsedFile],
    changeset: ChangeSet,
) -> Graph:
    """Assemble the structure graph for the current snapshot."""
    graph = Graph()
    graph.add_node(
        Node(
            id=ROOT_ID,
            kind="root",
            name=target.to_dict()["name"],
            path=target.scope or "",
            size=0,
        )
    )

    file_status = changeset.file_statuses()
    symbol_changes = changeset.symbol_statuses()
    file_stats = {fc.path: (fc.added, fc.removed) for fc in changeset.files}

    # ---- files and their symbols -------------------------------------
    for path in sorted(parsed):
        pf = parsed[path]
        parent_id = _ensure_dirs(graph, path, target.scope)
        fid = file_id(path)
        status = file_status.get(path, "unchanged")
        added, removed = file_stats.get(path, (0, 0))

        graph.add_node(
            Node(
                id=fid,
                kind="file",
                name=posixpath.basename(path),
                status=status,
                path=path,
                parent=parent_id,
                lang=pf.lang,
                size=max(1, pf.loc),
                added=added,
                removed=removed,
            )
        )
        graph.add_edge(parent_id, fid, "contains")

        for symbol in pf.symbols.values():
            node_id = sym_id(path, symbol.qualname)
            sym_parent = (
                sym_id(path, symbol.parent) if symbol.parent else fid
            )
            change = symbol_changes.get(node_id)
            sym_status = change.status if change else "unchanged"
            if sym_status == "moved":
                sym_status = "unchanged"

            graph.add_node(
                Node(
                    id=node_id,
                    kind=symbol.kind,
                    name=symbol.name,
                    status=sym_status,
                    path=path,
                    parent=sym_parent,
                    qualname=symbol.qualname,
                    lang=pf.lang,
                    line=symbol.lineno,
                    end_line=symbol.end_lineno,
                    size=symbol.loc,
                    added=change.added if change else 0,
                    removed=change.removed if change else 0,
                    is_entry=symbol.is_entry,
                    signature=symbol.signature,
                )
            )

        # Parent edges are added after all symbols exist, so a method whose
        # class appears later in the dict still links correctly.
        for symbol in pf.symbols.values():
            node_id = sym_id(path, symbol.qualname)
            sym_parent = sym_id(path, symbol.parent) if symbol.parent else fid
            if sym_parent not in graph.nodes:
                sym_parent = fid
                graph.nodes[node_id].parent = fid
            graph.add_edge(sym_parent, node_id, "contains")

    # ---- ghosts: things that existed only on the old side --------------
    for fc in changeset.files:
        if fc.status != "removed" or fc.path in parsed:
            continue
        parent_id = _ensure_dirs(graph, fc.path, target.scope)
        fid = file_id(fc.path)
        graph.add_node(
            Node(
                id=fid,
                kind="file",
                name=posixpath.basename(fc.path),
                status="removed",
                path=fc.path,
                parent=parent_id,
                size=max(1, fc.removed),
                removed=fc.removed,
            )
        )
        graph.add_edge(parent_id, fid, "contains")

    for change in changeset.symbols:
        if change.status != "removed":
            continue
        node_id = change.node_id
        if node_id in graph.nodes:
            continue
        fid = file_id(change.path)
        if fid not in graph.nodes:
            continue
        graph.add_node(
            Node(
                id=node_id,
                kind=change.kind,
                name=change.name,
                status="removed",
                path=change.path,
                parent=fid,
                qualname=change.qualname,
                line=change.line,
                size=max(1, change.removed),
                removed=change.removed,
            )
        )
        graph.add_edge(fid, node_id, "contains")

    _roll_up_sizes_and_status(graph)
    return graph


def _roll_up_sizes_and_status(graph: Graph) -> None:
    """Give directories a size and a status derived from their descendants.

    Lets the UI show a collapsed directory that still signals "something in
    here changed" without expanding it.
    """
    children: dict[str, list[str]] = {}
    for node in graph.nodes.values():
        if node.parent:
            children.setdefault(node.parent, []).append(node.id)

    def visit(node_id: str) -> tuple[int, str]:
        node = graph.nodes[node_id]
        kid_ids = children.get(node_id, [])

        if node.kind not in ("root", "dir"):
            best = node.status
            holds_change = False
            for kid in kid_ids:
                _, kid_status = visit(kid)
                if kid_status in CHANGED_STATUSES:
                    holds_change = True
                if _rank(kid_status) > _rank(best):
                    best = kid_status

            # A class whose *members* changed is untouched at the AST level, and
            # the symbol diff rightly keeps saying so. But it still has to be
            # drawn in the Changes view as the parent of its changed method, and
            # leaving it grey there reads as "this didn't change" sitting in a
            # view that promises only changes. Containers report that they hold
            # a change, exactly like directories do.
            if node.kind == "class" and holds_change and node.status not in CHANGED_STATUSES:
                node.status = "modified"

            return node.size, best

        total = 0
        statuses: list[str] = []
        for kid in kid_ids:
            size, status = visit(kid)
            total += size
            statuses.append(status)

        # A container reports that its *contents* changed — not the strongest
        # status among them. Taking the max meant a single new function made the
        # whole repository read as "added", which is simply wrong.
        changed = [s for s in statuses if s in CHANGED_STATUSES]
        if not changed:
            rolled = "unchanged"
        elif len(changed) == len(statuses) and all(s == "added" for s in changed):
            rolled = "added"  # genuinely a brand-new directory
        else:
            rolled = "modified"

        node.size = total
        node.status = rolled
        return total, rolled

    if ROOT_ID in graph.nodes:
        visit(ROOT_ID)
