"""Rank changed symbols by how much of the call graph they sit under."""

from __future__ import annotations

from .model import CHANGED_STATUSES, Graph


class HotspotRanker:
    """Scores changed nodes by transitive caller count."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self._callers = self._index()

    def _index(self) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for edge in self.graph.edges.values():
            if edge.kind == "calls":
                index.setdefault(edge.dst, []).append(edge.src)
        return index

    def blast_radius(self, node_id: str, limit: int = 400) -> int:
        seen: set[str] = set()
        stack = [node_id]
        while stack and len(seen) < limit:
            current = stack.pop()
            for caller in self._callers.get(current, []):
                if caller not in seen:
                    seen.add(caller)
                    stack.append(caller)
        return len(seen)

    def ranked(self) -> list[tuple[str, int]]:
        scores = [
            (node.id, self.blast_radius(node.id))
            for node in self.graph.nodes.values()
            if node.status in CHANGED_STATUSES
        ]
        scores.sort(key=lambda pair: pair[1], reverse=True)
        return scores
