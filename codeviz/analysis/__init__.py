"""Language analysis: source text in, symbols and call sites out."""

from .registry import detect_lang, parse_source

__all__ = ["detect_lang", "parse_source"]
