"""Terminal rendering — the default output, so it has to stand on its own."""

from __future__ import annotations

import os
import sys
from collections import defaultdict

from ..graph.model import CALL, Edge, Graph, Node, is_uncertain

_HI_FAN_IN = 5

#: Why an edge is not certain — stated plainly, so the reader can judge it themselves.
_WHY = {
    "ambiguous": "several definitions share this name",
    "inferred": "receiver type unknown; matched by name only",
    "heuristic": "passed as a reference, not called directly",
}


class _Style:
    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def accent(self, t: str) -> str:
        return self._wrap("36", t)

    def warn(self, t: str) -> str:
        return self._wrap("33", t)


def supports_color(stream=sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def render(graph: Graph, *, color: bool | None = None) -> str:
    s = _Style(supports_color() if color is None else color)
    out: list[str] = []

    touched = graph.touched
    out.append(
        s.bold(f"{len(touched)} symbols touched across {graph.files_changed} files")
        + s.dim(f"  ({len(graph.nodes) - len(touched)} shown for context)")
    )

    # A diff that is almost entirely new files is not a review of changes — it is a first
    # look at a codebase. Saying so is more honest than reporting "N lines changed" for
    # code that had no previous version.
    new_files = {n.path for n in touched if n.added}
    changed_files = {n.path for n in touched}
    if new_files and len(new_files) == len(changed_files):
        out.append(
            s.dim("Every file here is new — this is the shape of the code, not of a change.")
        )
    elif new_files:
        out.append(s.dim(f"{len(new_files)} of {len(changed_files)} files are new."))
    out.append("")

    outgoing: dict[str, list[Edge]] = defaultdict(list)
    for edge in graph.edges:
        outgoing[edge.src].append(edge)
    incoming = {e.dst for e in graph.edges}

    by_uid = {n.uid: n for n in graph.nodes}
    roots = [n for n in graph.nodes if outgoing.get(n.uid) and n.uid not in incoming]
    def weight(node: Node) -> int:
        """Changed symbols reachable from here — what a skimmer should meet first."""
        seen, stack, total = set(), [node.uid], 0
        while stack:
            uid = stack.pop()
            if uid in seen:
                continue
            seen.add(uid)
            target = by_uid.get(uid)
            if target is not None and target.touched:
                total += 1
            stack.extend(e.dst for e in outgoing.get(uid, ()))
        return total

    roots.sort(key=lambda n: (not n.touched, -weight(n), -n.fan_in, n.label))

    printed: set[str] = set()
    # A touched symbol reached only through an uncertain edge is never recursed into, so
    # without this its whole subtree would vanish from the report while still being
    # counted in the header. Anything changed gets to head its own chain.
    stranded = [
        n
        for n in graph.nodes
        if n.touched and outgoing.get(n.uid) and n not in roots
    ]
    stranded.sort(key=lambda n: (-weight(n), -n.fan_in, n.label))

    if roots or stranded:
        out.append(
            s.accent("CALL CHAINS")
            + s.dim("  — indented \u2192 means the line above calls it")
        )
        for root in roots:
            _chain(out, s, root, outgoing, by_uid, printed, depth=0)
            out.append("")
        for node in stranded:
            if node.uid in printed:
                continue
            _chain(out, s, node, outgoing, by_uid, printed, depth=0)
            out.append("")

    remaining = [
        n
        for n in graph.nodes
        if n.touched and n.uid not in printed and not outgoing.get(n.uid)
    ]
    called_by = [n for n in remaining if n.uid in incoming]
    isolated = [n for n in remaining if n.uid not in incoming]

    # A changed leaf — something everything calls and which calls nothing — never starts
    # a chain, so it would otherwise be counted in the header and then never shown. It is
    # also the highest-value case there is: a small edit with a wide blast radius.
    if called_by:
        callers_of: dict[str, list[str]] = defaultdict(list)
        for edge in graph.edges:
            callers_of[edge.dst].append(edge.src)

        kind_of = {(e.src, e.dst): e.kind for e in graph.edges}
        out.append(
            s.accent("CHANGED, REFERENCED FROM")
            + s.dim("  — these call nothing themselves")
        )
        for node in sorted(called_by, key=lambda n: (-n.fan_in, n.label)):
            out.append(f"  {_node_line(s, node)}")
            for caller_uid in sorted(set(callers_of.get(node.uid, ()))):
                caller = by_uid.get(caller_uid)
                if caller:
                    # An interface method's inbound edges are implementations, not calls;
                    # labelling them "called from" would misdescribe the relationship.
                    is_call = kind_of.get((caller_uid, node.uid)) == CALL
                    arrow = "←" if is_call else "← implemented by"
                    folder = (
                        caller.path.rsplit("/", 1)[0] + "/"
                        if "/" in caller.path
                        else ""
                    )
                    out.append(f"     {s.dim(arrow)} {caller.display}  {s.dim(folder)}")
            printed.add(node.uid)
        out.append("")

    if isolated:
        out.append(
            s.warn("NO CONNECTIONS FOUND")
            + s.dim("  — scopia could not trace these; not a statement that they are safe")
        )
        for node in sorted(isolated, key=lambda n: n.label):
            out.append(f"  {_node_line(s, node)}")
        out.append("")

    # An uncertain edge between two unchanged symbols is someone else's problem.
    uncertain = [
        e
        for e in graph.uncertain_edges
        if (by_uid.get(e.src) and by_uid[e.src].touched)
        or (by_uid.get(e.dst) and by_uid[e.dst].touched)
    ]
    if uncertain:
        out.append(
            s.warn("NOT CERTAIN")
            + s.dim("  — matched by name; treat these as leads, not facts")
        )
        for edge in sorted(uncertain, key=lambda e: (by_uid[e.src].display, by_uid[e.dst].display))[
            :10
        ]:
            if edge.src not in by_uid or edge.dst not in by_uid:
                continue
            why = _WHY.get(edge.provenance, edge.provenance)
            out.append(
                f"  {by_uid[edge.src].display} → {by_uid[edge.dst].display}  {s.dim(why)}"
            )
        if len(uncertain) > 10:
            out.append(s.dim(f"  +{len(uncertain) - 10} more"))
        out.append("")

    if graph.unparsed:
        out.append(
            s.accent("NOT ANALYSED")
            + s.dim("  — no parser for these; the graph says nothing about them")
        )
        for path, count in graph.unparsed[:8]:
            out.append(f"  {path}  {s.dim(f'({count} lines)')}")
        if len(graph.unparsed) > 8:
            out.append(s.dim(f"  +{len(graph.unparsed) - 8} more"))
        out.append("")

    tail = []
    if graph.utilities_hidden:
        tail.append(
            f"{graph.utilities_hidden} shared helper(s) hidden as plumbing "
            "(--utility 0 to show)"
        )
    if graph.truncated:
        tail.append(f"{graph.truncated} more nodes hidden by --max-nodes")
    for line in tail:
        out.append(s.dim(line))

    return "\n".join(out).rstrip() + "\n"


def _chain(
    out: list[str],
    s: _Style,
    node: Node,
    outgoing: dict[str, list[Edge]],
    by_uid: dict[str, Node],
    printed: set[str],
    depth: int,
    seen_on_path: frozenset[str] = frozenset(),
) -> None:
    indent = "  " + "   " * depth
    # "calls" spelled out: a bare tree connector leaves direction ambiguous.
    prefix = "" if depth == 0 else s.dim("└→ ")
    # Only a repeat with a subtree is worth marking — for a leaf there is nothing hidden.
    truncated = node.uid in printed and depth > 0 and _has_static_calls(outgoing, node.uid)
    suffix = s.dim("  ↑ shown above") if truncated else ""
    out.append(f"{indent}{prefix}{_node_line(s, node)}{suffix}")
    printed.add(node.uid)

    if truncated or depth >= 6:
        return

    edges = outgoing.get(node.uid, ())
    path = seen_on_path | {node.uid}

    # An ambiguous call is real information, but printing all six candidates under every
    # caller buries the chain it belongs to. Collapse to a count and keep the shape.
    ambiguous: dict[str, int] = {}
    for edge in edges:
        if edge.kind == CALL and is_uncertain(edge.provenance):
            target = by_uid.get(edge.dst)
            if target:
                key = target.label.rsplit(".", 1)[-1]
                ambiguous[key] = ambiguous.get(key, 0) + 1

    for name, count in sorted(ambiguous.items()):
        word = "candidate" if count == 1 else "candidates"
        out.append(f"{indent}   {s.dim('?')}  {name} {s.dim(f'→ {count} {word}')}")

    for edge in sorted(edges, key=lambda e: (e.order or 99, e.dst)):
        target = by_uid.get(edge.dst)
        if target is None or target.uid in path:
            continue
        if edge.kind != CALL:
            out.append(f"{indent}   {s.dim('┈ implements')} {target.display}")
            continue
        if is_uncertain(edge.provenance):
            continue
        # An untouched context leaf seen before adds nothing on a second sighting. A
        # *touched* one is shown every time: a small change reached from several chains
        # is exactly what the reviewer needs to notice.
        if (
            target.uid in printed
            and not target.touched
            and not _has_static_calls(outgoing, target.uid)
        ):
            continue
        _chain(out, s, target, outgoing, by_uid, printed, depth + 1, path)


def _has_static_calls(outgoing: dict[str, list[Edge]], uid: str) -> bool:
    return any(
        e.kind == CALL and not is_uncertain(e.provenance) for e in outgoing.get(uid, ())
    )


def _node_line(s: _Style, node: Node) -> str:
    label = node.display if node.touched else s.dim(node.display)
    bits = []
    if node.fresh:
        # Watch mode only: arrived or changed since the last redraw.
        bits.append(s.bold(s.accent("● just now")))
    if node.touched:
        if node.added:
            bits.append(s.accent("new"))
        else:
            unit = "line" if node.changed_lines == 1 else "lines"
            bits.append(f"{node.changed_lines} {unit} changed")
    if node.fan_in:
        approx = "~" if node.fan_in_shared else ""
        place = "place" if node.fan_in == 1 else "places"
        fan = f"called from {approx}{node.fan_in} {place}"
        bits.append(s.warn(fan) if node.fan_in >= _HI_FAN_IN else fan)
    if node.gone:
        bits.append(s.warn("removed"))
    elif node.pre_existing:
        bits.append(s.warn("existing code"))
    hidden = node.hidden_in + node.hidden_out
    if hidden:
        bits.append(s.warn(f"+{hidden} hidden"))
    if node.is_entry:
        bits.append(s.accent("entry point"))
    if not node.touched:
        bits.append(s.dim("context"))
    meta = s.dim("  " + " · ".join(bits)) if bits else ""
    # The label already carries the filename; only the directory adds anything.
    folder = node.path.rsplit("/", 1)[0] + "/" if "/" in node.path else ""
    return f"{label}{meta}  {s.dim(folder)}"
