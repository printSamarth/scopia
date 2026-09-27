"""Layered DAG layout.

Hand-rolled rather than pulled from a library: the rendered page has to be a single
self-contained file with no JS runtime, which rules out handing the graph to mermaid or
d3 at view time.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..graph.model import Edge, Graph, Node

NODE_HEIGHT = 46
MIN_WIDTH = 150
CHAR_WIDTH = 7.1
PAD_X = 28
GAP_X = 74
GAP_Y = 18
MARGIN = 28
ISOLATED_COLUMNS = 3


@dataclass
class Placed:
    node: Node
    x: float
    y: float
    w: float
    h: float = NODE_HEIGHT

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h


@dataclass
class Layout:
    placed: dict[str, Placed]
    edges: list[Edge]
    width: float
    height: float
    isolated_top: float | None = None


def node_width(node: Node) -> float:
    """Size to the label actually drawn, or the text spills past the box."""
    return max(MIN_WIDTH, len(node.display) * CHAR_WIDTH + PAD_X)


def compute(graph: Graph) -> Layout:
    linked = {e.src for e in graph.edges} | {e.dst for e in graph.edges}
    connected = [n for n in graph.nodes if n.uid in linked]
    isolated = [n for n in graph.nodes if n.uid not in linked]

    layers = _assign_layers(connected, graph.edges)
    ordered = _order_within_layers(layers, graph.edges)

    placed: dict[str, Placed] = {}
    layer_widths = [max((node_width(n) for n in col), default=MIN_WIDTH) for col in ordered]

    x = MARGIN
    column_heights = [len(col) * NODE_HEIGHT + max(len(col) - 1, 0) * GAP_Y for col in ordered]
    tallest = max(column_heights, default=0)

    for depth, column in enumerate(ordered):
        y = MARGIN + (tallest - column_heights[depth]) / 2
        for node in column:
            placed[node.uid] = Placed(node=node, x=x, y=y, w=node_width(node))
            y += NODE_HEIGHT + GAP_Y
        x += layer_widths[depth] + GAP_X

    width = max(x - GAP_X + MARGIN, MARGIN * 2 + MIN_WIDTH)
    height = MARGIN * 2 + tallest
    isolated_top = None

    if isolated:
        isolated_top = height + 10 if connected else MARGIN
        y = isolated_top + 26
        col_width = max((node_width(n) for n in isolated), default=MIN_WIDTH)
        for i, node in enumerate(sorted(isolated, key=lambda n: n.label)):
            col = i % ISOLATED_COLUMNS
            row = i // ISOLATED_COLUMNS
            placed[node.uid] = Placed(
                node=node,
                x=MARGIN + col * (col_width + 20),
                y=y + row * (NODE_HEIGHT + 12),
                w=col_width,
            )
        rows = (len(isolated) + ISOLATED_COLUMNS - 1) // ISOLATED_COLUMNS
        height = y + rows * (NODE_HEIGHT + 12) + MARGIN
        width = max(width, MARGIN * 2 + ISOLATED_COLUMNS * (col_width + 20))

    return Layout(
        placed=placed,
        edges=[e for e in graph.edges if e.src in placed and e.dst in placed],
        width=width,
        height=height,
        isolated_top=isolated_top,
    )


def _assign_layers(nodes: list[Node], edges: list[Edge]) -> dict[int, list[Node]]:
    by_uid = {n.uid: n for n in nodes}
    succ: dict[str, list[str]] = defaultdict(list)
    preds: dict[str, set[str]] = defaultdict(set)

    for edge in edges:
        if edge.src in by_uid and edge.dst in by_uid and edge.src != edge.dst:
            succ[edge.src].append(edge.dst)
            preds[edge.dst].add(edge.src)

    # Cycles are real in call graphs (recursion, mutual calls). Break them with a DFS so
    # layering has a DAG to work with; the edge itself is still drawn.
    back: set[tuple[str, str]] = set()
    state: dict[str, int] = {}

    def dfs(u: str) -> None:
        state[u] = 1
        for v in succ.get(u, ()):
            if state.get(v, 0) == 1:
                back.add((u, v))
            elif state.get(v, 0) == 0:
                dfs(v)
        state[u] = 2

    for node in sorted(by_uid):
        if state.get(node, 0) == 0:
            dfs(node)

    indegree = {
        u: len([p for p in preds.get(u, ()) if (p, u) not in back]) for u in by_uid
    }
    depth = {u: 0 for u in by_uid}
    queue = sorted([u for u, d in indegree.items() if d == 0])

    while queue:
        u = queue.pop(0)
        for v in succ.get(u, ()):
            if (u, v) in back:
                continue
            depth[v] = max(depth[v], depth[u] + 1)
            indegree[v] -= 1
            if indegree[v] == 0:
                queue.append(v)

    layers: dict[int, list[Node]] = defaultdict(list)
    for uid_, d in depth.items():
        layers[d].append(by_uid[uid_])
    return layers


def _order_within_layers(
    layers: dict[int, list[Node]], edges: list[Edge]
) -> list[list[Node]]:
    columns = [layers[k] for k in sorted(layers)]
    for column in columns:
        column.sort(key=lambda n: (not n.touched, -n.fan_in, n.label))

    position = {n.uid: i for col in columns for i, n in enumerate(col)}
    preds: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        preds[edge.dst].append(edge.src)

    # Two barycenter passes: enough to untangle typical review-sized graphs, and cheap.
    for _ in range(2):
        for column in columns[1:]:
            column.sort(
                key=lambda n: (
                    sum(position.get(p, 0) for p in preds.get(n.uid, ())) / len(preds[n.uid])
                    if preds.get(n.uid)
                    else position.get(n.uid, 0)
                )
            )
            for i, n in enumerate(column):
                position[n.uid] = i
    return columns
