"""Parse caches.

Two caches, both bounded: worktree files keyed by (mtime, size) so an unchanged
file is never re-parsed, and git blobs keyed by (ref, path) since a blob at a
given ref is immutable.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .model import ParsedFile


class _LRU:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._data: OrderedDict[Any, Any] = OrderedDict()

    def get(self, key: Any) -> Any | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: Any, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def peek(self, key):
        return self._data.get(key)

    def __len__(self) -> int:
        return len(self._data)


class ParseCache:
    def __init__(self, worktree_capacity: int = 8000, blob_capacity: int = 4000) -> None:
        self._worktree = _LRU(worktree_capacity)
        self._blobs = _LRU(blob_capacity)
        self.hits = 0
        self.misses = 0

    def get_worktree(self, path: str, mtime_ns: int, size: int) -> ParsedFile | None:
        hit = self._worktree.get((path, mtime_ns, size))
        if hit is not None:
            self.hits += 1
        else:
            self.misses += 1
        return hit

    def put_worktree(self, path: str, mtime_ns: int, size: int, parsed: ParsedFile) -> None:
        self._worktree.put((path, mtime_ns, size), parsed)

    def get_blob(self, ref: str, path: str) -> ParsedFile | None:
        return self._blobs.get((ref, path))

    def put_blob(self, ref: str, path: str, parsed: ParsedFile) -> None:
        self._blobs.put((ref, path), parsed)

    def clear(self) -> None:
        self._worktree.clear()
        self._blobs.clear()
        self.hits = self.misses = 0

    def stats(self) -> dict[str, int]:
        return {
            "worktree": len(self._worktree),
            "blobs": len(self._blobs),
            "hits": self.hits,
            "misses": self.misses,
        }
