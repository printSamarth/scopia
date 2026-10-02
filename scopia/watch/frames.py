"""The shared heart of both watch drivers: *when* is there something new to show.

Terminal and browser differ only in how they draw. Everything that decides whether to
draw — the debounce, the held-for-syntax-errors state, highlight decay, and a language
server's refinement arriving late — lives here once, so the two cannot disagree.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from ..graph.model import Graph
from .fresh import Highlights
from .loop import POLL_SECONDS, Watcher


@dataclass
class Frame:
    graph: Graph | None
    held: tuple[str, ...] = ()
    note: str = ""
    """One line about the language server, when `--lsp` is on."""
    decay_only: bool = False
    """Nothing changed but a freshness highlight expiring.

    The terminal redraws for this — the `just now` marker has to disappear. The live
    page must not: a republish swaps the whole SVG, and with highlights expiring one by
    one that is a visible blink per changed symbol.
    """


def frames(
    watcher: Watcher,
    *,
    interval: float = POLL_SECONDS,
    ttl: float | None = None,
    upgrader=None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[Frame]:
    highlights = Highlights() if ttl is None else Highlights(ttl)
    base: Graph | None = None  # newest tree-sitter graph
    shown: Graph | None = None  # what is on screen: `base`, or its refinement
    note = ""
    try:
        while True:
            tick = watcher.step()
            now = clock()
            redraw = tick.redraw

            if tick.graph is not None and tick.graph is not base:
                base = shown = tick.graph
                highlights.update(base, now)
                if upgrader is not None:
                    # Draw from tree-sitter now; the server's answers upgrade it later.
                    upgrader.submit(base)

            if upgrader is not None:
                refined = upgrader.poll()
                if refined is not None:
                    shown, stats = refined
                    note = _note(stats)
                    redraw = True

            decayed = shown is not None and highlights.apply(shown, now)
            if redraw or decayed:
                yield Frame(shown, tick.held, note, decay_only=not redraw)
            sleep(interval)
    finally:
        if upgrader is not None:
            upgrader.close()


def _note(stats) -> str:
    bits = [stats.summary()]
    bits += stats.notes
    return " · ".join(bits)
