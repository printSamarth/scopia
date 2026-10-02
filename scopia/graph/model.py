"""Graph data model.

Every edge carries its provenance. The graph is allowed to be uncertain; it is not
allowed to *look* certain when it isn't.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Edge provenance — how the edge was established.
STATIC = "static"  # an unqualified call with exactly one definition; high confidence
AMBIGUOUS = "ambiguous"  # several candidates (interface, duck typing, reflection)
INFERRED = "inferred"  # matched by name through a receiver whose type is unknown
HEURISTIC = "heuristic"  # a string callable, framework wiring, DI
TRACED = "traced"  # observed in a real run (phase 5)
RESOLVED = "resolved"  # a language server named the definition; as certain as it gets

#: Provenances that must never be drawn as a confident, solid edge.
UNCERTAIN = frozenset({AMBIGUOUS, INFERRED, HEURISTIC})


def is_uncertain(provenance: str) -> bool:
    return provenance in UNCERTAIN


def display_label(label: str, path: str, kind: str) -> str:
    """One naming scheme, used by every renderer: `file.Class.method`.

    A bare `generate` says nothing about where it lives, and two files can each define
    one. Prefixing the module stem makes every node self-identifying, which matters most
    in the graph where there is no room for a full path.
    """
    filename = path.rsplit("/", 1)[-1]
    if kind == "module":
        # The qualname is already the filename; `billing.billing.py` helps nobody.
        return filename
    stem = filename.rsplit(".", 1)[0]
    if not stem:
        return label
    # One class per file, named after the file, is the norm in PHP (PSR-4) and common
    # elsewhere — prefixing there yields `Controller.Controller.respond`, which is
    # noise rather than context.
    if label == stem or label.startswith(f"{stem}."):
        return label
    return f"{stem}.{label}"

# Edge kinds.
CALL = "call"
IMPLEMENTS = "implements"


@dataclass
class Node:
    uid: str
    label: str
    path: str
    kind: str
    touched: bool
    fan_in: int = 0
    fan_in_shared: bool = False
    """Other definitions share this name, so the count is spread across them."""
    changed_lines: int = 0
    gone: bool = False
    added: bool = False
    hidden_in: int = 0
    hidden_out: int = 0
    pre_existing: bool = False
    fresh: bool = False
    """Arrived or changed since the last redraw in watch mode. Transient display state —
    deliberately absent from `Graph.to_dict`, so it never leaks into golden tests."""
    is_entry: bool = False
    """Nothing in the repo calls this, so something outside does — an HTTP route, a
    scheduler, a CLI entry. These are where a reader's eye should start."""
    """The symbol was already there before this diff — so this edits behaviour
    that other code may already depend on, rather than adding something new."""
    """The file did not exist before — "N lines changed" would misdescribe it."""

    @property
    def display(self) -> str:
        """`file.Class.method` — the one name this node is known by, everywhere."""
        return display_label(self.label, self.path, self.kind)

    @property
    def isolated(self) -> bool:
        return self.degree == 0

    degree: int = field(default=0, compare=False)


@dataclass
class Edge:
    src: str
    dst: str
    kind: str
    provenance: str
    order: int | None = None
    line: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.kind)


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    truncated: int = 0
    utilities_hidden: int = 0
    files_changed: int = 0
    unparsed: list[tuple[str, int]] = field(default_factory=list)
    """(path, changed lines) for files in no supported language. The graph says
    nothing about these, and silence would read as "nothing changed here"."""
    files_indexed: int = 0
    files_parsed: int = 0
    index: Any = field(default=None, repr=False, compare=False)
    """The RepoIndex this graph was built from, kept so a resolver can refine it later."""

    @property
    def touched(self) -> list[Node]:
        return [n for n in self.nodes if n.touched]

    @property
    def uncertain_edges(self) -> list[Edge]:
        """Edges the reader should not treat as established fact."""
        return [e for e in self.edges if is_uncertain(e.provenance)]

    def node(self, uid: str) -> Node | None:
        for n in self.nodes:
            if n.uid == uid:
                return n
        return None

    def to_dict(self) -> dict:
        """Stable shape for golden tests — assert against this, never rendered output."""
        return {
            "nodes": [
                {
                    "uid": n.uid,
                    "label": n.label,
                    "path": n.path,
                    "kind": n.kind,
                    "touched": n.touched,
                    "fan_in": n.fan_in,
                    "fan_in_shared": n.fan_in_shared,
                    "changed_lines": n.changed_lines,
                    "gone": n.gone,
                    "added": n.added,
                    "pre_existing": n.pre_existing,
                    "is_entry": n.is_entry,
                    "hidden_in": n.hidden_in,
                    "hidden_out": n.hidden_out,
                }
                for n in sorted(self.nodes, key=lambda n: n.uid)
            ],
            "edges": [
                {
                    "src": e.src,
                    "dst": e.dst,
                    "kind": e.kind,
                    "provenance": e.provenance,
                    "order": e.order,
                }
                for e in sorted(self.edges, key=lambda e: (e.src, e.dst, e.kind))
            ],
            "truncated": self.truncated,
            "utilities_hidden": self.utilities_hidden,
            "files_changed": self.files_changed,
            "unparsed": self.unparsed,
        }
