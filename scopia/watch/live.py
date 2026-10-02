"""`scopia --watch --html`: the same loop as the terminal, publishing to a browser page."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TextIO

from ..render import html as html_render
from .frames import frames
from .loop import POLL_SECONDS, Watcher
from .server import LiveServer


def run(
    watcher: Watcher,
    server: LiveServer,
    *,
    out: TextIO = sys.stdout,
    interval: float = POLL_SECONDS,
    ttl: float | None = None,
    upgrader=None,
    also_write: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_publishes: int | None = None,
) -> int:
    publishes = 0
    last_note = ""
    try:
        stream = frames(
            watcher, interval=interval, ttl=ttl, upgrader=upgrader, sleep=sleep, clock=clock
        )
        with closing(stream):
            for frame in stream:
                if frame.graph is not None and not frame.decay_only:
                    server.publish(frame.graph)
                    if also_write is not None:
                        also_write.parent.mkdir(parents=True, exist_ok=True)
                        also_write.write_text(html_render.render(frame.graph), "utf-8")
                    publishes += 1
                    stamp = datetime.now().strftime("%H:%M:%S")
                    shown = len(frame.graph.touched)
                    out.write(f"{stamp}  {shown} symbol(s) changed · v{server.version}\n")
                if frame.held:
                    names = ", ".join(frame.held[:3])
                    out.write(f"          holding last good graph — not parseable: {names}\n")
                if frame.note and frame.note != last_note:
                    out.write(f"          {frame.note}\n")
                    last_note = frame.note
                out.flush()
                if max_publishes is not None and publishes >= max_publishes:
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        server.stop()
    return 0
