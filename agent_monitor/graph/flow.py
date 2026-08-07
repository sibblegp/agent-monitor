"""Build the static call graph.

Resolution is heuristic but deliberately conservative: a call edge is only
emitted when we can name a concrete target. Anything we can't resolve to an
in-repo symbol collapses into a single ``ext:<toplevel>`` node per package, so
the external surface stays visible without a thousand leaf nodes.

Entry points anchor the left edge of the flow layout: `main`, `__main__` blocks,
route/command decorators, `test_*`, plus any in-repo function nobody calls.
"""

from __future__ import annotations

import builtins
import posixpath

from ..model import Edge, Graph, Node, ParsedFile, RepoTarget, ext_id, file_id, sym_id

_BUILTINS = frozenset(dir(builtins)) | {
    "self",
    "cls",
    "super",
    "print",
    "range",
    "len",
    "int",
    "str",
    "list",
    "dict",
    "set",
    "tuple",
    "bool",
    "float",
    "type",
    "isinstance",
    "hasattr",
    "getattr",
    "setattr",
}

_MAX_BASE_DEPTH = 3


def _module_names(path: str) -> list[str]:
    """Dotted module names a Python file can plausibly be imported as.

    Registers the full path plus every suffix, so `src/pkg/mod.py` resolves for
    both `src.pkg.mod` and `pkg.mod` without needing to know the layout.
    """
    if not path.endswith((".py", ".pyi")):
        return []
    stem = path.rsplit(".", 1)[0]
    parts = stem.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    return [".".join(parts[i:]) for i in range(len(parts))]


class _Resolver:
    def __init__(self, parsed: dict[str, ParsedFile]) -> None:
        self.parsed = parsed
        # dotted module name -> file path (shortest path wins on collision)
        self.modules: dict[str, str] = {}
        for path in sorted(parsed):
            for name in _module_names(path):
                self.modules.setdefault(name, path)

    # -- helpers ---------------------------------------------------------

    def _resolve_relative(self, dotted: str, from_path: str) -> str:
        """Turn `..pkg.thing` into an absolute dotted name for `from_path`."""
        level = len(dotted) - len(dotted.lstrip("."))
        rest = dotted[level:]
        pkg_parts = posixpath.dirname(from_path).split("/") if posixpath.dirname(from_path) else []
        # level 1 == current package, each extra level walks one up
        up = level - 1
        if up > 0:
            pkg_parts = pkg_parts[:-up] if up <= len(pkg_parts) else []
        base = ".".join(p for p in pkg_parts if p)
        return f"{base}.{rest}" if base and rest else (rest or base)

    def resolve_dotted(self, dotted: str) -> tuple[str, str | None] | None:
        """Map a dotted name onto (file path, qualname|None) inside the repo."""
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            module = ".".join(parts[:i])
            path = self.modules.get(module)
            if path is None:
                continue
            rest = ".".join(parts[i:])
            if not rest:
                return path, None
            symbols = self.parsed[path].symbols
            if rest in symbols:
                return path, rest
            # `Class.method` where only `Class` is known, or a module attribute
            head = rest.split(".", 1)[0]
            if head in symbols:
                return path, head
            return path, None
        return None

    def method_on(
        self, path: str, class_qual: str, attr: str, depth: int = 0
    ) -> tuple[str, str] | None:
        """Find `attr` on a class, following in-repo base classes."""
        if depth > _MAX_BASE_DEPTH:
            return None
        pf = self.parsed.get(path)
        if pf is None:
            return None
        candidate = f"{class_qual}.{attr}"
        if candidate in pf.symbols:
            return path, candidate
        for base in pf.bases.get(class_qual, ()):
            target = self._locate_class(pf, base)
            if target is None:
                continue
            base_path, base_qual = target
            found = self.method_on(base_path, base_qual, attr, depth + 1)
            if found:
                return found
        return None

    def _locate_class(self, pf: ParsedFile, name: str) -> tuple[str, str] | None:
        """Find a class by the name used in a `class X(Base)` clause."""
        if name in pf.symbols and pf.symbols[name].kind == "class":
            return pf.path, name
        head = name.split(".", 1)[0]
        dotted = pf.imports.get(head)
        if dotted is None:
            return None
        if dotted.startswith("."):
            dotted = self._resolve_relative(dotted, pf.path)
        tail = name.split(".", 1)[1] if "." in name else ""
        full = f"{dotted}.{tail}" if tail else dotted
        hit = self.resolve_dotted(full)
        if hit and hit[1]:
            return hit[0], hit[1]
        return None


def build_flow(
    target: RepoTarget,
    parsed: dict[str, ParsedFile],
    graph: Graph,
) -> Graph:
    """Add `calls` edges and `ext:` nodes to an existing structure graph."""
    resolver = _Resolver(parsed)
    ext_seen: set[str] = set()
    in_repo_callees: set[str] = set()

    for path in sorted(parsed):
        pf = parsed[path]
        if not pf.calls:
            continue
        fid = file_id(path)

        for call in pf.calls:
            caller_id = sym_id(path, call.caller) if call.caller else fid
            if caller_id not in graph.nodes:
                caller_id = fid
            if caller_id not in graph.nodes:
                continue

            target_id = _resolve_call(resolver, pf, call.target, call.caller)
            if target_id is None:
                continue

            if target_id.startswith("ext:"):
                if target_id not in ext_seen:
                    ext_seen.add(target_id)
                    graph.add_node(
                        Node(
                            id=target_id,
                            kind="external",
                            name=target_id.removeprefix("ext:"),
                            size=1,
                        )
                    )
            elif target_id not in graph.nodes:
                continue
            else:
                in_repo_callees.add(target_id)

            if target_id != caller_id:
                graph.add_edge(caller_id, target_id, "calls")

    _mark_root_entries(graph, in_repo_callees)
    return graph


def _resolve_call(
    resolver: _Resolver, pf: ParsedFile, dotted: str, caller_qual: str
) -> str | None:
    """Resolve one call site to a node id, or None to drop it."""
    head, _, rest = dotted.partition(".")

    # 1. self.foo() / cls.foo() -> a method on the enclosing class (or a base)
    if head in ("self", "cls") and rest and caller_qual:
        class_qual = caller_qual.rsplit(".", 1)[0] if "." in caller_qual else None
        if class_qual and class_qual in pf.symbols:
            attr = rest.split(".", 1)[0]
            found = resolver.method_on(pf.path, class_qual, attr)
            if found:
                return sym_id(found[0], found[1])
        return None

    if head in ("self", "cls"):
        return None

    # 2. a symbol defined in this same file
    if dotted in pf.symbols:
        return sym_id(pf.path, dotted)
    if head in pf.symbols:
        if rest:
            nested = f"{head}.{rest.split('.', 1)[0]}"
            if nested in pf.symbols:
                return sym_id(pf.path, nested)
        return sym_id(pf.path, head)

    # 3. follow an import alias
    alias = pf.imports.get(head)
    if alias is not None:
        if alias.startswith("."):
            alias = resolver._resolve_relative(alias, pf.path)
        full = f"{alias}.{rest}" if rest else alias
        hit = resolver.resolve_dotted(full)
        if hit is not None:
            path, qual = hit
            return sym_id(path, qual) if qual else file_id(path)
        top = alias.split(".", 1)[0]
        if top and top not in _BUILTINS:
            return ext_id(top)
        return None

    # 4. an absolute dotted path that happens to name an in-repo module
    hit = resolver.resolve_dotted(dotted)
    if hit is not None:
        path, qual = hit
        return sym_id(path, qual) if qual else file_id(path)

    # 5. builtins are noise; anything else is an unimported external name
    if head in _BUILTINS:
        return None
    return None


def _mark_root_entries(graph: Graph, in_repo_callees: set[str]) -> None:
    """Uncalled module-level functions are roots of the flow layout.

    Deliberately restricted to plain functions. Methods are reached by dynamic
    dispatch far more often than by a name we can resolve statically, so
    treating every uncalled method as an entry point would drag `__init__` and
    most of the codebase into the left-hand column and make the pane useless.
    Methods that genuinely *are* entry points (routes, tests, CLI commands) are
    already flagged by the parser from their decorators or names.
    """
    for node in graph.nodes.values():
        if node.kind != "function":
            continue
        if node.name.startswith("__") and node.name.endswith("__"):
            continue
        if node.id not in in_repo_callees:
            node.is_entry = True


def focus_subgraph(
    graph: Graph, seeds: set[str], depth: int = 2
) -> tuple[set[str], set[str]]:
    """Node and edge ids within `depth` call hops of `seeds`, both directions."""
    out_adj: dict[str, list[tuple[str, str]]] = {}
    in_adj: dict[str, list[tuple[str, str]]] = {}
    for edge in graph.edges.values():
        if edge.kind != "calls":
            continue
        out_adj.setdefault(edge.src, []).append((edge.dst, edge.id))
        in_adj.setdefault(edge.dst, []).append((edge.src, edge.id))

    nodes = {s for s in seeds if s in graph.nodes}
    edges: set[str] = set()
    frontier = set(nodes)
    for _ in range(max(0, depth)):
        nxt: set[str] = set()
        for node_id in frontier:
            for neighbour, eid in out_adj.get(node_id, []):
                edges.add(eid)
                if neighbour not in nodes:
                    nxt.add(neighbour)
            for neighbour, eid in in_adj.get(node_id, []):
                edges.add(eid)
                if neighbour not in nodes:
                    nxt.add(neighbour)
        nodes |= nxt
        frontier = nxt
        if not frontier:
            break

    # keep only edges whose endpoints both survived
    edges = {
        e.id
        for e in graph.edges.values()
        if e.id in edges and e.src in nodes and e.dst in nodes
    }
    return nodes, edges
