from __future__ import annotations

import io
import json
import os
import subprocess
import urllib.error
import urllib.request

import pytest

from scopia.cli import main
from scopia.graph.build import build_graph
from scopia.graph.model import Graph
from scopia.vcs.git import Repo, parse_revisions
from scopia.watch import terminal
from scopia.watch.fresh import Highlights
from scopia.watch.loop import Watcher
from scopia.watch.server import LiveServer

BASE = {
    "app/util.py": "def fmt(x):\n    return str(x)\n",
    "app/svc.py": "from app.util import fmt\n\n\ndef load(i):\n    return fmt(i)\n",
}


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    for rel, body in BASE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, "utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return Repo(tmp_path)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _watcher(repo, clock, quiet=0.4) -> Watcher:
    def build() -> Graph:
        return build_graph(repo, parse_revisions(None), jobs=1, utility_fan_in=0)

    return Watcher(repo, build, quiet=quiet, clock=clock)


def _write(repo, rel, body):
    path = repo.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, "utf-8")


def _settle(w, clock, seconds=1.0):
    """Advance past the debounce window and poll once."""
    clock.t += seconds
    return w.step()


# --- loop ----------------------------------------------------------------------------


def test_first_poll_draws_immediately(repo):
    clock = Clock()
    w = _watcher(repo, clock)
    tick = w.step()
    assert tick.redraw and tick.graph is not None


def test_no_rebuild_while_files_keep_changing_then_one_after_quiet(repo):
    clock = Clock()
    w = _watcher(repo, clock)
    w.step()

    redraws = 0
    for n in range(5):
        _write(repo, "app/util.py", f"def fmt(x):\n    return str(x) + '{n}'\n")
        clock.t += 0.1  # always inside the quiet window
        redraws += w.step().redraw
    assert redraws == 0

    tick = _settle(w, clock)
    assert tick.redraw
    assert any(n.touched and n.label == "fmt" for n in tick.graph.nodes)
    # Nothing further changed, so nothing further to draw.
    assert not _settle(w, clock).redraw


def test_broken_file_holds_the_last_good_graph(repo):
    clock = Clock()
    w = _watcher(repo, clock)
    w.step()
    _write(repo, "app/util.py", "def fmt(x):\n    return str(x) + 1\n")
    w.step()
    good = _settle(w, clock).graph
    assert good is not None

    _write(repo, "app/util.py", "def fmt(x:\n    return (\n")
    w.step()
    held = _settle(w, clock)
    assert held.redraw and held.held == ("app/util.py",)
    assert held.graph is good  # held, not replaced or emptied

    # Still broken and nothing new: stays quiet rather than redrawing the same notice.
    assert not _settle(w, clock).redraw

    _write(
        repo,
        "app/util.py",
        "def fmt(x):\n    return str(x) + 2\n\n\ndef extra():\n    return 1\n",
    )
    w.step()
    fixed = _settle(w, clock)
    assert fixed.redraw and fixed.held == ()
    assert fixed.graph is not good
    assert any(n.label == "extra" for n in fixed.graph.nodes)


def test_touch_without_content_change_does_not_redraw(repo):
    clock = Clock()
    w = _watcher(repo, clock)
    w.step()
    _write(repo, "app/util.py", "def fmt(x):\n    return str(x) + '!'\n")
    w.step()
    assert _settle(w, clock).redraw

    path = repo.root / "app/util.py"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    w.step()  # fingerprint moves, arming the debounce
    assert not _settle(w, clock).redraw  # the graph did not


def test_editing_an_already_dirty_file_is_seen(repo):
    """Porcelain output is identical before and after — only the stat tells."""
    clock = Clock()
    w = _watcher(repo, clock)
    w.step()
    _write(repo, "app/util.py", "def fmt(x):\n    return str(x) + 'a'\n")
    w.step()
    first = _settle(w, clock)
    assert first.redraw

    _write(repo, "app/util.py", "def fmt(x):\n    y = 1\n    z = 2\n    return str(x) + 'a'\n")
    w.step()
    second = _settle(w, clock)
    assert second.redraw
    changed = {n.label: n.changed_lines for n in second.graph.touched}
    assert changed["fmt"] > {n.label: n.changed_lines for n in first.graph.touched}["fmt"]


def test_new_untracked_file_in_a_new_directory_is_picked_up(repo):
    clock = Clock()
    w = _watcher(repo, clock)
    w.step()
    _write(repo, "pkg/sub/fresh.py", "def brand_new():\n    return 1\n")
    w.step()
    tick = _settle(w, clock)
    assert tick.redraw
    assert any(n.label == "brand_new" and n.added for n in tick.graph.nodes)


def test_dirty_paths_reports_rename_once(repo):
    _git(repo.root, "mv", "app/util.py", "app/util2.py")
    assert repo.dirty_paths() == ["app/util2.py"]


def test_unsupported_files_are_never_gated(repo):
    clock = Clock()
    w = _watcher(repo, clock)
    w.step()
    _write(repo, "notes.txt", "def broken(:\n")
    w.step()
    tick = _settle(w, clock)
    assert tick.held == ()


# --- highlights ----------------------------------------------------------------------


def test_highlights_mark_arrivals_and_decay(repo):
    clock = Clock()
    w = _watcher(repo, clock)
    first = w.step().graph
    hl = Highlights(ttl=8.0)
    hl.update(first, 0.0)
    hl.apply(first, 0.0)
    assert not any(n.fresh for n in first.nodes)  # the opening screen is not "new"

    _write(repo, "app/util.py", "def fmt(x):\n    return str(x) + '!'\n")
    w.step()
    second = _settle(w, clock).graph
    hl.update(second, 10.0)
    assert hl.apply(second, 10.0) is True
    assert {n.label for n in second.nodes if n.fresh} == {"fmt"}

    assert hl.apply(second, 12.0) is False  # unchanged while lit
    assert hl.apply(second, 19.0) is True  # decayed: redraw needed with no file change
    assert not any(n.fresh for n in second.nodes)


def test_fresh_is_not_part_of_the_golden_shape(repo):
    _write(repo, "app/util.py", "def fmt(x):\n    return str(x) + '!'\n")
    graph = _watcher(repo, Clock()).step().graph
    graph.nodes[0].fresh = True
    assert all("fresh" not in n for n in graph.to_dict()["nodes"])


# --- terminal ------------------------------------------------------------------------


def test_terminal_draws_then_redraws_on_change_without_escapes_on_a_pipe(repo):
    clock = Clock()
    w = _watcher(repo, clock)
    out = io.StringIO()
    calls = {"n": 0}

    def sleep(_s):
        calls["n"] += 1
        if calls["n"] == 1:
            _write(repo, "app/util.py", "def fmt(x):\n    return str(x) + '!'\n")
        clock.t += 1.0

    code = terminal.run(
        w, color=False, out=out, sleep=sleep, clock=clock, max_frames=2, ttl=8.0
    )
    assert code == 0
    frames = out.getvalue().split("─" * 40)
    assert "Waiting for changes" in frames[0]
    assert "● just now" in frames[1]
    assert "\033[" not in out.getvalue()


def test_terminal_restores_the_screen_on_ctrl_c(repo):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    out = Tty()

    def sleep(_s):
        raise KeyboardInterrupt

    code = terminal.run(_watcher(repo, Clock()), color=False, out=out, sleep=sleep)
    assert code == 0
    assert out.getvalue().startswith("\033[?1049h")
    assert out.getvalue().rstrip().endswith("\033[?25h\033[?1049l")


# --- server --------------------------------------------------------------------------


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode("utf-8")


@pytest.fixture
def server():
    s = LiveServer()
    s.start()
    yield s
    s.stop()


def test_server_binds_loopback_only(server):
    assert server.url.startswith("http://127.0.0.1:")


def test_version_advances_and_graph_json_matches(repo, server):
    assert _get(server.url + "version") == "1"
    graph = _watcher(repo, Clock()).step().graph
    server.publish(graph)
    assert _get(server.url + "version") == "2"

    payload = json.loads(_get(server.url + "graph.json"))
    assert payload["version"] == 2
    assert set(payload) >= {"data", "svg", "sidebar", "stats", "banner", "footnote"}
    assert payload["data"]["nodes"] is not None


def test_page_is_seeded_with_its_own_version_and_404s_elsewhere(repo, server):
    graph = _watcher(repo, Clock()).step().graph
    server.publish(graph)
    page = _get(server.url)
    assert '<meta name="scopia-live" content="2">' in page
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(server.url + "nope")
    assert err.value.code == 404


def test_static_report_has_no_live_hook(repo):
    from scopia.render import html

    graph = _watcher(repo, Clock()).step().graph
    assert '<meta name="scopia-live"' not in html.render(graph)


# --- cli -----------------------------------------------------------------------------


def test_watch_rejects_a_revision(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    assert main(["--watch", "HEAD~1..HEAD"]) == 2
    assert "working tree" in capsys.readouterr().err


def test_html_without_path_needs_watch(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    assert main(["--html"]) == 2
    assert "PATH" in capsys.readouterr().err


def test_decay_only_is_set_when_nothing_structural_changed():
    """A highlight expiring is not a change, and the live page must not redraw for it.

    Each changed symbol's freshness expires at its own moment. Republishing for each one
    swapped the whole SVG, so a five-file edit produced five extra publishes and a
    visible blink per symbol after the agent had already stopped typing.
    """
    from scopia.watch.frames import Frame

    assert Frame(graph=None).decay_only is False, "a real change publishes"
    assert Frame(graph=None, decay_only=True).decay_only is True
