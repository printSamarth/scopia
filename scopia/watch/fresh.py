"""The "new since last redraw" reading of the graph.

The graph itself is always measured against HEAD, so over a long session it only grows and
eventually is the whole change again. This is the second reading of that *same* graph: a
transient flag on whatever arrived or changed recently, decaying after a few seconds. One
graph, two readings — never two notations.
"""

from __future__ import annotations

from ..graph.model import Graph

DEFAULT_TTL = 8.0


class Highlights:
    def __init__(self, ttl: float = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._lines: dict[str, int] | None = None  # uid -> changed_lines at last update
        self._expires: dict[str, float] = {}
        self._shown: frozenset[str] = frozenset()

    def update(self, graph: Graph, now: float) -> None:
        """Record a new graph. Touched symbols that appeared or grew are marked fresh."""
        current = {n.uid: n.changed_lines for n in graph.touched}
        # The first graph is everything already present when the user started watching;
        # marking it all fresh would highlight the whole screen and so nothing.
        if self._lines is not None:
            for uid, lines in current.items():
                if self._lines.get(uid) != lines:
                    self._expires[uid] = now + self._ttl
        self._lines = current

    def apply(self, graph: Graph, now: float) -> bool:
        """Stamp `fresh` onto the nodes. True when the lit set differs from the last call.

        That return value is how a driver knows a highlight has just decayed and the screen
        needs redrawing even though no file changed.
        """
        self._expires = {u: t for u, t in self._expires.items() if t > now}
        lit = frozenset(self._expires)
        for node in graph.nodes:
            node.fresh = node.uid in lit
        changed = lit != self._shown
        self._shown = lit
        return changed
