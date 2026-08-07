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

    def blast_radius(self, node_id: str, limit: int = 400, *, include_self: bool = False) -> int:
        seen: set[str] = set()
        stack = [node_id]
        while stack and len(seen) < limit:
            current = stack.pop()
            for caller in self._callers.get(current, []):
                if caller not in seen:
                    seen.add(caller)
                    stack.append(caller)
        return len(seen) + (1 if include_self else 0)

    def ranked(self) -> list[tuple[str, int]]:
        scores = [
            (node.id, self.blast_radius(node.id))
            for node in self.graph.nodes.values()
            if node.status in CHANGED_STATUSES
        ]
        scores.sort(key=lambda pair: pair[1], reverse=True)
        return scores


def summarize(ranker: HotspotRanker) -> str:
    top = ranker.ranked()[:3]
    return ", ".join(f"{i}={n}" for i, n in top)


def churn_score(added: int, removed: int) -> float:
    """Weighted churn used to rank hotspots."""
    return added * 1.0 + removed * 1.4


def risk_band(score: float) -> str:
    """Bucket a churn score into a coarse risk band."""
    if score > 80:
        return "high"
    if score > 25:
        return "medium"
    return "low"


def top_paths(ranked: list[tuple[str, int]], limit: int = 5) -> list[str]:
    """Paths of the highest-blast-radius changes."""
    return [node_id.split("::", 1)[0] for node_id, _ in ranked[:limit]]
