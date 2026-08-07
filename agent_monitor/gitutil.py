"""Thin wrappers over the `git` CLI.

We shell out rather than using pygit2/GitPython: git is guaranteed present for
any repo worth watching, behaves identically on every OS, and needs no compiled
dependency. All commands use ``-z`` so paths containing spaces, newlines, or
non-UTF8 bytes survive intact, and none of them use ``shell=True``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .model import FileChange, RepoTarget

#: Directories we never descend into, git or not.
VENDORED = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "venv",
        ".venv",
        "env_cv",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        "vendor",
        "site-packages",
        ".idea",
        ".vscode",
    }
)

MAX_BLOB_BYTES = 1_000_000


class GitError(RuntimeError):
    """A git command failed."""


def _run(root: str, args: list[str], *, check: bool = True) -> bytes:
    """Run `git -C root <args>` and return raw stdout."""
    proc = subprocess.run(
        ["git", "-C", root, *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 and check:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc.stdout


def _text(root: str, args: list[str], *, check: bool = True) -> str:
    return _run(root, args, check=check).decode("utf-8", "replace").strip()


def _split_z(raw: bytes) -> list[str]:
    """Split NUL-separated git output into decoded, non-empty strings."""
    return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]


def _posix(path: str) -> str:
    return path.replace(os.sep, "/")


# --------------------------------------------------------------------------
# target resolution
# --------------------------------------------------------------------------


def resolve_target(path: str, scope: str | None = None) -> RepoTarget:
    """Resolve a user-chosen directory into (repo root, analyzed subtree).

    Critically, the *scope* is what we analyze and watch while the *root* is
    what git commands run against. Opening a subdirectory of a huge repo
    therefore does not drag the whole repo into the graph.
    """
    abs_path = str(Path(path).expanduser().resolve())
    if not os.path.isdir(abs_path):
        raise GitError(f"not a directory: {abs_path}")

    try:
        root = _text(abs_path, ["rev-parse", "--show-toplevel"])
    except GitError:
        # Not a git repo — still usable, just without history-based diffing.
        return RepoTarget(root=abs_path, scope="", is_git=False)

    root = str(Path(root).resolve())
    if scope is not None:
        rel = _posix(scope).strip("/")
    else:
        rel = _posix(os.path.relpath(abs_path, root))
        if rel == ".":
            rel = ""

    branch = _text(root, ["rev-parse", "--abbrev-ref", "HEAD"], check=False) or None
    if branch == "HEAD":  # detached
        branch = _text(root, ["rev-parse", "--short", "HEAD"], check=False) or None
    if not branch:
        # No commits yet: rev-parse fails, but the symbolic ref already exists.
        branch = _text(root, ["symbolic-ref", "--short", "HEAD"], check=False) or None

    return RepoTarget(root=root, scope=rel, is_git=True, branch=branch)


def has_commits(root: str) -> bool:
    """False for a freshly `git init`ed repo with no HEAD yet."""
    proc = subprocess.run(
        ["git", "-C", root, "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def default_branch(root: str) -> str:
    """Best guess at the repo's trunk, for branch-vs-trunk diffs."""
    head = _text(root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], check=False)
    if head:
        return head.split("/", 1)[-1]
    for candidate in ("main", "master", "trunk", "develop"):
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", f"refs/heads/{candidate}"],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            return candidate
    return "HEAD"


# --------------------------------------------------------------------------
# listing files
# --------------------------------------------------------------------------


def list_files(target: RepoTarget) -> list[str]:
    """All repo-relative files in scope that git would consider 'yours'.

    In a git repo that means tracked files plus untracked-but-not-ignored ones,
    which gives correct .gitignore semantics for free. Outside a repo we walk
    the tree and skip the vendored directory list.
    """
    if target.is_git:
        paths: set[str] = set()
        for args in (
            ["ls-files", "-z"],
            ["ls-files", "--others", "--exclude-standard", "-z"],
        ):
            paths.update(_split_z(_run(target.root, args, check=False)))
        return sorted(p for p in paths if target.in_scope(p))

    base = Path(target.abs_scope)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in VENDORED and not d.startswith(".")]
        for name in filenames:
            full = Path(dirpath) / name
            out.append(_posix(str(full.relative_to(target.root))))
    return sorted(out)


def check_ignore(root: str, paths: list[str]) -> set[str]:
    """Return the subset of `paths` that git ignores.

    Batched through a single `check-ignore --stdin` call rather than
    reimplementing .gitignore semantics. Exit code 1 means "none ignored",
    which is a success, not an error.
    """
    if not paths:
        return set()
    payload = b"\0".join(p.encode("utf-8") for p in paths) + b"\0"
    proc = subprocess.run(
        ["git", "-C", root, "check-ignore", "-z", "--stdin"],
        input=payload,
        capture_output=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        return set()
    return set(_split_z(proc.stdout))


# --------------------------------------------------------------------------
# reading content
# --------------------------------------------------------------------------


def read_blob(root: str, ref: str, path: str) -> bytes | None:
    """Content of `path` at `ref`, or None if it did not exist there."""
    proc = subprocess.run(
        ["git", "-C", root, "show", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def list_files_at_ref(root: str, ref: str) -> list[str]:
    """Every file present at `ref`."""
    raw = _run(root, ["ls-tree", "-r", "--name-only", "-z", ref], check=False)
    return _split_z(raw)


def read_blobs(root: str, ref: str, paths: list[str]) -> dict[str, bytes]:
    """Read many blobs at `ref` in a single `git cat-file --batch` process.

    Spawning one `git show` per file is the obvious implementation and is
    unusably slow on a large tree; batching keeps opening a historical commit
    to a single subprocess.
    """
    if not paths:
        return {}

    payload = "".join(f"{ref}:{p}\n" for p in paths).encode("utf-8")
    proc = subprocess.run(
        ["git", "-C", root, "cat-file", "--batch"],
        input=payload,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return {}

    out: dict[str, bytes] = {}
    buf = proc.stdout
    pos = 0
    for path in paths:
        newline = buf.find(b"\n", pos)
        if newline == -1:
            break
        header = buf[pos:newline].decode("utf-8", "replace")
        pos = newline + 1
        parts = header.rsplit(" ", 2)
        if len(parts) != 3 or not parts[2].isdigit():
            # "<object> missing" — nothing consumed beyond the header line
            continue
        size = int(parts[2])
        if size <= MAX_BLOB_BYTES:
            out[path] = buf[pos : pos + size]
        pos += size + 1  # trailing newline after the payload
    return out


def read_worktree(root: str, path: str) -> bytes | None:
    full = Path(root) / path
    try:
        if full.stat().st_size > MAX_BLOB_BYTES:
            return None
        return full.read_bytes()
    except (OSError, ValueError):
        return None


def read_side(root: str, ref: str | None, path: str) -> bytes | None:
    """Read one side of a diff: a git ref, or the working tree when ref is None."""
    if ref is None:
        return read_worktree(root, path)
    return read_blob(root, ref, path)


# --------------------------------------------------------------------------
# change detection
# --------------------------------------------------------------------------

_STATUS_MAP = {
    "A": "added",
    "M": "modified",
    "D": "removed",
    "R": "renamed",
    "C": "added",
    "T": "modified",
}


def _parse_name_status(raw: bytes) -> list[tuple[str, str, str | None]]:
    """Parse `--name-status -z` output into (status, path, old_path) triples.

    The -z format is a flat NUL-separated stream where rename/copy entries take
    three fields (``R100``, old, new) and everything else takes two.
    """
    fields = _split_z(raw)
    out: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(fields):
        code = fields[i]
        letter = code[:1]
        if letter in ("R", "C"):
            if i + 2 >= len(fields):
                break
            out.append((_STATUS_MAP.get(letter, "modified"), fields[i + 2], fields[i + 1]))
            i += 3
        else:
            if i + 1 >= len(fields):
                break
            out.append((_STATUS_MAP.get(letter, "modified"), fields[i + 1], None))
            i += 2
    return out


def _parse_numstat(raw: bytes) -> dict[str, tuple[int, int]]:
    """Parse `--numstat -z` into {path: (added, removed)}.

    Normal entries are ``<add>\\t<del>\\t<path>\\0``; renames put an empty path
    in that field and follow with two more NUL-separated fields (old, new).
    """
    fields = _split_z(raw)
    out: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(fields):
        parts = fields[i].split("\t")
        if len(parts) < 3:
            i += 1
            continue
        added = 0 if parts[0] == "-" else int(parts[0] or 0)
        removed = 0 if parts[1] == "-" else int(parts[1] or 0)
        path = parts[2]
        if path == "":  # rename: the next two fields are old, new
            if i + 2 < len(fields):
                out[fields[i + 2]] = (added, removed)
            i += 3
        else:
            out[path] = (added, removed)
            i += 1
    return out


def changed_files(
    target: RepoTarget,
    mode: str = "live",
    ref: str | None = None,
) -> tuple[list[FileChange], str | None]:
    """Return (changes, base_ref) for the requested comparison.

    Modes:
      live         working tree (staged + unstaged + untracked) vs HEAD
      commit       <ref>^ vs <ref>            (handles the root commit)
      branch       merge-base(<ref>, trunk) vs <ref>
      range        "a..b"
    """
    if not target.is_git:
        return [], None

    root = target.root

    if mode == "live":
        if not has_commits(root):
            # Nothing committed yet — treat every file in scope as new.
            files = [
                FileChange(path=p, status="added", new_ref=None)
                for p in list_files(target)
            ]
            return files, None
        base, head = "HEAD", None
        name_status = _run(root, ["diff", "--name-status", "-M", "-z", "HEAD"], check=False)
        numstat = _run(root, ["diff", "--numstat", "-z", "HEAD"], check=False)
        untracked = _split_z(
            _run(root, ["ls-files", "--others", "--exclude-standard", "-z"], check=False)
        )
    elif mode == "commit":
        if not ref:
            raise GitError("commit mode requires a ref")
        head = ref
        parent_exists = (
            subprocess.run(
                ["git", "-C", root, "rev-parse", "--verify", f"{ref}^"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if parent_exists:
            base = f"{ref}^"
            name_status = _run(root, ["diff", "--name-status", "-M", "-z", base, ref])
            numstat = _run(root, ["diff", "--numstat", "-z", base, ref])
        else:  # root commit — diff against the empty tree
            base = None
            name_status = _run(
                root, ["show", "--name-status", "-M", "-z", "--format=", ref]
            )
            numstat = _run(root, ["show", "--numstat", "-z", "--format=", ref])
        untracked = []
    elif mode == "branch":
        if not ref:
            raise GitError("branch mode requires a ref")
        trunk = default_branch(root)
        base = _text(root, ["merge-base", ref, trunk], check=False) or trunk
        head = ref
        name_status = _run(root, ["diff", "--name-status", "-M", "-z", base, ref])
        numstat = _run(root, ["diff", "--numstat", "-z", base, ref])
        untracked = []
    elif mode == "range":
        if not ref or ".." not in ref:
            raise GitError("range mode requires 'a..b'")
        base, head = ref.split("..", 1)
        name_status = _run(root, ["diff", "--name-status", "-M", "-z", base, head])
        numstat = _run(root, ["diff", "--numstat", "-z", base, head])
        untracked = []
    else:
        raise GitError(f"unknown mode: {mode}")

    stats = _parse_numstat(numstat)
    changes: list[FileChange] = []
    for status, path, old_path in _parse_name_status(name_status):
        if not target.in_scope(path):
            continue
        added, removed = stats.get(path, (0, 0))
        changes.append(
            FileChange(
                path=path,
                status=status,
                old_path=old_path,
                old_ref=base,
                new_ref=head,
                added=added,
                removed=removed,
            )
        )

    for path in untracked:
        if target.in_scope(path):
            changes.append(FileChange(path=path, status="added", old_ref=None, new_ref=None))

    changes.sort(key=lambda c: c.path)
    return changes, base


# --------------------------------------------------------------------------
# metadata for the UI pickers
# --------------------------------------------------------------------------


def recent_commits(root: str, limit: int = 25) -> list[dict[str, str]]:
    if not has_commits(root):
        return []
    raw = _run(
        root,
        ["log", f"-{limit}", "--format=%H%x00%h%x00%s%x00%an%x00%ar%x00"],
        check=False,
    )
    fields = raw.decode("utf-8", "replace").split("\0")
    out: list[dict[str, str]] = []
    for i in range(0, len(fields) - 4, 5):
        sha, short, subject, author, when = fields[i : i + 5]
        if not sha.strip():
            continue
        out.append(
            {
                "sha": sha.strip(),
                "short": short.strip(),
                "subject": subject.strip(),
                "author": author.strip(),
                "when": when.strip(),
            }
        )
    return out


def head_info(root: str) -> tuple[str | None, str]:
    """(sha, subject) of HEAD, or (None, "") in a repo with no commits."""
    if not has_commits(root):
        return None, ""
    raw = _text(root, ["log", "-1", "--format=%H%x00%s"], check=False)
    sha, _, subject = raw.partition("\0")
    return (sha.strip() or None), subject.strip()


def branches(root: str) -> list[str]:
    raw = _text(root, ["for-each-ref", "--format=%(refname:short)", "refs/heads"], check=False)
    return [b for b in raw.splitlines() if b.strip()]
