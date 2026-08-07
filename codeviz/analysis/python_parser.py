"""Python analysis via the standard library `ast` module.

Extracts, per file: the symbol table (classes / functions / methods with
signatures and semantic body hashes), the import alias map, class bases, and
every call site attributed to its enclosing symbol. Cross-file call resolution
happens later in ``graph/flow.py``, which needs all files at once.

The body hash is ``sha1(ast.dump(node, include_attributes=False))``. Hashing the
AST rather than the source text means reformatting, comment edits, and line
moves do not register as changes — only semantics do. Docstrings *are* AST
nodes, so editing one is correctly reported as a modification.
"""

from __future__ import annotations

import ast
import hashlib
import re

from ..model import ParsedFile, RawCall, Symbol
from .generic import count_loc

#: Decorator names that mark a function as an application entry point.
_ENTRY_DECORATOR = re.compile(
    r"\b(route|get|post|put|patch|delete|head|options|command|group|task|"
    r"callback|websocket|on_event|entrypoint|main)\b"
)


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _body_hash(node: ast.AST) -> str:
    """Semantic fingerprint of a definition.

    For functions this is the full AST dump. For classes we deliberately hash a
    *shallow* view — bases, decorators, class-level statements, and the list of
    member names — so that editing a method body marks only that method as
    modified rather than lighting up its whole class as well. Adding, removing,
    or renaming a member still changes the class hash, because the member list
    is part of it.
    """
    if isinstance(node, ast.ClassDef):
        parts: list[str] = [
            f"bases={[_safe_unparse(b) for b in node.bases]}",
            f"keywords={[_safe_unparse(k) for k in node.keywords]}",
            f"decorators={[_safe_unparse(d) for d in node.decorator_list]}",
        ]
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parts.append(f"member:{stmt.name}")
            else:
                parts.append(ast.dump(stmt, include_attributes=False))
        return _hash("\n".join(parts))
    return _hash(ast.dump(node, include_attributes=False))


def _safe_unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - unparse is very robust in 3.10+
        return ""


def _signature(node: ast.AST) -> str:
    """A stable rendering of the public shape of a definition."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = _safe_unparse(node.args)
        ret = _safe_unparse(node.returns)
        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        return f"{prefix}({args})" + (f" -> {ret}" if ret else "")
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(_safe_unparse(b) for b in node.bases)
        return f"({bases})" if bases else "()"
    return ""


def _decorators(node: ast.AST) -> tuple[str, ...]:
    raw = getattr(node, "decorator_list", []) or []
    return tuple(_safe_unparse(d) for d in raw)


def dotted_name(node: ast.AST) -> str | None:
    """Render `a.b.c` from an expression, or None if it isn't a plain dotted path."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


class _Walker(ast.NodeVisitor):
    """Single pass collecting symbols, imports, bases, and call sites."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.symbols: dict[str, Symbol] = {}
        self.imports: dict[str, str] = {}
        self.bases: dict[str, tuple[str, ...]] = {}
        self.calls: list[RawCall] = []
        self._stack: list[tuple[str, str]] = []  # (qualname, kind)

    # -- scope helpers ---------------------------------------------------

    @property
    def _qual_prefix(self) -> str:
        return ".".join(name for name, _ in self._stack)

    @property
    def _enclosing_symbol(self) -> str:
        """Innermost def/class that a call should be attributed to."""
        return self._qual_prefix

    # -- imports ---------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.imports[alias.asname] = alias.name
            else:
                # `import a.b.c` binds only the top-level name `a`
                top = alias.name.split(".", 1)[0]
                self.imports[top] = top
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        # Relative imports are recorded with leading dots preserved; the flow
        # builder resolves them against the importing file's package.
        prefix = "." * (node.level or 0)
        for alias in node.names:
            local = alias.asname or alias.name
            if alias.name == "*":
                continue
            self.imports[local] = f"{prefix}{module}.{alias.name}" if module else f"{prefix}{alias.name}"
        self.generic_visit(node)

    # -- definitions -----------------------------------------------------

    def _add_definition(self, node: ast.AST, kind: str) -> str:
        name = getattr(node, "name", "?")
        parent_qual = self._qual_prefix or None
        parent_kind = self._stack[-1][1] if self._stack else None
        qualname = f"{parent_qual}.{name}" if parent_qual else name

        resolved_kind = "method" if (kind == "function" and parent_kind == "class") else kind
        decorators = _decorators(node)

        symbol = Symbol(
            qualname=qualname,
            name=name,
            kind=resolved_kind,  # type: ignore[arg-type]
            lineno=getattr(node, "lineno", 1),
            end_lineno=getattr(node, "end_lineno", None) or getattr(node, "lineno", 1),
            signature=_signature(node),
            body_hash=_body_hash(node),
            parent=parent_qual,
            decorators=decorators,
            is_entry=self._is_entry(name, resolved_kind, decorators, parent_qual),
        )
        self.symbols[qualname] = symbol
        return qualname

    @staticmethod
    def _is_entry(
        name: str, kind: str, decorators: tuple[str, ...], parent: str | None
    ) -> bool:
        if kind == "class":
            return False
        if parent is None and name == "main":
            return True
        if name.startswith("test_"):
            return True
        return any(_ENTRY_DECORATOR.search(d) for d in decorators)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualname = self._add_definition(node, "class")
        self.bases[qualname] = tuple(
            n for n in (dotted_name(b) for b in node.bases) if n
        )
        self._stack.append((node.name, "class"))
        self.generic_visit(node)
        self._stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._add_definition(node, "function")
        self._stack.append((node.name, "function"))
        self.generic_visit(node)
        self._stack.pop()

    visit_FunctionDef = _visit_function  # type: ignore[assignment]
    visit_AsyncFunctionDef = _visit_function  # type: ignore[assignment]

    # -- calls -----------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        target = dotted_name(node.func)
        if target:
            self.calls.append(
                RawCall(
                    caller=self._enclosing_symbol,  # "" means module level
                    target=target,
                    lineno=getattr(node, "lineno", 1),
                )
            )
        self.generic_visit(node)


def _mark_main_block_entries(tree: ast.Module, walker: _Walker) -> None:
    """Functions invoked from `if __name__ == "__main__":` are entry points."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = _safe_unparse(node.test)
        if "__name__" not in test or "__main__" not in test:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                target = dotted_name(inner.func)
                if target and target in walker.symbols:
                    walker.symbols[target].is_entry = True


def parse_python(path: str, text: str) -> ParsedFile:
    """Parse one Python file. Syntax errors yield a file node with `error` set."""
    parsed = ParsedFile(path=path, lang="python", loc=count_loc(text))
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        parsed.error = f"{type(exc).__name__}: {exc}"
        return parsed

    walker = _Walker(path)
    try:
        walker.visit(tree)
        _mark_main_block_entries(tree, walker)
    except RecursionError:  # pathologically nested source
        parsed.error = "RecursionError while walking AST"
        return parsed

    parsed.symbols = walker.symbols
    parsed.imports = walker.imports
    parsed.bases = walker.bases
    parsed.calls = walker.calls
    return parsed
