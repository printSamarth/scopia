"""Command line entry point.

Zero-config by design: bare `scopia` reviews the working tree against HEAD. Every
flag has a working default, and there is no init step or config file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .graph.build import (
    DEFAULT_HOPS,
    DEFAULT_MAX_NODES,
    DEFAULT_UTILITY_FAN_IN,
    build_graph,
)
from .graph.model import Graph
from .vcs.git import GitError, Repo, parse_revisions

GATE_MIN_FILES = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scopia",
        description="Draw one graph of what a diff changed, so a reviewer sees the shape "
        "before reading the lines.",
        epilog="Examples:\n"
        "  scopia                       review the working tree against HEAD\n"
        "  scopia HEAD~3..HEAD          review a range\n"
        "  scopia main.. --html r.html  write a self-contained report\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "revisions",
        nargs="?",
        help="revision or range (default: working tree vs HEAD)",
    )
    parser.add_argument("--html", metavar="PATH", help="write a self-contained HTML report")
    parser.add_argument(
        "--format",
        choices=("text", "mermaid", "json"),
        default="text",
        help="stdout format (default: text)",
    )
    parser.add_argument(
        "--hops",
        type=int,
        default=DEFAULT_HOPS,
        help=f"neighbour hops to expand (default: {DEFAULT_HOPS})",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=DEFAULT_MAX_NODES,
        help=f"cap on drawn nodes (default: {DEFAULT_MAX_NODES})",
    )
    parser.add_argument(
        "--utility",
        type=int,
        default=DEFAULT_UTILITY_FAN_IN,
        metavar="N",
        help="hide unchanged helpers called from more than N places "
        f"(default: {DEFAULT_UTILITY_FAN_IN}; 0 keeps everything)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip paths matching GLOB (repeatable); vendor and node_modules "
        "are skipped already",
    )
    parser.add_argument("--jobs", type=int, default=None, help="parser processes when indexing")
    parser.add_argument(
        "--always",
        action="store_true",
        help="draw even when the diff is too small to be worth a graph",
    )
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("--version", action="version", version=f"scopia {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # `scopia review <range>` is the documented long form; `scopia <range>` is the
    # same thing with less typing.
    if args and args[0] == "review":
        args = args[1:]

    opts = build_parser().parse_args(args)

    try:
        repo = Repo.discover(Path.cwd())
    except GitError as exc:
        print(f"scopia: {exc}", file=sys.stderr)
        print("scopia needs to run inside a git repository.", file=sys.stderr)
        return 2

    try:
        graph = build_graph(
            repo,
            parse_revisions(opts.revisions),
            hops=opts.hops,
            max_nodes=opts.max_nodes,
            jobs=opts.jobs,
            utility_fan_in=opts.utility,
            exclude=tuple(opts.exclude),
        )
    except GitError as exc:
        print(f"scopia: {exc}", file=sys.stderr)
        hint = _revision_hint(repo, opts.revisions, str(exc))
        if hint:
            print(hint, file=sys.stderr)
        return 2

    if not graph.nodes:
        print("No changes in supported files.")
        return 0

    if not opts.always and _below_gate(graph):
        print(_gate_message(graph))
        return 0

    if opts.html:
        _write_html(graph, Path(opts.html))

    if opts.format == "mermaid":
        from .render import mermaid

        print(mermaid.render(graph))
    elif opts.format == "json":
        print(json.dumps(graph.to_dict(), indent=2))
    else:
        from .render import text

        sys.stdout.write(text.render(graph, color=False if opts.no_color else None))
        if opts.html:
            print(f"\nHTML report: {opts.html}")

    return 0


def _revision_hint(repo: Repo, spec: str | None, error: str) -> str:
    """Explain a revision git rejected, in terms of what the user typed.

    `HEAD~1..HEAD` on a repo with one commit is the common case — the documented way to
    review a commit fails on the very first one, and raw git output does not say why.
    """
    if "unknown revision" not in error and "ambiguous argument" not in error:
        return ""
    try:
        count = int(repo.commit_count())
    except (GitError, ValueError):
        return ""
    if count <= 1 and spec and "~" in spec:
        return (
            f"This repository has {count} commit(s), so '{spec}' has no parent to "
            "compare against.\nReview the working tree with just 'scopia', or a single "
            "commit with 'scopia <sha>'."
        )
    return "Check the revision exists: git log --oneline -5"


def _below_gate(graph: Graph) -> bool:
    """A small, self-contained change is faster to read as a plain diff.

    Drawing one anyway would be the tool asserting its own relevance rather than earning it.
    """
    touched = graph.touched
    files = {n.path for n in touched}
    touched_uids = {n.uid for n in touched}
    linking = any(e.src in touched_uids and e.dst in touched_uids for e in graph.edges)
    return len(files) < GATE_MIN_FILES and not linking


def _gate_message(graph: Graph) -> str:
    touched = graph.touched
    names = ", ".join(sorted(n.label for n in touched)[:4])
    return (
        f"{len(touched)} symbol(s) touched in "
        f"{len({n.path for n in touched})} file(s), with no connections between them"
        f"{': ' + names if names else ''}.\n"
        "Too small to be worth a graph — read the diff. Use --always to draw it anyway."
    )


def _write_html(graph: Graph, path: Path) -> None:
    from .render import html as html_render

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_render.render(graph), "utf-8")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
