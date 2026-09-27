"""Graph -> mermaid flowchart, in the one notation."""

from __future__ import annotations

from ..graph.model import CALL, Graph, Node, is_uncertain

_HI_FAN_IN = 5


def render(graph: Graph) -> str:
    ids = {n.uid: f"n{i}" for i, n in enumerate(graph.nodes)}
    lines = ["flowchart LR"]

    for node in graph.nodes:
        lines.append(f'  {ids[node.uid]}["{_label(node)}"]')

    for edge in graph.edges:
        src, dst = ids.get(edge.src), ids.get(edge.dst)
        if not src or not dst:
            continue
        if edge.kind == CALL and not is_uncertain(edge.provenance):
            label = f'|"{edge.order}"|' if edge.order else ""
            lines.append(f"  {src} -->{label} {dst}")
        elif edge.kind == CALL:
            lines.append(f'  {src} -.->|"?"| {dst}')
        else:
            lines.append(f'  {src} -.->|"implements"| {dst}')

    classes: dict[str, list[str]] = {"touched": [], "context": [], "isolated": [], "hot": []}
    for node in graph.nodes:
        classes[_class_of(node)].append(ids[node.uid])

    for name, members in classes.items():
        if members:
            lines.append(f"  class {','.join(members)} {name}")

    lines += [
        "  classDef touched stroke-width:2px",
        "  classDef hot stroke-width:3px,stroke:#c2660e",
        "  classDef context stroke-dasharray:0,opacity:0.65",
        "  classDef isolated stroke-dasharray:3 3",
    ]
    if graph.truncated:
        lines.append(f'  more["+{graph.truncated} more"]')
        lines.append("  class more context")
    return "\n".join(lines)


def _class_of(node: Node) -> str:
    if not node.touched:
        return "context"
    if node.fan_in >= _HI_FAN_IN:
        return "hot"
    if node.isolated:
        return "isolated"
    return "touched"


def _label(node: Node) -> str:
    label = node.display.replace('"', "'")
    if node.touched and node.fan_in:
        approx = "~" if node.fan_in_shared else ""
        caller = "caller" if node.fan_in == 1 else "callers"
        # <br/> renders reliably across mermaid versions; a literal \n does not.
        label += f"<br/>{approx}{node.fan_in} {caller}"
    return label
