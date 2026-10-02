"""Change detection, debounce and the parse-clean gate.

No dependency on a file-watching library. `git status --porcelain` names every dirty and
untracked path in ~10ms, then a `stat()` of those paths catches the case porcelain alone
cannot: editing a file that is already dirty leaves its status line unchanged.

The loop is deliberately free of I/O it does not own — time is injected, and the build is
a callback — so every behaviour below is testable with a fake clock and a temp repo.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..graph.model import Graph
from ..lang import adapter_for, parses_cleanly
from ..vcs.git import GitError, Repo

POLL_SECONDS = 1.0
QUIET_SECONDS = 0.4

Fingerprint = dict[str, tuple[int, int] | None]
"""path -> (mtime_ns, size), or None when the path no longer exists."""


@dataclass
class Tick:
    """What one poll produced. `redraw` is the only thing a driver needs to act on."""

    redraw: bool = False
    graph: Graph | None = None
    """The newest graph, whether or not this tick produced it."""
    held: tuple[str, ...] = ()
    """Files that do not parse yet. While non-empty the last good graph stays on screen."""


class Watcher:
    def __init__(
        self,
        repo: Repo,
        build: Callable[[], Graph],
        *,
        quiet: float = QUIET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repo = repo
        self._build = build
        self._quiet = quiet
        self._clock = clock

        self.graph: Graph | None = None
        self.held: tuple[str, ...] = ()
        self._seen: Fingerprint | None = None  # most recent fingerprint observed
        self._built: Fingerprint = {}  # fingerprint the current graph was built from
        self._changed_at = 0.0
        self._pending = False
        self._last_dict: dict | None = None

    def fingerprint(self) -> Fingerprint:
        out: Fingerprint = {}
        for path in self.repo.dirty_paths():
            try:
                st = (self.repo.root / path).stat()
            except OSError:
                out[path] = None
            else:
                out[path] = (st.st_mtime_ns, st.st_size)
        return out

    def step(self) -> Tick:
        """One poll. Cheap when nothing changed; never blocks beyond a build."""
        now = self._clock()
        try:
            fp = self.fingerprint()
        except GitError:
            # `.git/index.lock` is held for a moment during an agent's own `git add`.
            return Tick(graph=self.graph, held=self.held)

        if self._seen is None:
            # First look: draw immediately. Nobody wants to stare at a blank screen for the
            # debounce window, and there is no "changing" to wait out yet.
            self._seen = fp
            return self._rebuild(fp, gate=False)

        if fp != self._seen:
            self._seen = fp
            self._changed_at = now
            self._pending = True
            return Tick(graph=self.graph, held=self.held)

        if self._pending and now - self._changed_at >= self._quiet:
            return self._rebuild(fp, gate=True)
        return Tick(graph=self.graph, held=self.held)

    def _rebuild(self, fp: Fingerprint, *, gate: bool) -> Tick:
        previous_held = self.held
        if gate:
            broken = self._unparseable(fp)
            if broken:
                # Hold the last good graph. Stay pending: the fingerprint only moves again
                # when the agent writes, and that write re-arms the debounce on its own.
                self.held = broken
                return Tick(redraw=broken != previous_held, graph=self.graph, held=broken)

        try:
            graph = self._build()
        except GitError:
            return Tick(graph=self.graph, held=self.held)

        self._pending = False
        self._built = fp
        self.held = ()
        snapshot = graph.to_dict()
        # A formatter rewrote a file, or it was touched without changing: the stat moved
        # but the graph did not. Redrawing would flash the screen to show nothing new.
        if snapshot == self._last_dict:
            return Tick(redraw=bool(previous_held), graph=self.graph)
        self._last_dict = snapshot
        self.graph = graph
        return Tick(redraw=True, graph=graph)

    def _unparseable(self, fp: Fingerprint) -> tuple[str, ...]:
        """Changed files in a supported language that do not parse cleanly right now."""
        broken = []
        for path, stat in fp.items():
            if stat is None or self._built.get(path) == stat or adapter_for(path) is None:
                continue
            try:
                source = (self.repo.root / path).read_bytes()
            except OSError:
                continue
            if not parses_cleanly(path, source):
                broken.append(path)
        return tuple(sorted(broken))
