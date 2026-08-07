#!/usr/bin/env python3
"""Drive a demo repo through a scripted series of edits.

Lets you see every change state animate without waiting for a real agent, and
doubles as the manual test for the live pipeline.

    python tools/simulate_agent.py /tmp/demo-repo
    # then, in another terminal:
    python -m agent_monitor /tmp/demo-repo

Each step pauses so you can watch it land. Ctrl-C stops at any point.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

STEP_PAUSE = 4.0


def run(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip("\n"), encoding="utf-8")


def seed(root: Path) -> None:
    """Create a small multi-language repo with a committed baseline."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    run(root, "init", "-q")
    run(root, "config", "user.email", "demo@example.com")
    run(root, "config", "user.name", "Demo")

    write(root, "app/store.py", """
\"\"\"In-memory record store.\"\"\"


class Store:
    def __init__(self):
        self.items = {}

    def get(self, key):
        return self.items.get(key)

    def put(self, key, value):
        self.items[key] = value
        return value


def build_store():
    return Store()
""")

    write(root, "app/service.py", """
\"\"\"Business logic on top of the store.\"\"\"

from .store import build_store


class Service:
    def __init__(self):
        self.store = build_store()

    def fetch(self, key):
        return self.store.get(key)

    def save(self, key, value):
        return self.store.put(key, value)


def main():
    service = Service()
    service.save("a", 1)
    return service.fetch("a")


if __name__ == "__main__":
    main()
""")

    write(root, "web/client.js", """
import { Service } from './service.js';

export class Client {
  constructor(base) {
    this.base = base;
  }

  async load(key) {
    return fetch(`${this.base}/${key}`);
  }
}

export function boot() {
  return new Client('/api');
}
""")

    write(root, "app/legacy.py", """
\"\"\"Deprecated helpers, deleted by the last step of the demo.\"\"\"

from .store import build_store


class LegacyAdapter:
    def __init__(self):
        self.store = build_store()

    def read(self, key):
        return self.store.get(key)

    def write(self, key, value):
        return self.store.put(key, value)


def legacy_entry():
    return LegacyAdapter().read("a")
""")

    write(root, "README.md", "# Demo repo\n\nUsed by tools/simulate_agent.py.\n")
    run(root, "add", "-A")
    run(root, "commit", "-qm", "baseline")


STEPS: list[tuple[str, str, str]] = [
    (
        "modify a function body",
        "app/store.py",
        """
\"\"\"In-memory record store.\"\"\"


class Store:
    def __init__(self):
        self.items = {}
        self.hits = 0

    def get(self, key):
        value = self.items.get(key)
        if value is not None:
            self.hits += 1
        return value

    def put(self, key, value):
        self.items[key] = value
        return value


def build_store():
    return Store()
""",
    ),
    (
        "change a signature (API-breaking)",
        "app/store.py",
        """
\"\"\"In-memory record store.\"\"\"


class Store:
    def __init__(self, capacity=128):
        self.items = {}
        self.hits = 0
        self.capacity = capacity

    def get(self, key, default=None):
        value = self.items.get(key, default)
        if value is not None:
            self.hits += 1
        return value

    def put(self, key, value):
        self.items[key] = value
        return value


def build_store():
    return Store()
""",
    ),
    (
        "add a class with methods",
        "app/cache.py",
        """
\"\"\"A small write-through cache.\"\"\"

from .store import build_store


class Cache:
    def __init__(self, ttl=60):
        self.ttl = ttl
        self.backing = build_store()
        self.entries = {}

    def get(self, key):
        return self.entries.get(key) or self.backing.get(key)

    def put(self, key, value):
        self.entries[key] = value
        return self.backing.put(key, value)

    def evict(self, key):
        self.entries.pop(key, None)


def warm(cache, pairs):
    for key, value in pairs:
        cache.put(key, value)
    return cache
""",
    ),
    (
        "reformat only — should register NO change",
        "app/service.py",
        """
\"\"\"Business logic on top of the store.\"\"\"

from .store import build_store


class Service:

    def __init__(self):
        self.store = build_store()

    # fetch a single record
    def fetch(self, key):
        return self.store.get(key)

    def save(self, key, value):
        return self.store.put(key, value)


def main():
    service = Service()
    service.save("a", 1)
    return service.fetch("a")


if __name__ == "__main__":
    main()
""",
    ),
    (
        "add a JS method",
        "web/client.js",
        """
import { Service } from './service.js';

export class Client {
  constructor(base) {
    this.base = base;
  }

  async load(key) {
    return fetch(`${this.base}/${key}`);
  }

  async save(key, value) {
    return fetch(`${this.base}/${key}`, { method: 'PUT', body: value });
  }
}

export function boot() {
  return new Client('/api');
}
""",
    ),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="where to create the demo repo")
    parser.add_argument("--pause", type=float, default=STEP_PAUSE, help="seconds between steps")
    parser.add_argument("--seed-only", action="store_true", help="create the repo and stop")
    args = parser.parse_args(argv)

    root = Path(args.path).expanduser().resolve()
    print(f"Seeding demo repo at {root}")
    seed(root)
    print("Baseline committed.\n")
    print(f"  Open it now:  python -m agent_monitor {root}")
    print("  (or in the Electron shell: File → Open Repository)\n")

    if args.seed_only:
        return 0

    input("Press Enter once Agent Monitor is open and settled… ")

    for index, (label, rel, content) in enumerate(STEPS, 1):
        print(f"[{index}/{len(STEPS)}] {label}  ({rel})")
        write(root, rel, content)
        time.sleep(args.pause)

    print("\n[final] delete a committed file (app/legacy.py)")
    (root / "app" / "legacy.py").unlink(missing_ok=True)
    time.sleep(args.pause)

    print("\nDone. The Changes view should now show:")
    print("  modified Store.get / Store.__init__   (amber)")
    print("  signature changed Store.get           (pink, double ring)")
    print("  added Cache + its methods                (emerald)")
    print("  removed LegacyAdapter + its methods      (crimson ghosts)")
    print("  added Client.save                     (emerald)")
    print("  NO change from the app/service.py reformat — that's the point")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(130)
