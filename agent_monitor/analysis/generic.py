"""Fallback parser for languages we don't understand structurally.

Produces a file-level node with no symbols, so an unsupported language still
shows up in the graph with its add/modify/delete status rather than vanishing.
"""

from __future__ import annotations

from ..model import ParsedFile


def count_loc(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def parse_generic(path: str, text: str, lang: str = "text") -> ParsedFile:
    return ParsedFile(path=path, lang=lang, loc=count_loc(text))
