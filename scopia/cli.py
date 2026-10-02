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
        "  scopia main.. --html r.html  write a self-contained report\n"
        "  scopia --watch               keep the graph current while an agent writes\n"
        "  scopia --watch --html        the same, as a live page in your browser\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "revisions",
        nargs="?",
        help="revision or range (default: working tree vs HEAD)",
    )
    parser.add_argument(
        "--html",
        metavar="PATH",
        nargs="?",
        const="",
        default=None,
        help="write a self-contained HTML report; with --watch, serve it live instead "
        "(PATH, if given, is also rewritten on each update)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="follow the working tree and redraw as files settle; Ctrl-C to quit",
    )
    parser.add_argument(
        "--lsp",
        action="store_true",
        help="sharpen name-matched edges with a language server when one is installed "
        "(Python: jedi-language-server). Falls back silently to static analysis.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="with --watch --html, print the URL without opening a browser",
    )
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
        "--to-entry",
        action="store_true",
        help="climb callers until each chain reaches an entry point — the route, job "
        "or command that triggers the changed code",
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

    if opts.watch:
        problem = _watch_conflict(opts)
        if problem:
            print(f"scopia: {problem}", file=sys.stderr)
            return 2
    elif opts.html == "":
        print(
            "scopia: --html needs a PATH to write to (or add --watch to serve it live).",
            file=sys.stderr,
        )
        return 2

    try:
        repo = Repo.discover(Path.cwd())
    except GitError as exc:
        print(f"scopia: {exc}", file=sys.stderr)
        print("scopia needs to run inside a git repository.", file=sys.stderr)
        return 2

    if opts.watch:
        try:
            return _watch(repo, opts)
        except KeyboardInterrupt:
            # An impatient second Ctrl-C lands while the first is still tearing down.
            # Quitting is what was asked for either way; a stack trace just makes a
            # normal exit look like a crash.
            return 0

    try:
        graph = build_graph(
            repo,
            parse_revisions(opts.revisions),
            hops=opts.hops,
            max_nodes=opts.max_nodes,
            jobs=opts.jobs,
            utility_fan_in=opts.utility,
            exclude=tuple(opts.exclude),
            to_entry=opts.to_entry,
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

    if opts.lsp:
        graph = _refine(repo, graph)

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


def _watch_conflict(opts: argparse.Namespace) -> str:
    if opts.revisions:
        return "--watch follows the working tree against HEAD; drop the revision."
    if opts.format != "text":
        return "--watch draws the text report or the live page; --format does not apply."
    return ""


def _graceful_signals() -> None:
    """Turn SIGTERM and SIGHUP (a closed terminal) into the same clean exit as Ctrl-C.

    Left alone they kill the process without running `finally` blocks, which strands any
    language server as an orphan — and not every server exits when its stdin closes.
    """
    import signal

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)


def _watch(repo: Repo, opts: argparse.Namespace) -> int:
    from .watch.loop import Watcher

    _graceful_signals()

    # The graph starts blank and fills in as work lands. Diffing against HEAD would
    # open with everything already uncommitted — in a dirty tree that is hundreds of
    # nodes, and the file just written is lost among them.
    session_start = repo.working_tree_shas()

    def changed_since_start() -> frozenset[str]:
        now = repo.working_tree_shas()
        moved = {path for path, sha in now.items() if session_start.get(path) != sha}
        moved |= {path for path in session_start if path not in now}  # deleted
        return frozenset(moved)

    def build() -> Graph:
        # Watch has no gate: a small change is exactly what you want to see arrive.
        return build_graph(
            repo,
            parse_revisions(None),
            hops=opts.hops,
            max_nodes=opts.max_nodes,
            jobs=1,
            utility_fan_in=opts.utility,
            exclude=tuple(opts.exclude),
            to_entry=opts.to_entry,
            only_paths=changed_since_start(),
        )

    watcher = Watcher(repo, build)
    upgrader = _upgrader(repo, opts)
    if opts.html is None:
        from .render import text
        from .watch import terminal

        color = False if opts.no_color else text.supports_color()
        return terminal.run(watcher, color=color, upgrader=upgrader)

    from .watch import live
    from .watch.server import LiveServer

    server = LiveServer()
    server.start()
    print(f"scopia watching {repo.root}\nlive page: {server.url}  (Ctrl-C to quit)")
    if not opts.no_open:
        import webbrowser

        webbrowser.open(server.url)
    return live.run(
        watcher,
        server,
        upgrader=upgrader,
        also_write=Path(opts.html) if opts.html else None,
    )


def _upgrader(repo: Repo, opts: argparse.Namespace):
    if not opts.lsp:
        return None
    from .lsp.resolve import AsyncUpgrader
    from .lsp.session import Sessions

    return AsyncUpgrader(Sessions(repo.root), repo.root)


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


def _refine(repo: Repo, graph: Graph) -> Graph:
    from .lsp.resolve import upgrade
    from .lsp.session import Sessions

    sessions = Sessions(repo.root)
    try:
        refined, stats = upgrade(graph, sessions, repo.root)
    finally:
        sessions.close()  # never leave a server running behind a one-shot command
    print(f"scopia: {stats.summary()} ({stats.seconds:.1f}s)", file=sys.stderr)
    for note in stats.notes:
        print(f"scopia: {note}", file=sys.stderr)
    return refined


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
