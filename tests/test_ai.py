"""AI layer behaviour, exercised against a stubbed client.

These never make a network call. They cover the properties that matter for cost
and correctness: opt-in, clean failure, no duplicate work, and a transcript that
narrates deltas rather than re-describing the same code.
"""

from __future__ import annotations

import subprocess
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_monitor.ai import AiAnnotator, AiUnavailable, Narrator  # noqa: E402
import agent_monitor.ai.narrator as narrator_module  # noqa: E402
from agent_monitor.config import Settings  # noqa: E402
from agent_monitor.engine import Engine  # noqa: E402


class _Block:
    type = "tool_use"

    def __init__(self, name, payload):
        self.name = name
        self.input = payload


class _Response:
    def __init__(self, name, payload):
        self.content = [_Block(name, payload)]
        self.usage = types.SimpleNamespace(input_tokens=500, output_tokens=40)


class _StubClient:
    """Records every request and answers with a valid forced-tool response."""

    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool = kwargs["tool_choice"]["name"]
        if tool == "narrate_step":
            return _Response(tool, {"headline": "did a thing", "detail": "d.", "phase": "implementing"})
        return _Response(tool, {"summaries": [], "risk": [], "themes": [], "review_note": "note"})

    def narrations(self):
        return [c for c in self.calls if c["tool_choice"]["name"] == "narrate_step"]


def _seeded_repo(tmp: Path) -> Path:
    root = tmp / "demo"
    root.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "T")
    (root / "app.py").write_text("def alpha():\n    return 1\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    return root


def _harness(tmp: Path):
    settings = Settings()
    settings.ai_enabled = True
    settings._session_key = "sk-ant-stub"
    stub = _StubClient()
    annotator = AiAnnotator(settings)
    annotator._client = lambda: stub
    narrator_module.MIN_INTERVAL_S = 0.0
    narrator = Narrator(settings, annotator)
    narrator.set_enabled(True)

    engine = Engine()
    engine.attach_ai(annotator, narrator)
    engine.open(str(_seeded_repo(tmp)))
    engine._stop_watcher()  # drive rescans explicitly so tests are deterministic
    return engine, narrator, stub, settings


def _observe(engine, narrator):
    langs: dict[str, int] = {}
    for parsed in engine.parsed.values():
        langs[parsed.lang] = langs.get(parsed.lang, 0) + 1
    narrator.observe(
        engine.target, engine.changeset, langs, sorted(engine.parsed),
        engine._read_side, engine._symbol_hashes(),
    )
    for _ in range(80):
        if not narrator._inflight:
            break
        time.sleep(0.05)
    time.sleep(0.1)


# ── opt-in and failure behaviour ──────────────────────────────────────


def test_annotation_requires_a_key():
    settings = Settings()
    settings.ai_enabled = True
    settings._session_key = None
    import os

    os.environ.pop("ANTHROPIC_API_KEY", None)
    annotator = AiAnnotator(settings)
    try:
        annotator._client()
    except AiUnavailable as exc:
        assert "key" in str(exc).lower()
    else:  # pragma: no cover
        raise AssertionError("expected AiUnavailable")


def test_narration_is_silent_while_disabled(tmp_path: Path):
    engine, narrator, stub, _ = _harness(tmp_path)
    narrator.set_enabled(False)
    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 2\n")
    engine.rescan()
    _observe(engine, narrator)
    assert stub.narrations() == []
    engine.close()


# ── transcript semantics ──────────────────────────────────────────────


def test_existing_changes_are_the_baseline_not_news(tmp_path: Path):
    """Opening a dirty repo must not open with commentary about old work."""
    engine, narrator, stub, _ = _harness(tmp_path)
    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 99\n")
    engine.rescan()
    _observe(engine, narrator)  # first observation — baseline
    assert stub.narrations() == []
    assert narrator.entries == []
    engine.close()


def test_first_change_after_the_baseline_is_narrated(tmp_path: Path):
    """Regression: the baseline used to swallow the first real edit."""
    engine, narrator, stub, _ = _harness(tmp_path)
    _observe(engine, narrator)  # baseline on a clean tree

    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 2\n")
    engine.rescan()
    _observe(engine, narrator)

    assert len(stub.narrations()) == 1
    assert len(narrator.entries) == 1
    engine.close()


def test_unchanged_rescan_costs_nothing(tmp_path: Path):
    engine, narrator, stub, _ = _harness(tmp_path)
    _observe(engine, narrator)
    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 2\n")
    engine.rescan()
    _observe(engine, narrator)
    before = len(stub.narrations())

    engine.rescan()
    _observe(engine, narrator)
    assert len(stub.narrations()) == before
    engine.close()


def test_prior_entries_are_fed_back_for_continuity(tmp_path: Path):
    engine, narrator, stub, _ = _harness(tmp_path)
    _observe(engine, narrator)
    app = Path(engine.target.root) / "app.py"

    for body in ("return 2", "return 3"):
        app.write_text(f"def alpha():\n    {body}\n")
        engine.rescan()
        _observe(engine, narrator)

    prompt = stub.narrations()[-1]["messages"][0]["content"]
    assert "Your previous entries" in prompt
    engine.close()


def test_second_edit_diffs_against_the_last_entry(tmp_path: Path):
    """A follow-up edit must read as a follow-up, not a fresh introduction."""
    engine, narrator, stub, _ = _harness(tmp_path)
    _observe(engine, narrator)

    fresh = Path(engine.target.root) / "fresh.py"
    fresh.write_text("def beta():\n    return 1\n")
    engine.rescan()
    _observe(engine, narrator)

    fresh.write_text("def beta():\n    total = 0\n    for i in range(3):\n        total += i\n    return total\n")
    engine.rescan()
    _observe(engine, narrator)

    prompt = stub.narrations()[-1]["messages"][0]["content"]
    assert "changed again since your last entry" in prompt
    assert "as you last described it" in prompt
    assert "+def beta" not in prompt  # not re-introduced
    engine.close()


# ── request shape ─────────────────────────────────────────────────────


def test_requests_are_cheap_and_deterministic(tmp_path: Path):
    engine, narrator, stub, settings = _harness(tmp_path)
    _observe(engine, narrator)
    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 5\n")
    engine.rescan()
    _observe(engine, narrator)

    call = stub.narrations()[-1]
    assert call["thinking"] == {"type": "disabled"}
    assert call["output_config"]["effort"] == "low"
    assert call["tool_choice"] == {"type": "tool", "name": "narrate_step"}
    assert call["model"] == settings.model
    # the repo listing is the cached prefix; only the diff varies per call
    assert any(block.get("cache_control") for block in call["system"])
    engine.close()


def test_annotations_merge_into_the_graph(tmp_path: Path):
    engine, _narrator, _stub, _ = _harness(tmp_path)
    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 7\n")
    engine.rescan()

    node_id = next(
        c.node_id for c in engine.changeset.symbols if c.qualname == "alpha"
    )
    engine.apply_ai(
        {
            "summaries": [{"id": node_id, "text": "returns a constant"}],
            "risk": [{"id": node_id, "level": "low", "reason": "isolated"}],
            "themes": [{"name": "tweaks", "members": [node_id]}],
            "review_note": "small change",
        }
    )
    node = engine.graph.nodes[node_id]
    assert node.summary == "returns a constant"
    assert node.risk == "low"
    assert node.theme == "tweaks"
    assert "ai" in engine.snapshot()
    engine.close()


# ── annotations must survive graph rebuilds ───────────────────────────


def test_annotations_survive_a_rescan(tmp_path: Path):
    """Regression: `rescan()` rebuilds the graph, which used to wipe every
    annotation — so a synopsis appeared and then silently vanished."""
    engine, _narrator, _stub, _ = _harness(tmp_path)
    app = Path(engine.target.root) / "app.py"
    app.write_text("def alpha():\n    return 7\n")
    engine.rescan()

    node_id = next(c.node_id for c in engine.changeset.symbols if c.qualname == "alpha")
    engine.apply_ai({"summaries": [{"id": node_id, "text": "returns a constant"}],
                     "risk": [], "themes": [], "review_note": ""})
    assert engine.graph.nodes[node_id].summary == "returns a constant"

    # something else changes -> full rebuild
    (Path(engine.target.root) / "other.py").write_text("def beta():\n    return 1\n")
    engine.rescan()

    assert engine.graph.nodes[node_id].summary == "returns a constant"
    engine.close()


def test_changed_files_get_a_derived_synopsis(tmp_path: Path):
    """Hovering a changed *file* should say something, not just its symbols."""
    engine, _narrator, _stub, _ = _harness(tmp_path)
    app = Path(engine.target.root) / "app.py"
    app.write_text("def alpha():\n    return 7\n\ndef gamma():\n    return 8\n")
    engine.rescan()

    ids = {c.qualname: c.node_id for c in engine.changeset.symbols}
    engine.apply_ai({
        "summaries": [
            {"id": ids["alpha"], "text": "returns a constant"},
            {"id": ids["gamma"], "text": "adds a second helper"},
        ],
        "risk": [{"id": ids["gamma"], "level": "high", "reason": "widely called"}],
        "themes": [], "review_note": "",
    })

    file_node = engine.graph.nodes["file:app.py"]
    assert file_node.summary, "changed file should carry a derived synopsis"
    assert "constant" in file_node.summary or "helper" in file_node.summary
    # worst child risk rolls up so a risky change is visible while collapsed
    assert file_node.risk == "high"
    engine.close()
