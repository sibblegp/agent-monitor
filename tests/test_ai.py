"""AI layer behaviour, exercised against a stubbed client.

These never make a network call. They cover the properties that matter for cost
and correctness: opt-in, clean failure, no duplicate work, and a transcript that
narrates deltas rather than re-describing the same code.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
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


class _Delta:
    """One `input_json_delta` fragment of a streamed tool call."""

    type = "content_block_delta"

    def __init__(self, partial_json):
        self.delta = types.SimpleNamespace(type="input_json_delta", partial_json=partial_json)


class _Stream:
    """Context manager mimicking `client.messages.stream(...)`.

    Emits the tool-call JSON in small fragments so the partial-JSON reader and
    the delta callbacks are genuinely exercised, not stubbed past.
    """

    def __init__(self, payload, response):
        self._payload = payload
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        import json as _json

        text = _json.dumps(self._payload)
        for i in range(0, len(text), 7):
            yield _Delta(text[i : i + 7])

    def get_final_message(self):
        return self._response


class _StubClient:
    """Records every request and answers with a valid forced-tool response."""

    def __init__(self):
        self.calls = []
        self.messages = self

    def _payload_for(self, tool):
        if tool == "narrate_step":
            return {"headline": "did a thing", "detail": "d.", "phase": "implementing"}
        return {"summaries": [], "risk": [], "themes": [], "review_note": "note"}

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool = kwargs["tool_choice"]["name"]
        return _Response(tool, self._payload_for(tool))

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        tool = kwargs["tool_choice"]["name"]
        payload = self._payload_for(tool)
        return _Stream(payload, _Response(tool, payload))

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


def test_annotation_requires_a_key(monkeypatch):
    """Hermetic: a key saved on the developer's machine must not decide this."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings()
    settings.ai_enabled = True
    settings._session_key = None
    settings._stored_key = None
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


def test_pre_existing_work_gets_an_opening_entry(tmp_path: Path):
    """The change set is the diff against HEAD — git decides it, not the session.

    Work that was already uncommitted when the repo opened is still work the
    reader hasn't seen described, so it opens the transcript rather than being
    silently absorbed.
    """
    engine, narrator, stub, _ = _harness(tmp_path)
    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 99\n")
    engine.rescan()
    _observe(engine, narrator)

    assert len(stub.narrations()) == 1
    prompt = stub.narrations()[0]["messages"][0]["content"]
    assert "opening entry" in prompt
    assert "already uncommitted" in prompt
    assert len(narrator.entries) == 1
    engine.close()


def test_first_change_is_narrated(tmp_path: Path):
    """Regression: an early-return once swallowed the first real edit."""
    engine, narrator, stub, _ = _harness(tmp_path)
    _observe(engine, narrator)  # clean tree -> nothing to say

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


def test_requests_arriving_mid_flight_are_not_dropped(tmp_path: Path):
    """Regression: edits made during an in-flight call were silently discarded.

    A call takes seconds and agents write in bursts, so most edits land
    mid-flight. Dropping them left those symbols with no summary and nothing to
    retry them, which is why hovering a changed node often showed nothing.
    """
    engine, _narrator, stub, _ = _harness(tmp_path)
    annotator = engine.ai

    started = threading.Event()
    release = threading.Event()
    real_create = stub.create

    def slow_create(**kwargs):
        started.set()
        release.wait(5)
        return real_create(**kwargs)

    stub.create = slow_create

    app = Path(engine.target.root) / "app.py"
    app.write_text("def alpha():\n    return 2\n")
    engine.rescan()
    engine.request_ai()
    assert started.wait(5), "first call never started"

    # A second burst arrives while the first call is still running.
    (Path(engine.target.root) / "second.py").write_text("def beta():\n    return 3\n")
    engine.rescan()
    engine.request_ai()

    release.set()
    for _ in range(100):
        if not annotator._inflight and annotator._pending_args is None:
            break
        time.sleep(0.05)

    annotate_calls = [c for c in stub.calls if c["tool_choice"]["name"] == "annotate_changeset"]
    assert len(annotate_calls) >= 2, "the mid-flight request was dropped"

    sent = "\n".join(c["messages"][0]["content"] for c in annotate_calls)
    assert "second.py" in sent, "the symbol added mid-flight was never annotated"
    engine.close()


def test_narration_streams_progressively(tmp_path: Path):
    """The pane should fill in as the model writes, not all at once at the end."""
    engine, narrator, stub, _ = _harness(tmp_path)
    deltas: list[dict] = []
    narrator.on_delta = deltas.append

    _observe(engine, narrator)
    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 42\n")
    engine.rescan()
    _observe(engine, narrator)

    assert deltas, "no streaming callbacks fired"
    assert deltas[0]["state"] == "start"

    detail_states = [d.get("detail", "") for d in deltas if d["state"] == "delta"]
    assert len(detail_states) > 1, "detail arrived in a single lump, not progressively"
    # each update must extend the previous one, never rewrite it
    for earlier, later in zip(detail_states, detail_states[1:]):
        assert later.startswith(earlier), (earlier, later)
    assert detail_states[-1] == narrator.entries[0]["detail"]
    engine.close()


def test_unparsed_files_are_narrated(tmp_path: Path):
    """Regression: adding a shell script produced no transcript entry at all."""
    engine, narrator, stub, _ = _harness(tmp_path)
    _observe(engine, narrator)

    (Path(engine.target.root) / "deploy.sh").write_text("#!/bin/sh\necho deploying\n")
    engine.rescan()
    _observe(engine, narrator)

    assert len(stub.narrations()) == 1, "a non-Python file change was never narrated"
    prompt = stub.narrations()[-1]["messages"][0]["content"]
    assert "deploy.sh" in prompt
    engine.close()


# ── every changed symbol must eventually get a synopsis ───────────────


class _PartialClient(_StubClient):
    """Answers for only the first `answers` symbols of each request.

    Models routinely under-fill a long array. The annotator has to cope with
    that rather than treating silence as "analysed".
    """

    def __init__(self, answers=1):
        super().__init__()
        self.answers = answers
        self.asked = []  # ids requested, per call

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool = kwargs["tool_choice"]["name"]
        if tool != "annotate_changeset":
            return _Response(tool, self._payload_for(tool))
        text = kwargs["messages"][0]["content"]
        ids = re.findall(r"^\[([^\]]+)\]$", text, re.MULTILINE)
        self.asked.append(ids)
        return _Response(
            tool,
            {
                "summaries": [{"id": i, "text": f"about {i}"} for i in ids[: self.answers]],
                "risk": [],
                "themes": [],
                "review_note": "note",
            },
        )


def _annotate(engine, annotator):
    langs: dict[str, int] = {}
    for parsed in engine.parsed.values():
        langs[parsed.lang] = langs.get(parsed.lang, 0) + 1
    return annotator.annotate(
        engine.target, engine.changeset, langs, sorted(engine.parsed),
        engine._read_side, engine._symbol_hashes(),
    )


def test_symbols_the_model_skips_are_retried(tmp_path: Path):
    """Regression: a skipped symbol used to be cached as analysed-but-empty.

    It then matched the cache forever, so it was never re-requested and never
    had a summary to serve — the synopsis simply never appeared on hover.
    """
    engine, _narrator, _stub, settings = _harness(tmp_path)
    stub = _PartialClient(answers=1)
    annotator = AiAnnotator(settings)
    annotator._client = lambda: stub

    root = Path(engine.target.root)
    (root / "app.py").write_text(
        "def alpha():\n    return 2\n\ndef beta():\n    return 3\n\ndef gamma():\n    return 4\n"
    )
    engine.rescan()

    first = _annotate(engine, annotator)
    assert len(first["summaries"]) == 1, first["summaries"]

    # Nothing about the code changed, so a cache-only design would go quiet.
    # The undescribed symbols must be asked about again.
    second = _annotate(engine, annotator)
    assert len(stub.asked) >= 2, "skipped symbols were never re-requested"
    assert len(second["summaries"]) >= 2, "retry produced no additional synopsis"
    engine.close()


def test_large_changesets_are_batched(tmp_path: Path):
    """One giant request comes back partly unanswered, so split it up."""
    from agent_monitor.ai.client import SYMBOLS_PER_BATCH

    engine, _narrator, _stub, settings = _harness(tmp_path)
    stub = _PartialClient(answers=99)
    annotator = AiAnnotator(settings)
    annotator._client = lambda: stub

    count = SYMBOLS_PER_BATCH * 2 + 3
    body = "".join(f"def fn{i}():\n    return {i}\n\n" for i in range(count))
    (Path(engine.target.root) / "app.py").write_text(body)
    engine.rescan()

    result = _annotate(engine, annotator)
    assert len(stub.asked) == 3, f"expected 3 batches, got {len(stub.asked)}"
    assert all(len(ids) <= SYMBOLS_PER_BATCH for ids in stub.asked), [len(i) for i in stub.asked]
    # Every symbol asked about is described exactly once.
    assert len(result["summaries"]) == sum(len(ids) for ids in stub.asked)
    engine.close()


class _PrefixStrippingClient(_StubClient):
    """Answers with the node id minus its `sym:` prefix — what really happens.

    Sonnet treats `sym:` as a namespace marker and normalises it away, so every
    annotation keyed a node that doesn't exist and no synopsis ever appeared.
    """

    def __init__(self):
        super().__init__()
        self.asked = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool = kwargs["tool_choice"]["name"]
        if tool != "annotate_changeset":
            return _Response(tool, self._payload_for(tool))
        text = kwargs["messages"][0]["content"]
        blocks = re.findall(r"^\[([^\]]+)\]\n  status.*?\n  symbol: \S+ (\S+) in (\S+)", text, re.M | re.S)
        self.asked.append([b[0] for b in blocks])
        return _Response(
            tool,
            {
                # Deliberately NOT the token it was handed.
                "summaries": [{"id": f"{path}::{qual}", "text": f"about {qual}"} for _, qual, path in blocks],
                "risk": [],
                "themes": [],
                "review_note": "note",
            },
        )


def test_annotations_survive_a_reformatted_id(tmp_path: Path):
    """A recognisable-but-reformatted id must still land on its node."""
    engine, _narrator, _stub, settings = _harness(tmp_path)
    stub = _PrefixStrippingClient()
    annotator = AiAnnotator(settings)
    annotator._client = lambda: stub

    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 7\n")
    engine.rescan()

    result = _annotate(engine, annotator)
    ids = {s["id"] for s in result["summaries"]}
    assert ids, "no summaries came back at all"
    node_ids = set(engine.graph.nodes)
    assert ids <= node_ids, f"summaries keyed nodes that don't exist: {ids - node_ids}"

    engine.apply_ai(result)
    described = [n for n in engine.graph.nodes.values() if n.summary]
    assert described, "annotations never reached the graph"
    engine.close()


def test_symbols_are_referenced_by_opaque_token(tmp_path: Path):
    """The prompt must not hand the model an id it will want to tidy up."""
    engine, _narrator, _stub, settings = _harness(tmp_path)
    stub = _PartialClient(answers=99)
    annotator = AiAnnotator(settings)
    annotator._client = lambda: stub

    (Path(engine.target.root) / "app.py").write_text("def alpha():\n    return 7\n")
    engine.rescan()
    _annotate(engine, annotator)

    sent = stub.asked[0]
    assert sent, "nothing was sent"
    assert all(re.fullmatch(r"s\d+", ref) for ref in sent), sent
    engine.close()


def test_whole_file_units_get_a_real_diff():
    """Regression: an edit past the top of the file produced an empty diff.

    Whole-file units carry no line number, and the windowing path compared the
    first 60 lines of each side — so any edit below that looked like no change
    at all, and the synopsis came back as "no textual diff available".
    """
    from agent_monitor.ai.client import _slice_diff
    from agent_monitor.model import SymbolChange

    old = "\n".join(f"line {i}" for i in range(200))
    new = old.replace("line 150", "line 150 CHANGED HERE")
    change = SymbolChange(
        path="README.md", qualname="README.md", name="README.md",
        kind="file", status="modified", line=None, added=1, removed=1,
    )
    diff = _slice_diff(old, new, change)
    assert "CHANGED HERE" in diff, f"the actual edit is missing from the diff:\n{diff}"


def test_reference_tokens_never_reach_the_reader():
    """`s7` addresses a symbol in the protocol; it names nothing on screen."""
    from agent_monitor.ai.client import _scrub_refs

    assert _scrub_refs("adds retry logic (`s3`)") == "adds retry logic"
    assert _scrub_refs("touches the panel (`s4`, `s6`, `s12`)") == "touches the panel"
    assert _scrub_refs("check `s1` before merging") == "check before merging"
    # Real backticked code must survive untouched.
    assert _scrub_refs("adds a `_usage_lock` field") == "adds a `_usage_lock` field"
    assert _scrub_refs("renames `sha256` to `s3hash`") == "renames `sha256` to `s3hash`"
    assert _scrub_refs(None) is None


class _MalformedClient(_StubClient):
    """Answers with strings where the schema demanded objects.

    Observed for real on a branch review: one batch returned bare theme names
    and the AttributeError took down the whole review, losing the work the
    other batches had already done.
    """

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool = kwargs["tool_choice"]["name"]
        if tool != "annotate_changeset":
            return _Response(tool, self._payload_for(tool))
        text = kwargs["messages"][0]["content"]
        ids = re.findall(r"^\[([^\]]+)\]$", text, re.MULTILINE)
        return _Response(
            tool,
            {
                "summaries": [{"id": ids[0], "text": "fine"}, "not an object", None],
                "risk": ["also not an object"],
                "themes": ["bare theme name", {"name": "real", "members": ids[:1]}],
                "review_note": "note",
            },
        )


def test_a_malformed_batch_does_not_lose_the_whole_review(tmp_path: Path):
    engine, _narrator, _stub, settings = _harness(tmp_path)
    annotator = AiAnnotator(settings)
    annotator._client = lambda: _MalformedClient()

    (Path(engine.target.root) / "app.py").write_text(
        "def alpha():\n    return 2\n\ndef beta():\n    return 3\n"
    )
    engine.rescan()

    result = _annotate(engine, annotator)  # must not raise
    assert [s["text"] for s in result["summaries"]] == ["fine"]
    assert [t["name"] for t in result["themes"]] == ["real"]
    engine.close()
