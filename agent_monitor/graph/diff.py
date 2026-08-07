"""Symbol-level diffing — the feature that makes this more than a file watcher.

Instead of "this file changed", we parse *both* sides into symbol tables and
report exactly which functions, methods, and classes differ, and how:

    added              only on the new side
    removed            only on the old side
    signature_changed  params / return type / decorators differ (API-breaking)
    modified           body semantics differ
    moved              same semantics, different line — deliberately NOT a change

Because ``Symbol.body_hash`` hashes the AST rather than the source text,
reformatting and comment edits produce no changes at all, so a `black` run
doesn't light up the whole graph.
"""

from __future__ import annotations

import difflib

from ..model import ParsedFile, SymbolChange, Symbol


def _symbol_lines(text: str | None, symbol: Symbol) -> list[str]:
    if not text:
        return []
    lines = text.splitlines()
    start = max(0, symbol.lineno - 1)
    end = min(len(lines), symbol.end_lineno)
    return lines[start:end]


def _line_delta(old_lines: list[str], new_lines: list[str]) -> tuple[int, int]:
    """(added, removed) line counts between two blocks of source."""
    if not old_lines:
        return len(new_lines), 0
    if not new_lines:
        return 0, len(old_lines)
    added = removed = 0
    # NOTE: autojunk off keeps short symbols accurate
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return added, removed


def _api_shape(symbol: Symbol, strict: bool = True) -> tuple[str, tuple[str, ...]]:
    if not strict:
        return symbol.signature, ()
    return symbol.signature, symbol.decorators


def diff_file(
    path: str,
    old: ParsedFile | None,
    new: ParsedFile | None,
    *,
    old_text: str | None = None,
    new_text: str | None = None,
) -> list[SymbolChange]:
    """Classify every symbol difference between two versions of one file.

    Pass ``old=None`` for an added file and ``new=None`` for a removed one.
    """
    changes: list[SymbolChange] = []

    if new is None and old is None:
        return changes

    if old is None or not old.symbols:
        if new is None:
            return changes
        for symbol in new.symbols.values():
            changes.append(
                SymbolChange(
                    path=path,
                    qualname=symbol.qualname,
                    name=symbol.name,
                    kind=symbol.kind,
                    status="added",
                    line=symbol.lineno,
                    added=symbol.loc,
                )
            )
        return changes

    if new is None:
        for symbol in old.symbols.values():
            changes.append(
                SymbolChange(
                    path=path,
                    qualname=symbol.qualname,
                    name=symbol.name,
                    kind=symbol.kind,
                    status="removed",
                    line=symbol.lineno,
                    removed=symbol.loc,
                )
            )
        return changes

    old_syms, new_syms = old.symbols, new.symbols

    for qualname, symbol in new_syms.items():
        previous = old_syms.get(qualname)
        if previous is None:
            changes.append(
                SymbolChange(
                    path=path,
                    qualname=qualname,
                    name=symbol.name,
                    kind=symbol.kind,
                    status="added",
                    line=symbol.lineno,
                    added=symbol.loc,
                )
            )
            continue

        signature_differs = _api_shape(previous) != _api_shape(symbol)
        body_differs = previous.body_hash != symbol.body_hash

        if not signature_differs and not body_differs:
            # Same semantics. A pure line shift is a move, which we record but
            # never render as a change — it's noise from edits elsewhere.
            if previous.lineno != symbol.lineno:
                changes.append(
                    SymbolChange(
                        path=path,
                        qualname=qualname,
                        name=symbol.name,
                        kind=symbol.kind,
                        status="moved",
                        line=symbol.lineno,
                    )
                )
            continue

        added, removed = _line_delta(
            _symbol_lines(old_text, previous), _symbol_lines(new_text, symbol)
        )
        changes.append(
            SymbolChange(
                path=path,
                qualname=qualname,
                name=symbol.name,
                kind=symbol.kind,
                status="signature_changed" if signature_differs else "modified",
                line=symbol.lineno,
                added=added,
                removed=removed,
            )
        )

    for qualname, symbol in old_syms.items():
        if qualname not in new_syms:
            changes.append(
                SymbolChange(
                    path=path,
                    qualname=qualname,
                    name=symbol.name,
                    kind=symbol.kind,
                    status="removed",
                    line=symbol.lineno,
                    removed=symbol.loc,
                )
            )

    changes.sort(key=lambda c: (c.line or 0, c.qualname))
    return changes
