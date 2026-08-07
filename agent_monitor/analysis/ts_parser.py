"""JavaScript / TypeScript analysis via tree-sitter.

Mirrors what `python_parser` extracts — symbols with signatures and semantic
body hashes, an import alias map, class bases, and call sites attributed to
their enclosing symbol — so the rest of the pipeline treats JS/TS exactly like
Python.

Grammars load lazily and this module is imported behind a guard, so a missing
`tree-sitter-language-pack` degrades JS/TS to file-level nodes rather than
breaking the app.
"""

from __future__ import annotations

import hashlib
import re

from tree_sitter_language_pack import get_parser

from ..model import ParsedFile, RawCall, Symbol
from .generic import count_loc

_PARSERS: dict[str, object] = {}

#: tree-sitter grammar to use per detected language.
_GRAMMAR = {
    "typescript": "tsx",  # tsx grammar is a superset; handles .ts and .tsx
    "javascript": "javascript",
}

_ENTRY_DECORATOR = re.compile(
    r"\b(route|get|post|put|patch|delete|command|task|handler|middleware)\b"
)

#: Node types that introduce a new named scope we care about.
_FUNCTIONISH = {
    "function_declaration",
    "function_expression",
    "generator_function_declaration",
    "method_definition",
    "arrow_function",
}


def _parser_for(lang: str):
    grammar = _GRAMMAR.get(lang, "javascript")
    if grammar not in _PARSERS:
        _PARSERS[grammar] = get_parser(grammar)
    return _PARSERS[grammar]


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _hash_node(node, src: bytes) -> str:
    """Structural fingerprint: node types only, so formatting is ignored.

    Walking the type sequence rather than the raw text means reindenting or
    adding comments doesn't register as a change, matching the Python side's
    AST-hash behaviour.
    """
    parts: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "comment":
            continue
        if current.child_count == 0:
            parts.append(f"{current.type}:{_text(current, src)}")
        else:
            parts.append(current.type)
            stack.extend(reversed(current.children))
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def _field(node, name: str):
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


def _name_of(node, src: bytes) -> str | None:
    ident = _field(node, "name")
    if ident is not None:
        return _text(ident, src)
    return None


def _signature(node, src: bytes) -> str:
    params = _field(node, "parameters")
    ret = _field(node, "return_type")
    sig = _text(params, src) if params is not None else "()"
    sig = " ".join(sig.split())
    if ret is not None:
        sig += f" {' '.join(_text(ret, src).split())}"
    return sig


def _callee_name(node, src: bytes) -> str | None:
    """Dotted text of a call target, or None if it isn't a plain path."""
    fn = _field(node, "function")
    if fn is None:
        return None
    if fn.type in ("identifier", "property_identifier"):
        return _text(fn, src)
    if fn.type == "member_expression":
        text = _text(fn, src)
        # Reject anything with computed access or calls inside it.
        if any(ch in text for ch in "()[]{}\n"):
            return None
        return text.strip()
    return None


class _Walk:
    def __init__(self, path: str, src: bytes) -> None:
        self.path = path
        self.src = src
        self.symbols: dict[str, Symbol] = {}
        self.imports: dict[str, str] = {}
        self.bases: dict[str, tuple[str, ...]] = {}
        self.calls: list[RawCall] = []

    # -- imports ---------------------------------------------------------

    def _handle_import(self, node) -> None:
        source = _field(node, "source")
        if source is None:
            return
        module = _text(source, self.src).strip("\"'`")
        clause = None
        for child in node.children:
            if child.type == "import_clause":
                clause = child
                break
        if clause is None:
            return

        for child in clause.children:
            if child.type == "identifier":  # default import
                self.imports[_text(child, self.src)] = module
            elif child.type == "namespace_import":
                ident = child.children[-1] if child.child_count else None
                if ident is not None:
                    self.imports[_text(ident, self.src)] = module
            elif child.type == "named_imports":
                for spec in child.children:
                    if spec.type != "import_specifier":
                        continue
                    alias = _field(spec, "alias")
                    name = _field(spec, "name")
                    if name is None:
                        continue
                    local = _text(alias or name, self.src)
                    self.imports[local] = f"{module}#{_text(name, self.src)}"

    # -- definitions -----------------------------------------------------

    def _add(self, node, name: str, kind: str, stack: list[str]) -> str:
        parent = ".".join(stack) if stack else None
        qualname = f"{parent}.{name}" if parent else name
        decorators = tuple(
            _text(child, self.src)
            for child in (node.parent.children if node.parent else [])
            if child.type == "decorator"
        )
        self.symbols[qualname] = Symbol(
            qualname=qualname,
            name=name,
            kind=kind,  # type: ignore[arg-type]
            lineno=node.start_point[0] + 1,
            end_lineno=node.end_point[0] + 1,
            signature=_signature(node, self.src) if kind != "class" else "",
            body_hash=_hash_node(node, self.src),
            parent=parent,
            decorators=decorators,
            is_entry=(
                name in ("main", "default")
                or name.startswith("test")
                or any(_ENTRY_DECORATOR.search(d) for d in decorators)
            ),
        )
        return qualname

    def visit(self, node, stack: list[str] | None = None) -> None:
        stack = stack or []

        if node.type in ("import_statement",):
            self._handle_import(node)

        elif node.type == "class_declaration":
            name = _name_of(node, self.src) or "default"
            qual = self._add(node, name, "class", stack)
            heritage = _field(node, "superclass") or next(
                (c for c in node.children if c.type == "class_heritage"), None
            )
            if heritage is not None:
                text = _text(heritage, self.src).replace("extends", "").strip()
                base = text.split("<")[0].split(",")[0].strip()
                if base:
                    self.bases[qual] = (base,)
            for child in node.children:
                self.visit(child, stack + [name])
            return

        elif node.type in ("function_declaration", "generator_function_declaration"):
            name = _name_of(node, self.src)
            if name:
                self._add(node, name, "function", stack)
                for child in node.children:
                    self.visit(child, stack + [name])
                return

        elif node.type == "method_definition":
            name_node = _field(node, "name")
            if name_node is not None:
                name = _text(name_node, self.src)
                kind = "method" if stack else "function"
                self._add(node, name, kind, stack)
                for child in node.children:
                    self.visit(child, stack + [name])
                return

        elif node.type == "variable_declarator":
            # `const foo = () => {}` / `const foo = function () {}`
            value = _field(node, "value")
            name_node = _field(node, "name")
            if (
                value is not None
                and name_node is not None
                and value.type in ("arrow_function", "function_expression")
            ):
                name = _text(name_node, self.src)
                self._add(value, name, "method" if stack else "function", stack)
                for child in value.children:
                    self.visit(child, stack + [name])
                return

        elif node.type == "call_expression":
            target = _callee_name(node, self.src)
            if target:
                self.calls.append(
                    RawCall(
                        caller=".".join(stack),
                        target=target,
                        lineno=node.start_point[0] + 1,
                    )
                )

        for child in node.children:
            self.visit(child, stack)


def parse_ts(path: str, text: str, lang: str = "typescript") -> ParsedFile:
    """Parse a JS/TS source file into the shared ParsedFile shape."""
    parsed = ParsedFile(path=path, lang=lang, loc=count_loc(text))
    src = text.encode("utf-8", "replace")

    try:
        tree = _parser_for(lang).parse(src)
    except Exception as exc:
        parsed.error = f"{type(exc).__name__}: {exc}"
        return parsed

    walker = _Walk(path, src)
    try:
        walker.visit(tree.root_node)
    except RecursionError:
        parsed.error = "RecursionError while walking syntax tree"
        return parsed

    parsed.symbols = walker.symbols
    parsed.imports = walker.imports
    parsed.bases = walker.bases
    parsed.calls = walker.calls
    return parsed
