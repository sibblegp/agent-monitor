"""Analysis and diffing behaviour.

Plain asserts — run with `pytest`, or directly with `python tests/run_tests.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_monitor import gitutil  # noqa: E402
from agent_monitor.analysis import parse_source  # noqa: E402
from agent_monitor.analysis.registry import detect_lang, ts_unavailable_reason  # noqa: E402
from agent_monitor.graph.diff import diff_file  # noqa: E402


def _diff(old: str, new: str, path: str = "m.py"):
    po = parse_source(path, old.encode())
    pn = parse_source(path, new.encode())
    return {
        (c.status, c.qualname)
        for c in diff_file(path, po, pn, old_text=old, new_text=new)
    }


# ── the property the whole tool rests on ──────────────────────────────


def test_reformatting_is_not_a_change():
    """A formatter run must not light up the graph."""
    old = "def alpha(a, b=1):\n    return a + b\n"
    new = "# a new comment\ndef alpha(a,   b=1):\n\n    return a + b\n"
    changes = _diff(old, new)
    assert not any(s in ("modified", "signature_changed") for s, _ in changes), changes


def test_docstring_edit_is_a_change():
    """Docstrings are real AST nodes, so editing one *is* a modification."""
    old = 'def alpha():\n    """One."""\n    return 1\n'
    new = 'def alpha():\n    """Two."""\n    return 1\n'
    assert ("modified", "alpha") in _diff(old, new)


def test_signature_change_outranks_body_change():
    old = "def alpha(a):\n    return a\n"
    new = "def alpha(a, b=2):\n    return a + b\n"
    assert ("signature_changed", "alpha") in _diff(old, new)


def test_added_and_removed_symbols():
    old = "def a():\n    return 1\ndef b():\n    return 2\n"
    new = "def a():\n    return 1\ndef c():\n    return 3\n"
    changes = _diff(old, new)
    assert ("added", "c") in changes
    assert ("removed", "b") in changes


# ── class granularity ─────────────────────────────────────────────────

BASE_CLASS = "class T(B):\n    X = 1\n    def go(self):\n        return 1\n"


def test_editing_a_method_does_not_mark_its_class():
    new = BASE_CLASS.replace("return 1\n", "return 999\n")
    changes = _diff(BASE_CLASS, new)
    assert ("modified", "T.go") in changes
    assert not any(q == "T" for _, q in changes), changes


def test_class_attribute_edit_marks_the_class():
    new = BASE_CLASS.replace("X = 1", "X = 2")
    assert ("modified", "T") in _diff(BASE_CLASS, new)


def test_changing_a_base_class_is_a_signature_change():
    new = BASE_CLASS.replace("class T(B):", "class T(Other):")
    assert ("signature_changed", "T") in _diff(BASE_CLASS, new)


def test_adding_a_member_marks_the_class_and_the_member():
    new = BASE_CLASS + "    def extra(self):\n        pass\n"
    changes = _diff(BASE_CLASS, new)
    assert ("added", "T.extra") in changes
    assert ("modified", "T") in changes


# ── call resolution ───────────────────────────────────────────────────


def test_self_call_resolves_to_a_method():
    src = "class T:\n    def go(self):\n        return self.helper()\n    def helper(self):\n        return 1\n"
    parsed = parse_source("m.py", src.encode())
    targets = {(c.caller, c.target) for c in parsed.calls}
    assert ("T.go", "self.helper") in targets


def test_imports_are_recorded_with_aliases():
    src = "import os\nimport a.b as ab\nfrom x.y import z as zz\n"
    parsed = parse_source("m.py", src.encode())
    assert parsed.imports["os"] == "os"
    assert parsed.imports["ab"] == "a.b"
    assert parsed.imports["zz"] == "x.y.z"


def test_entry_points_are_detected():
    src = "def main():\n    helper()\n\ndef helper():\n    return 1\n\nif __name__ == '__main__':\n    main()\n"
    parsed = parse_source("m.py", src.encode())
    assert parsed.symbols["main"].is_entry
    assert not parsed.symbols["helper"].is_entry


def test_syntax_error_yields_a_file_node_not_an_exception():
    parsed = parse_source("bad.py", b"def oops(:\n")
    assert parsed.error is not None
    assert parsed.symbols == {}


def test_binary_content_is_not_parsed():
    parsed = parse_source("blob.py", b"\x00\x01\x02binary")
    assert parsed.lang == "binary"


# ── languages ─────────────────────────────────────────────────────────


def test_language_detection():
    assert detect_lang("a/b.py") == "python"
    assert detect_lang("a/b.tsx") == "typescript"
    assert detect_lang("a/b.mjs") == "javascript"
    assert detect_lang("a/b.rs") == "text"
    assert detect_lang("a/b.bin") == "other"


def test_javascript_symbols_when_tree_sitter_available():
    if ts_unavailable_reason() is not None:
        return  # optional extra not installed; file-level fallback is correct
    src = (
        "import { render } from './r.js';\n"
        "export class Widget extends Base {\n"
        "  draw(ctx) { return render(ctx); }\n"
        "}\n"
        "export function boot() { return new Widget(); }\n"
    )
    parsed = parse_source("app.ts", src.encode())
    quals = set(parsed.symbols)
    assert "Widget" in quals
    assert "Widget.draw" in quals
    assert "boot" in quals
    assert parsed.bases.get("Widget") == ("Base",)
    assert parsed.imports.get("render", "").startswith("./r.js")


# ── git plumbing ──────────────────────────────────────────────────────


def _demo_repo(tmp: Path) -> Path:
    root = tmp / "repo"
    root.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "T")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "src" / "keep me.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    return root


def test_rename_is_detected_as_rename(tmp_path: Path):
    root = _demo_repo(tmp_path)
    subprocess.run(["git", "-C", str(root), "mv", "src/a.py", "src/b.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "rename"], check=True, capture_output=True)
    target = gitutil.resolve_target(str(root))
    changes, _base = gitutil.changed_files(target, "commit", "HEAD")
    statuses = {(c.status, c.path, c.old_path) for c in changes}
    assert ("renamed", "src/b.py", "src/a.py") in statuses


def test_paths_with_spaces_survive(tmp_path: Path):
    root = _demo_repo(tmp_path)
    (root / "src" / "keep me.py").write_text("x = 2\n")
    target = gitutil.resolve_target(str(root))
    changes, _ = gitutil.changed_files(target, "live")
    assert any(c.path == "src/keep me.py" for c in changes)


def test_scope_limits_analysis_to_a_subtree(tmp_path: Path):
    root = _demo_repo(tmp_path)
    (root / "other").mkdir()
    (root / "other" / "z.py").write_text("z = 1\n")
    scoped = gitutil.resolve_target(str(root / "src"))
    assert scoped.scope == "src"
    files = gitutil.list_files(scoped)
    assert all(f.startswith("src/") for f in files), files


def test_root_commit_has_no_parent(tmp_path: Path):
    root = _demo_repo(tmp_path)
    target = gitutil.resolve_target(str(root))
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    changes, base = gitutil.changed_files(target, "commit", sha)
    assert base is None
    assert all(c.status == "added" for c in changes), changes


# ── unparsed languages must still be describable ──────────────────────


def test_changeset_units_cover_files_without_symbols(tmp_path: Path):
    """A shell script, README, or JSON config has no symbols to diff.

    Iterating `symbols` alone made every such change invisible to the AI layer —
    no narration, no hover summary. `units()` represents them as a single
    file-level unit instead.
    """
    from agent_monitor.engine import Engine

    root = _demo_repo(tmp_path)
    (root / "run.sh").write_text("#!/usr/bin/env bash\necho hi\n")
    (root / "README.md").write_text("# hello\n")
    (root / "src" / "a.py").write_text("def alpha():\n    return 2\n")

    engine = Engine()
    engine.open(str(root))
    engine._stop_watcher()

    units = engine.changeset.units()
    by_path = {u.path: u for u in units}

    assert "run.sh" in by_path, "shell script produced no describable unit"
    assert "README.md" in by_path, "markdown produced no describable unit"
    assert by_path["run.sh"].kind == "file"
    assert by_path["run.sh"].node_id == "file:run.sh"

    # A file that *did* yield symbol changes is represented by those symbols,
    # not duplicated as a whole-file unit.
    py_units = [u for u in units if u.path == "src/a.py"]
    assert py_units and all(u.kind != "file" for u in py_units), py_units
    engine.close()


def test_changes_view_contains_only_changed_things(tmp_path: Path):
    """The Changes view must not show unchanged code.

    It used to pull in two call-hops of context around every change, so a
    one-line edit dragged in its callers, their callers, and all of their
    parent directories — the view was mostly unchanged code.
    """
    from agent_monitor.engine import Engine
    from agent_monitor.model import CHANGED_STATUSES

    root = _demo_repo(tmp_path)
    # A caller chain that must NOT be dragged in by proximity.
    (root / "src" / "chain.py").write_text(
        "from a import alpha\n"
        "def one():\n    return alpha()\n"
        "def two():\n    return one()\n"
        "def three():\n    return two()\n"
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "chain"], check=True, capture_output=True)

    (root / "src" / "a.py").write_text("def alpha():\n    return 42\n")

    engine = Engine()
    engine.open(str(root))
    engine._stop_watcher()

    nodes = engine.focus()["nodes"]
    graph = engine.graph.nodes
    stray = [
        n for n in nodes
        if graph[n].status not in CHANGED_STATUSES and graph[n].kind != "root"
    ]
    assert not stray, f"unchanged nodes leaked into the changes view: {stray}"
    assert any(n.endswith("::alpha") for n in nodes), nodes
    assert not any("chain.py" in n for n in nodes), "unchanged callers were pulled in"
    engine.close()


def test_a_class_holding_a_changed_method_reads_as_changed(tmp_path: Path):
    """The class node is drawn as the method's parent, so it must not read grey."""
    from agent_monitor.engine import Engine
    from agent_monitor.model import CHANGED_STATUSES, sym_id

    root = _demo_repo(tmp_path)
    (root / "src" / "k.py").write_text("class T:\n    def go(self):\n        return 1\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "cls"], check=True, capture_output=True)

    (root / "src" / "k.py").write_text("class T:\n    def go(self):\n        return 999\n")

    engine = Engine()
    engine.open(str(root))
    engine._stop_watcher()

    cls = engine.graph.nodes[sym_id("src/k.py", "T")]
    assert cls.status in CHANGED_STATUSES, "class holding a changed method reads as unchanged"
    engine.close()


# ── review notes ──────────────────────────────────────────────────────


def test_against_mode_spans_a_branch_plus_the_working_tree(tmp_path: Path):
    """A review must cover committed *and* uncommitted work on the branch."""
    root = _demo_repo(tmp_path)
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)

    # `git init` picks the trunk name from config, so ask rather than assume.
    trunk = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run("checkout", "-qb", "feature")
    (root / "src" / "committed.py").write_text("def one():\n    return 1\n")
    run("add", "-A")
    run("commit", "-qm", "on the branch")
    (root / "src" / "dirty.py").write_text("def two():\n    return 2\n")

    target = gitutil.resolve_target(str(root))
    changes, base = gitutil.changed_files(target, "against", trunk)
    paths = {c.path for c in changes}
    assert "src/committed.py" in paths, "committed branch work is missing"
    assert "src/dirty.py" in paths, "uncommitted work is missing"
    assert base, "no merge base resolved"


def test_review_groups_notes_by_file(tmp_path: Path):
    from agent_monitor.engine import Engine

    root = _demo_repo(tmp_path)
    (root / "src" / "a.py").write_text("def alpha():\n    return 2\n\ndef beta():\n    return 3\n")
    (root / "notes.txt").write_text("hello\n")

    engine = Engine()
    engine.open(str(root))
    engine._stop_watcher()

    review = engine.review()
    by_path = {g["path"]: g for g in review["groups"]}
    assert set(by_path) == {"src/a.py", "notes.txt"}, by_path.keys()
    assert review["counts"]["files"] == 2

    names = {i["name"] for i in by_path["src/a.py"]["items"]}
    assert "beta" in names, names
    # An unparsed file is its own unit, not a container of symbols.
    assert by_path["notes.txt"]["items"] == []
    engine.close()


def test_against_an_unrelated_ref_is_an_error_not_an_empty_review(tmp_path: Path):
    """An unresolvable base used to produce an empty diff, reading as "no changes"."""
    root = _demo_repo(tmp_path)
    target = gitutil.resolve_target(str(root))
    try:
        gitutil.changed_files(target, "against", "no-such-branch")
    except gitutil.GitError:
        return
    raise AssertionError("an unresolvable base silently reported no changes")
