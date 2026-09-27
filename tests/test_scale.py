"""Requirement 2: nothing may scale with repo size.

These assert *shape*, not wall-clock — timing tests are flaky on shared CI runners. The
things that actually went wrong during development were all countable: a subprocess per
file, and tens of thousands of nodes built only to be thrown away by the cap.
"""

from __future__ import annotations

import subprocess

import pytest

from scopia.graph.build import build_graph
from scopia.index.repo import build_index
from scopia.vcs.git import Repo, parse_revisions

N_FILES = 120
N_FUNCS = 12
N_TOUCHED = 40


@pytest.fixture(scope="module")
def wide_repo(tmp_path_factory) -> Repo:
    root = tmp_path_factory.mktemp("wide")

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q")
    for i in range(N_FILES):
        path = root / f"pkg{i // 20}" / f"mod{i}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = ["from pkg0.mod0 import shared_helper\n"]
        if i == 0:
            body.append("def shared_helper(x):\n    return x * 2\n")
        for j in range(N_FUNCS):
            body.append(f"def fn_{i}_{j}(x):\n    y = shared_helper(x)\n    return y + {j}\n")
        path.write_text("\n".join(body))
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")

    for i in range(N_TOUCHED):
        path = root / f"pkg{i // 20}" / f"mod{i}.py"
        path.write_text(path.read_text().replace("    return y + 0\n", "    return y + 9\n", 1))
    helper = root / "pkg0" / "mod0.py"
    helper.write_text(helper.read_text().replace("    return x * 2\n", "    return x * 3\n", 1))
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "wide change")
    return Repo(root)


def test_git_calls_do_not_scale_with_file_count(wide_repo, monkeypatch):
    """Regression: one `git show` per changed file per side made a fully-cached run as
    slow as a cold one. Blob access must be batched."""
    import scopia.vcs.git as git_mod

    calls = []
    original = subprocess.run

    def counting_run(args, **kwargs):
        if args and args[0] == "git":
            calls.append(args[1])
        return original(args, **kwargs)

    monkeypatch.setattr(git_mod.subprocess, "run", counting_run)
    build_graph(wide_repo, parse_revisions("HEAD~1..HEAD"))

    assert len(calls) < 20, f"{len(calls)} git subprocesses for {N_TOUCHED} changed files"


def test_graph_size_tracks_the_diff_not_the_repo(wide_repo):
    graph = build_graph(wide_repo, parse_revisions("HEAD~1..HEAD"))
    total_symbols = N_FILES * N_FUNCS
    assert len(graph.nodes) <= 60
    assert len(graph.nodes) < total_symbols / 10


def test_hot_symbol_does_not_drag_in_the_whole_repo(wide_repo):
    """`shared_helper` is called from every function in the repo.

    It must appear with its true fan-in, and symbols that changed and call it keep their
    edges — but the hundreds of *untouched* callers must not each be pulled in as a
    context node. The fan-in number already carries that information.
    """
    graph = build_graph(wide_repo, parse_revisions("HEAD~1..HEAD"), max_nodes=500)
    helper = next((n for n in graph.nodes if n.label == "shared_helper"), None)
    assert helper is not None
    assert helper.fan_in > 100, "fan-in should still report the true caller count"

    by_uid = {n.uid: n for n in graph.nodes}
    context_callers = [
        e for e in graph.edges if e.dst == helper.uid and not by_uid[e.src].touched
    ]
    assert len(context_callers) <= 12, "untouched callers must be summarised, not drawn"


def test_warm_index_parses_nothing(wide_repo):
    import shutil

    shutil.rmtree(wide_repo.root / ".git" / "scopia", ignore_errors=True)
    cold = build_index(wide_repo)
    warm = build_index(wide_repo)
    assert cold.files_parsed == cold.files_indexed
    assert warm.files_parsed == 0
