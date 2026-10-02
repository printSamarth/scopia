from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scopia.graph.build import build_graph
from scopia.graph.model import AMBIGUOUS, RESOLVED
from scopia.lsp import resolve
from scopia.lsp import session as session_mod
from scopia.lsp.client import LspClient, LspError
from scopia.lsp.session import Sessions
from scopia.vcs.git import Repo, parse_revisions

from .conftest import _make_repo

FAKE = str(Path(__file__).with_name("fake_lsp_server.py"))
RANGE = "HEAD~1..HEAD"

BASE = {
    "a.py": "class A:\n    def run(self):\n        return 1\n",
    "b.py": "class B:\n    def run(self):\n        return 2\n",
    "main.py": "def go(x):\n    return x\n",
}
HEAD = {**BASE, "main.py": "def go(x):\n    return x.run()\n"}
CALL_POS = "main.py:1:13"  # `    return x.run()` — the `r` of run


@pytest.fixture
def repo(tmp_path_factory) -> Repo:
    return _make_repo(tmp_path_factory, "lsprepo", BASE, HEAD)


def _fake(tmp_path, monkeypatch, **config) -> Path:
    cfg = tmp_path / "fake.json"
    cfg.write_text(json.dumps(config))
    monkeypatch.setitem(session_mod.SERVERS, "python", [[sys.executable, FAKE, str(cfg)]])
    return cfg


def _graph(repo):
    return build_graph(repo, parse_revisions(RANGE), hops=1, utility_fan_in=0, jobs=1)


# --- client --------------------------------------------------------------------------


def test_client_handshake_reads_capabilities_and_answers_server_requests(tmp_path):
    log = tmp_path / "log"
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"definitions": {}, "log": str(log)}))
    with LspClient([sys.executable, FAKE, str(cfg)], tmp_path) as client:
        client.initialize()
        assert client.capabilities["definitionProvider"] is True
        client.request(
            "textDocument/definition",
            {"textDocument": {"uri": tmp_path.resolve().as_uri() + "/x.py"},
             "position": {"line": 0, "character": 0}},
        )
        client.close()
    received = [json.loads(line) for line in log.read_text().splitlines()]
    # The server asked us for something mid-flight; an unanswered request stalls real servers.
    assert {"jsonrpc": "2.0", "id": 9001, "result": None} in received
    assert [m.get("method") for m in received if "method" in m][-2:] == ["shutdown", "exit"]


def test_client_times_out_on_a_silent_server_and_leaves_no_process(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mode": "silent"}))
    client = LspClient([sys.executable, FAKE, str(cfg)], tmp_path)
    client.initialize()
    with pytest.raises(LspError, match="timed out"):
        client.request("textDocument/definition", {
            "textDocument": {"uri": tmp_path.as_uri() + "/x.py"},
            "position": {"line": 0, "character": 0}}, timeout=0.5)
    client.close()
    assert client._proc.poll() is not None


def test_client_reports_a_crashed_server_instead_of_hanging(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mode": "crash"}))
    client = LspClient([sys.executable, FAKE, str(cfg)], tmp_path)
    client.initialize()
    started = time.monotonic()
    with pytest.raises(LspError):
        client.request("textDocument/definition", {
            "textDocument": {"uri": tmp_path.as_uri() + "/x.py"},
            "position": {"line": 0, "character": 0}}, timeout=10)
    assert time.monotonic() - started < 5
    assert not client.alive()
    client.close()


def test_missing_binary_is_an_lsp_error(tmp_path):
    with pytest.raises(LspError):
        LspClient(["definitely-not-a-language-server"], tmp_path)


# --- resolution ----------------------------------------------------------------------


def test_static_pass_alone_calls_it_ambiguous(repo):
    graph = _graph(repo)
    calls = [e for e in graph.edges if e.src == "main.py::go"]
    assert {e.dst for e in calls} == {"a.py::A.run", "b.py::B.run"}
    assert all(e.provenance == AMBIGUOUS or e.provenance == "inferred" for e in calls)


def test_server_answer_collapses_fan_out_to_one_resolved_edge(repo, tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, definitions={CALL_POS: [["a.py", 1]]})
    graph = _graph(repo)
    sessions = Sessions(repo.root)
    refined, stats = resolve.upgrade(graph, sessions, repo.root)
    sessions.close()

    calls = [e for e in refined.edges if e.src == "main.py::go"]
    assert [(e.dst, e.provenance) for e in calls] == [("a.py::A.run", RESOLVED)]
    assert stats.uncertain_before == 2 and stats.uncertain_after == 0
    # The orphaned candidate was only context because of the edge just disproved.
    assert "b.py::B.run" not in {n.uid for n in refined.nodes}
    # The graph on screen is never mutated underneath a renderer.
    assert len([e for e in graph.edges if e.src == "main.py::go"]) == 2


def test_definition_outside_the_repo_drops_the_name_matched_edges(repo, tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, definitions={CALL_POS: "external"})
    sessions = Sessions(repo.root)
    refined, _stats = resolve.upgrade(_graph(repo), sessions, repo.root)
    sessions.close()
    assert not [e for e in refined.edges if e.src == "main.py::go"]


def test_an_empty_answer_leaves_the_edge_alone(repo, tmp_path, monkeypatch):
    """No answer is not 'no such call' — keep the static guess rather than invent a fact."""
    _fake(tmp_path, monkeypatch, definitions={})
    sessions = Sessions(repo.root)
    refined, stats = resolve.upgrade(_graph(repo), sessions, repo.root)
    sessions.close()
    assert len([e for e in refined.edges if e.src == "main.py::go"]) == 2
    assert stats.answered == 0


def test_timeouts_leave_the_graph_as_static_analysis_drew_it(repo, tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, mode="silent", definitions={})
    monkeypatch.setattr(LspClient, "default_timeout", 0.3)
    sessions = Sessions(repo.root)
    refined, stats = resolve.upgrade(_graph(repo), sessions, repo.root)
    sessions.close()
    assert stats.answered == 0
    assert len([e for e in refined.edges if e.src == "main.py::go"]) == 2


def test_missing_server_degrades_with_a_note_not_an_error(repo, monkeypatch):
    monkeypatch.setitem(session_mod.SERVERS, "python", [["no-such-language-server"]])
    sessions = Sessions(repo.root)
    refined, stats = resolve.upgrade(_graph(repo), sessions, repo.root)
    assert len(refined.edges) == len(_graph(repo).edges)
    assert any("not found" in n for n in stats.notes)


def test_server_without_definition_support_is_not_used(repo, tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, definition=False, definitions={CALL_POS: [["a.py", 1]]})
    sessions = Sessions(repo.root)
    _refined, stats = resolve.upgrade(_graph(repo), sessions, repo.root)
    assert any("go-to-definition" in n for n in stats.notes)
    assert stats.answered == 0


def test_cli_lsp_flag_runs_and_reports_on_stderr(repo, tmp_path, monkeypatch, capsys):
    from scopia.cli import main

    _fake(tmp_path, monkeypatch, definitions={CALL_POS: [["a.py", 1]]})
    monkeypatch.chdir(repo.root)
    assert main([RANGE, "--lsp", "--always", "--no-color", "--hops", "1", "--utility", "0"]) == 0
    captured = capsys.readouterr()
    assert "language server: resolved 1 of 1" in captured.err
    assert "main.go" in captured.out


def test_no_orphan_server_after_one_shot(repo, tmp_path, monkeypatch):
    log = tmp_path / "log"
    _fake(tmp_path, monkeypatch, definitions={CALL_POS: [["a.py", 1]]}, log=str(log))
    sessions = Sessions(repo.root)
    resolve.upgrade(_graph(repo), sessions, repo.root)
    pid_client = next(iter(sessions._sessions.values())).client
    sessions.close()
    assert pid_client._proc.poll() is not None


# --- async ---------------------------------------------------------------------------


def test_async_upgrader_delivers_a_refined_graph_without_blocking_submit(
    repo, tmp_path, monkeypatch
):
    _fake(tmp_path, monkeypatch, definitions={CALL_POS: [["a.py", 1]]})
    upgrader = resolve.AsyncUpgrader(Sessions(repo.root), repo.root)
    try:
        started = time.monotonic()
        upgrader.submit(_graph(repo))
        assert time.monotonic() - started < 0.5  # drawing never waits on the server
        got = None
        deadline = time.monotonic() + 15
        while got is None and time.monotonic() < deadline:
            got = upgrader.poll()
            time.sleep(0.05)
        assert got is not None
        refined, stats = got
        assert stats.uncertain_after == 0
    finally:
        upgrader.close()


def test_frames_draw_static_first_then_upgrade_in_place(repo):
    from scopia.watch.frames import frames
    from scopia.watch.loop import Watcher

    base = _graph(repo)
    refined = resolve.apply(base, {("main.py::go", "run"): resolve.Answer({"a.py::A.run"})})

    class Stub:
        def __init__(self):
            self.submitted, self.calls, self.closed = [], 0, False

        def submit(self, graph):
            self.submitted.append(graph)

        def poll(self):
            self.calls += 1
            if self.calls >= 2:
                stats = resolve.Stats(groups=1, answered=1, uncertain_before=2)
                return refined, stats
            return None

        def close(self):
            self.closed = True

    stub = Stub()
    watcher = Watcher(repo, lambda: base, clock=lambda: 0.0)
    gen = frames(watcher, upgrader=stub, sleep=lambda _s: None, clock=lambda: 0.0)
    first, second = next(gen), next(gen)
    assert first.graph is base and stub.submitted == [base]
    assert second.graph is refined and "resolved 1 of 1" in second.note
    gen.close()
    assert stub.closed  # the server is released however the loop ends


def test_subprocess_is_really_gone(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{}")
    client = LspClient([sys.executable, FAKE, str(cfg)], tmp_path)
    pid = client._proc.pid
    client.close()
    out = subprocess.run(["ps", "-p", str(pid)], capture_output=True)
    assert out.returncode != 0


def test_one_shot_stops_at_its_time_budget_and_says_so(repo, tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, definitions={CALL_POS: [["a.py", 1]]})
    sessions = Sessions(repo.root)
    refined, stats = resolve.upgrade(_graph(repo), sessions, repo.root, budget=0.0)
    sessions.close()
    assert stats.out_of_time and "time budget" in stats.summary()
    assert len([e for e in refined.edges if e.src == "main.py::go"]) == 2  # unchanged
