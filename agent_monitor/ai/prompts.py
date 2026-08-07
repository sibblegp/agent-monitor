"""System prompt and the forced-tool schema for changeset annotation.

Structured output comes from a *forced tool call* rather than
``output_config.format``: structured outputs aren't documented as supported on
Sonnet 4.6, whereas a forced tool call behaves identically on every model. That
keeps this correct if the model is switched in settings.
"""

from __future__ import annotations

SYSTEM = """You annotate code changesets for a live visualization.

You receive the symbols that changed in a repository, with their diffs. For each
one, produce a short factual summary and a risk assessment, then group the whole
changeset into a few named themes and write a brief review note.

Rules:
- Summaries are one short clause describing what the change *does*, in the
  imperative-free present tense. No preamble, no restating the symbol name.
  Good: "adds exponential backoff between retries"
  Bad:  "This function was changed to add retry logic."
- Risk reflects blast radius and subtlety, not size. A one-line change to auth
  or a signature every caller depends on is high. A new isolated helper is low.
- Themes group changes by intent, not by directory. 1-4 themes; every theme
  needs at least two members, so skip themes you can't fill.
- The review note is 2-3 sentences for someone about to review this diff: what
  changed overall and what deserves attention. No bullet points.
- Only reference symbols that appear in the input. Never invent identifiers.
- If a change is trivial or you cannot tell what it does, say so plainly rather
  than guessing.
"""

ANNOTATE_TOOL = {
    "name": "annotate_changeset",
    "description": (
        "Record summaries, risk levels, themes, and a review note for the "
        "changeset. Call this exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summaries": {
                "type": "array",
                "description": "One entry per changed symbol you can describe.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The node id, copied exactly."},
                        "text": {
                            "type": "string",
                            "description": "One short clause describing what the change does.",
                        },
                    },
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            },
            "risk": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "level": {"type": "string", "enum": ["low", "medium", "high"]},
                        "reason": {
                            "type": "string",
                            "description": "One clause justifying the level.",
                        },
                    },
                    "required": ["id", "level", "reason"],
                    "additionalProperties": False,
                },
            },
            "themes": {
                "type": "array",
                "description": "1-4 named groupings over the changeset.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Two or three words, lower case.",
                        },
                        "members": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Node ids belonging to this theme.",
                        },
                    },
                    "required": ["name", "members"],
                    "additionalProperties": False,
                },
            },
            "review_note": {
                "type": "string",
                "description": "2-3 sentences for a reviewer about to read this diff.",
            },
        },
        "required": ["summaries", "risk", "themes", "review_note"],
        "additionalProperties": False,
    },
}


NARRATE_SYSTEM = """You are narrating, live, what a coding agent is doing to a repository.

You get the symbols that changed since your previous entry, plus the last few
entries you wrote. Write the next entry in that running commentary.

This is a play-by-play, not a summary. Rules:

- Describe only what is NEW since your last entry. Never re-describe earlier
  work; the reader already saw it.
- Continue the thread. If your previous entry said a class was being added and
  now its callers changed, say it's being wired in — don't restart the story.
- Say what the agent appears to be *doing*, and why it follows from what came
  before. "adding retry handling to the cache layer" beats "modified 3 methods".
- Be concrete. Name the symbols and files involved.
- When the intent genuinely isn't clear from the diff, say so plainly and
  describe the mechanical change instead. Never invent a rationale.
- Never speculate about work that hasn't appeared in a diff yet.
- No preamble, no "it looks like the agent is". Just say what's happening.
"""

NARRATE_TOOL = {
    "name": "narrate_step",
    "description": "Record one entry in the running commentary. Call this exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "3-7 words, lower case, no trailing period. What just happened.",
            },
            "detail": {
                "type": "string",
                "description": (
                    "One or two sentences continuing the commentary. Concrete, "
                    "names the symbols involved, and connects to the previous entry "
                    "when there is a connection."
                ),
            },
            "phase": {
                "type": "string",
                "enum": [
                    "scaffolding",
                    "implementing",
                    "refactoring",
                    "wiring",
                    "testing",
                    "fixing",
                    "cleanup",
                    "docs",
                    "unclear",
                ],
                "description": "The kind of work this step represents.",
            },
        },
        "required": ["headline", "detail", "phase"],
        "additionalProperties": False,
    },
}


def build_narration(recent: list[dict], entries: list[dict]) -> str:
    """Prompt body: what you already said, then what's new."""
    parts: list[str] = []

    if recent:
        parts.append("Your previous entries, oldest first:")
        for item in recent:
            parts.append(f"  - {item['headline']}: {item['detail']}")
        parts.append("")
    else:
        parts.append("This is your first entry — there is no prior commentary.\n")

    parts.append("Changed since your last entry:")
    parts.append(build_changes(entries))
    return "\n".join(parts)


def build_context(repo_name: str, langs: dict[str, int], tree: list[str]) -> str:
    """Stable prefix describing the repo — cached across calls."""
    lang_summary = ", ".join(f"{k} ({v})" for k, v in sorted(langs.items()) if k != "other")
    listing = "\n".join(f"  {p}" for p in tree[:220])
    return (
        f"Repository: {repo_name}\n"
        f"Languages: {lang_summary or 'unknown'}\n"
        f"Files:\n{listing}\n"
    )


def build_changes(entries: list[dict]) -> str:
    """The volatile part of the prompt: what actually changed."""
    blocks = []
    for entry in entries:
        header = (
            f"[{entry['id']}]\n"
            f"  status: {entry['status']}\n"
            f"  symbol: {entry['kind']} {entry['qualname']} in {entry['path']}\n"
        )
        if entry.get("signature"):
            header += f"  signature: {entry['signature']}\n"
        diff = entry.get("diff") or "(no textual diff available)"
        blocks.append(f"{header}  diff:\n{_indent(diff)}")
    return "\n\n".join(blocks)


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())
