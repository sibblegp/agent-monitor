"""Dispatch a file to the right parser based on its extension."""

from __future__ import annotations

from ..model import ParsedFile
from .generic import parse_generic
from .python_parser import parse_python

PYTHON_EXT = {".py", ".pyi", ".pyw"}
JS_EXT = {".js", ".jsx", ".mjs", ".cjs"}
TS_EXT = {".ts", ".tsx", ".mts", ".cts"}

TEXT_EXT = {
    ".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".scss", ".sass", ".less", ".sql", ".sh", ".bash", ".zsh",
    ".fish", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".swift", ".lua", ".vim", ".xml", ".svg", ".gradle", ".proto",
}

#: Set to a callable by ts_parser when tree-sitter is importable.
_ts_parse = None
_ts_reason: str | None = None


def _load_ts():
    """Import the tree-sitter parser lazily; record why if it isn't available."""
    global _ts_parse, _ts_reason
    if _ts_parse is not None or _ts_reason is not None:
        return _ts_parse
    try:
        from .ts_parser import parse_ts  # noqa: PLC0415

        _ts_parse = parse_ts
    except Exception as exc:  # ImportError, or a grammar failing to load
        _ts_reason = f"{type(exc).__name__}: {exc}"
    return _ts_parse


def ts_unavailable_reason() -> str | None:
    """Human-readable reason JS/TS is degraded, or None when it works."""
    _load_ts()
    return _ts_reason


def detect_lang(path: str) -> str:
    dot = path.rfind(".")
    ext = path[dot:].lower() if dot > 0 else ""
    if ext in PYTHON_EXT:
        return "python"
    if ext in TS_EXT:
        return "typescript"
    if ext in JS_EXT:
        return "javascript"
    if ext in TEXT_EXT:
        return "text"
    return "other"


def decode(raw: bytes) -> str | None:
    """Decode source bytes, or None if this looks like a binary file."""
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8-sig", errors="replace")


def parse_source(path: str, raw: bytes) -> ParsedFile:
    """Parse file content into a ParsedFile, never raising."""
    lang = detect_lang(path)
    text = decode(raw)
    if text is None:
        return ParsedFile(path=path, lang="binary", loc=0)

    if lang == "python":
        return parse_python(path, text)

    if lang in ("typescript", "javascript"):
        parser = _load_ts()
        if parser is not None:
            try:
                return parser(path, text, lang)
            except Exception as exc:
                out = parse_generic(path, text, lang)
                out.error = f"tree-sitter failed: {type(exc).__name__}: {exc}"
                return out
        return parse_generic(path, text, lang)

    return parse_generic(path, text, lang)
