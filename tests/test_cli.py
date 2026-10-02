from __future__ import annotations

import json

import pytest

from scopia.cli import main

RANGE = "HEAD~1..HEAD"


@pytest.fixture
def run(monkeypatch, capsys):
    def _run(repo, *args: str) -> tuple[int, str]:
        monkeypatch.chdir(repo.root)
        code = main(list(args))
        return code, capsys.readouterr().out

    return _run


def test_default_output_names_the_shapes(run, py_fixture):
    code, out = run(py_fixture, RANGE, "--no-color")
    assert code == 0
    assert "CALL CHAINS" in out
    assert "clamp" in out
    # The section must not claim these symbols are unconnected, only that no connection
    # was found — a reviewer reads "NO EDGES" as "safe to skip".
    assert "NO CONNECTIONS FOUND" in out
    assert "NO EDGES" not in out


def test_html_report_embeds_graph_data_for_interaction(run, py_fixture, tmp_path):
    """The page carries its own data so clicking a node can work without a server."""
    target = tmp_path / "r.html"
    run(py_fixture, RANGE, "--no-color", "--html", str(target))
    page = target.read_text("utf-8")

    start = page.index('<script type="application/json" id="data">') + len(
        '<script type="application/json" id="data">'
    )
    blob = page[start : page.index("</script>", start)]
    data = json.loads(blob.replace("\\u003c", "<"))

    assert data["nodes"] and data["edges"]
    # Every node drawn in the svg must resolve in the payload, or clicking it does nothing.
    for uid in data["nodes"]:
        assert f'data-uid="{uid}"' in page or "&" in uid
    for edge in data["edges"]:
        assert edge["src"] in data["nodes"] and edge["dst"] in data["nodes"]


def test_every_touched_symbol_appears_in_the_output(run, py_fixture):
    """The header counts touched symbols; all of them must actually be shown.

    Regression: symbols reachable only through an uncertain edge were never recursed
    into, so their subtrees silently vanished while still being counted.
    """
    import json as _json

    _, payload = run(py_fixture, RANGE, "--format", "json")
    touched = [n["label"] for n in _json.loads(payload)["nodes"] if n["touched"]]

    _, text_out = run(py_fixture, RANGE, "--no-color")
    missing = [label for label in touched if label.rsplit(".", 1)[-1] not in text_out]
    assert not missing, f"counted but never displayed: {missing}"


def test_small_change_is_told_to_just_read_the_diff(run, tiny_fixture):
    """A two-line change in one file gets no graph — drawing one would be the tool
    asserting its own relevance rather than earning it."""
    code, out = run(tiny_fixture, RANGE, "--no-color")
    assert code == 0
    assert "read the diff" in out
    assert "CALL CHAINS" not in out


def test_gate_can_be_overridden(run, tiny_fixture):
    code, out = run(tiny_fixture, RANGE, "--no-color", "--always")
    assert code == 0
    assert "read the diff" not in out


def test_json_output_is_machine_readable(run, py_fixture):
    code, out = run(py_fixture, RANGE, "--format", "json")
    assert code == 0
    payload = json.loads(out)
    assert payload["nodes"] and payload["edges"]
    assert {"uid", "label", "fan_in", "touched"} <= set(payload["nodes"][0])
    known = {"static", "ambiguous", "inferred", "heuristic", "traced"}
    assert all(e["provenance"] in known for e in payload["edges"])


def test_mermaid_output_is_a_flowchart(run, py_fixture):
    code, out = run(py_fixture, RANGE, "--format", "mermaid")
    assert code == 0
    assert out.startswith("flowchart LR")
    assert "classDef" in out


def test_html_report_is_self_contained(run, py_fixture, tmp_path):
    target = tmp_path / "report.html"
    code, _ = run(py_fixture, RANGE, "--no-color", "--html", str(target))
    assert code == 0
    page = target.read_text("utf-8")
    assert page.startswith("<!doctype html>")
    assert "<svg" in page
    # Inline script is fine — fetching anything is not. The page has to open from disk,
    # offline, years from now. The SVG xmlns is a namespace identifier, never fetched.
    fetchable = page.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "http://" not in fetchable
    assert "https://" not in fetchable
    assert "<script src" not in page
    assert "<link" not in page
    assert "@import" not in page


def test_outside_a_repo_fails_clearly(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    code = main([])
    err = capsys.readouterr().err
    assert code == 2
    assert "git repository" in err


def test_review_subcommand_is_accepted(run, py_fixture):
    code, out = run(py_fixture, "review", RANGE, "--no-color")
    assert code == 0
    assert "symbols touched" in out


def test_react_report_names_components_not_dom(run, react_fixture):
    """The report is about this repo. `<form>` and `<input>` belong to the browser."""
    code, out = run(react_fixture, RANGE, "--no-color")
    assert code == 0
    assert "CheckoutForm" in out
    assert "PromoField" in out, "a rendered component is part of the change"
    assert "NOT ANALYSED" not in out, "every file in the fixture has a parser"
    body = out.split("CALL CHAINS", 1)[1]
    for tag in ("<form", "<input", "<span"):
        assert tag not in body


def test_working_tree_is_the_zero_argument_default(run, py_fixture):
    """Bare `scopia` must do something useful with no arguments at all."""
    code, out = run(py_fixture, "--no-color")
    assert code == 0
    assert out.strip()


def test_impatient_double_ctrl_c_exits_quietly(monkeypatch, py_fixture, capsys):
    """A second Ctrl-C lands while the first is still tearing down.

    Quitting is what was asked for either way; a stack trace makes a normal exit look
    like a crash, and watch mode is quit by Ctrl-C every single time.
    """
    import scopia.cli as cli

    monkeypatch.chdir(py_fixture.root)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_watch", interrupted)
    assert main(["--watch"]) == 0
    assert "Traceback" not in capsys.readouterr().err
