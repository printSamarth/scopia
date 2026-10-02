"""Turn language-server answers into sharper edges.

The tree-sitter graph is drawn first; this refines it. For every call the static pass could
only match by name, ask the server where the call really goes, then either confirm one
target (`RESOLVED`), or — when the server says the definition lives outside the repo —
drop the name-matched edges outright, since they were never real.
"""

from __future__ import annotations

import dataclasses
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..graph.build import _number_calls
from ..graph.model import CALL, RESOLVED, Edge, Graph, Node, is_uncertain
from ..lang.base import LITERAL
from .session import Sessions

Group = tuple[str, str]  # (caller uid, callee name)


@dataclass
class Answer:
    targets: set[str] = field(default_factory=set)
    """Repo definitions the server named."""
    external: bool = False
    """The server named a definition outside the repo."""


@dataclass
class Stats:
    uncertain_before: int = 0
    uncertain_after: int = 0
    groups: int = 0
    answered: int = 0
    seconds: float = 0.0
    out_of_time: bool = False
    warmup: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.groups:
            return "language server: nothing uncertain to resolve"
        line = (
            f"language server: resolved {self.answered} of {self.groups} uncertain call(s); "
            f"uncertain edges {self.uncertain_before} → {self.uncertain_after}"
        )
        return line + " (stopped at the time budget)" if self.out_of_time else line


def uncertain_groups(graph: Graph) -> dict[Group, list[Edge]]:
    index = graph.index
    groups: dict[Group, list[Edge]] = defaultdict(list)
    if index is None:
        return groups
    for edge in graph.edges:
        if edge.kind != CALL or not is_uncertain(edge.provenance):
            continue
        target = index.defs.get(edge.dst)
        if target is None:
            continue
        groups[(edge.src, target.name)].append(edge)

    for src, name in list(groups):
        forms = {f for c, f, _s, _m in index.callees_of.get(src, ()) if c == name}
        # A string passed as a callable has no call syntax at its position to ask about.
        if forms and forms <= {LITERAL}:
            del groups[(src, name)]
    return groups


def ask(
    graph: Graph, sessions: Sessions, root: Path, cancelled=lambda: False
) -> dict[Group, Answer]:
    """Query the servers for every uncertain group. Blocking; run it off the UI thread."""
    index = graph.index
    answers: dict[Group, Answer] = {}
    by_path: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for uid_, definition in index.defs.items():
        by_path[index.path_of.get(uid_, "")].append((uid_, definition))
    touched = {n.uid for n in graph.touched}
    ordered = sorted(
        uncertain_groups(graph).items(),
        # What changed first: if time runs out, the edges a reviewer is looking at are the
        # ones that got sharpened.
        key=lambda item: (item[0][0] not in touched, item[0]),
    )
    for (src, name), _edges in ordered:
        if cancelled():
            break
        session = sessions.get(index.lang_of.get(src, ""))
        if session is None:
            continue
        path = index.path_of[src]
        positions = _positions(root, path, name, index.call_lines.get((src, name), ()))
        if not positions:
            continue
        answer = Answer()
        known = True
        for line, col in positions:
            locations = session.definition(path, line, col)
            if locations is None:
                known = False
                break
            for rel, line0 in locations:
                if rel is None:
                    answer.external = True
                else:
                    hit = _def_at(by_path.get(rel, ()), line0 + 1, name)
                    if hit:
                        answer.targets.add(hit)
                    else:
                        answer.external = True
        if known and (answer.targets or answer.external):
            answers[(src, name)] = answer
    return answers


def _positions(root: Path, path: str, name: str, lines) -> list[tuple[int, int]]:
    """Zero-based (line, column) of `name(` on each call line.

    The index records lines but not columns, so the column is found by reading the line.
    Only same-line calls are handled; a chained call whose name sits on a later line is
    left unresolved rather than guessed at.
    """
    try:
        source = (root / path).read_text("utf-8", "replace").splitlines()
    except OSError:
        return []
    pattern = re.compile(rf"(?<![\w$]){re.escape(name)}(?=\s*\()")
    out = []
    for line in sorted(set(lines)):
        if 1 <= line <= len(source):
            out += [(line - 1, m.start()) for m in pattern.finditer(source[line - 1])]
    return out


def _def_at(candidates, line: int, name: str) -> str | None:
    """The definition containing `line`, preferring the one actually named `name`."""
    best, best_key = None, None
    for uid_, definition in candidates:
        if not definition.contains(line):
            continue
        key = (definition.name != name, definition.span)
        if best_key is None or key < best_key:
            best, best_key = uid_, key
    return best


def apply(graph: Graph, answers: dict[Group, Answer]) -> Graph:
    """A refined copy; the graph being displayed is never mutated underneath a renderer."""
    index = graph.index
    out = dataclasses.replace(
        graph,
        nodes=[dataclasses.replace(n) for n in graph.nodes],
        edges=[dataclasses.replace(e) for e in graph.edges],
    )
    if not answers or index is None:
        return out

    nodes = {n.uid: n for n in out.nodes}
    edges = {e.key: e for e in out.edges}
    before = {end for e in out.edges for end in (e.src, e.dst)}
    groups = uncertain_groups(graph)

    for group, answer in answers.items():
        src, _name = group
        old = groups.get(group, ())
        for edge in old:
            edges.pop(edge.key, None)
        for target in sorted(answer.targets):
            if target == src:
                continue
            if target not in nodes:
                definition = index.defs[target]
                nodes[target] = Node(
                    uid=target,
                    label=definition.qualname,
                    path=index.path_of[target],
                    kind=definition.kind,
                    touched=False,
                    fan_in=index.fan_in(target),
                    fan_in_shared=index.fan_in_is_shared(target),
                )
            line = next((e.line for e in old if e.dst == target), None)
            edges[(src, target, CALL)] = Edge(src, target, CALL, RESOLVED, line=line)

    out.edges = list(edges.values())
    for node in nodes.values():
        node.degree = 0
    for edge in out.edges:
        for end in (edge.src, edge.dst):
            if end in nodes:
                nodes[end].degree += 1
    # Context that was only there because of an edge we just disproved has no reason to stay.
    out.nodes = [
        n for n in nodes.values() if n.touched or n.degree or n.uid not in before
    ]
    for edge in out.edges:
        edge.order = None
    _number_calls(out)
    return out


ONE_SHOT_BUDGET = 30.0


def upgrade(
    graph: Graph, sessions: Sessions, root: Path, budget: float = ONE_SHOT_BUDGET
) -> tuple[Graph, Stats]:
    """Synchronous end to end — what one-shot `scopia --lsp` uses.

    Bounded by `budget` seconds: a command someone is waiting on must not run for minutes
    on a huge diff. Watch mode has no such limit, since it resolves in the background.
    """
    began = time.monotonic()
    stats = Stats(uncertain_before=len(graph.uncertain_edges))
    stats.groups = len(uncertain_groups(graph))
    deadline = began + budget
    answers = ask(graph, sessions, root, lambda: time.monotonic() > deadline)
    stats.out_of_time = time.monotonic() > deadline and len(answers) < stats.groups
    refined = apply(graph, answers)
    stats.answered = len(answers)
    stats.uncertain_after = len(refined.uncertain_edges)
    stats.seconds = time.monotonic() - began
    stats.warmup = sessions.warmups()
    stats.notes = list(sessions.notes)
    return refined, stats


class AsyncUpgrader:
    """Resolves in a background thread so a redraw never waits on a language server.

    `submit` the newest graph; `poll` returns its refinement once ready. A graph submitted
    while an older one is still being resolved supersedes it — answers about a file the
    agent has since rewritten are not worth finishing.
    """

    def __init__(self, sessions: Sessions, root: Path) -> None:
        self._sessions = sessions
        self._root = root
        self._cond = threading.Condition()
        self._job: Graph | None = None
        self._generation = 0
        self._ready: tuple[int, Graph, Stats] | None = None
        self._stop = False
        self.last_stats: Stats | None = None
        self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def submit(self, graph: Graph) -> None:
        with self._cond:
            self._generation += 1
            self._job = graph
            self._ready = None
            self._cond.notify()

    def poll(self) -> tuple[Graph, Stats] | None:
        with self._cond:
            ready, self._ready = self._ready, None
        if ready is None:
            return None
        generation, graph, stats = ready
        with self._cond:
            if generation != self._generation:
                return None
        self.last_stats = stats
        return graph, stats

    def close(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()
        self._sessions.close()

    def _work(self) -> None:
        while True:
            with self._cond:
                while self._job is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                graph, generation = self._job, self._generation
                self._job = None
            began = time.monotonic()
            stats = Stats(uncertain_before=len(graph.uncertain_edges))
            stats.groups = len(uncertain_groups(graph))
            try:
                answers = ask(
                    graph,
                    self._sessions,
                    self._root,
                    lambda g=generation: g != self._generation,
                )
                refined = apply(graph, answers)
            except Exception:  # a resolver bug must never take the watcher down
                continue
            stats.answered = len(answers)
            stats.uncertain_after = len(refined.uncertain_edges)
            stats.seconds = time.monotonic() - began
            stats.warmup = self._sessions.warmups()
            stats.notes = list(self._sessions.notes)
            with self._cond:
                if generation == self._generation and answers:
                    self._ready = (generation, refined, stats)
