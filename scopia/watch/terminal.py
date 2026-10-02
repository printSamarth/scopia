"""`scopia --watch`: clear and redraw the text report on every settle."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from typing import TextIO

from ..graph.model import Graph
from ..render import text
from .frames import Frame, frames
from .loop import POLL_SECONDS, Watcher

_ALT_ON = "\033[?1049h\033[?25l"
_ALT_OFF = "\033[?25h\033[?1049l"
_CLEAR = "\033[H\033[2J"


def run(
    watcher: Watcher,
    *,
    color: bool,
    out: TextIO = sys.stdout,
    interval: float = POLL_SECONDS,
    ttl: float | None = None,
    upgrader=None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_frames: int | None = None,
) -> int:
    """Block until Ctrl-C. `max_frames` exists so tests can stop the loop deterministically."""
    interactive = bool(getattr(out, "isatty", lambda: False)())
    drawn = 0
    if interactive:
        out.write(_ALT_ON)
        out.flush()
    try:
        stream = frames(
            watcher, interval=interval, ttl=ttl, upgrader=upgrader, sleep=sleep, clock=clock
        )
        with closing(stream):
            for frame in stream:
                _draw(out, frame, color, interactive)
                drawn += 1
                if max_frames is not None and drawn >= max_frames:
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        # The terminal must come back sane whatever ended the loop: Ctrl-C, a crash, or a
        # build error. A stuck alternate buffer and a hidden cursor are the classic failure.
        if interactive:
            out.write(_ALT_OFF)
            out.flush()
    return 0


def _draw(out: TextIO, frame: Frame, color: bool, interactive: bool) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    header = f"scopia --watch · updated {stamp} · Ctrl-C to quit"
    graph: Graph | None = frame.graph
    if graph is None or not graph.nodes:
        body = "Waiting for changes in supported files…\n"
    else:
        body = text.render(graph, color=color)
    footer = ""
    if frame.held:
        held = frame.held
        names = ", ".join(held[:3]) + (f" +{len(held) - 3} more" if len(held) > 3 else "")
        footer += f"\nHolding the last good graph — not parseable yet: {names}\n"
    if frame.note:
        footer += f"\n{frame.note}\n"
    out_text = f"{header}\n\n{body}{footer}"
    if interactive:
        out.write(_CLEAR + out_text)
    else:
        # A pipe or a log: no cursor control, just separated frames.
        out.write(out_text + "\n" + "─" * 40 + "\n")
    out.flush()
