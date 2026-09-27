"""Repo-wide symbol and reference index.

Deliberately *not* an LSP client. A name-based index over-connects where names collide,
but that imprecision is made visible as an ambiguous fan-out edge rather than hidden
behind a confident-looking straight line — and it costs the user no extra install.
"""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..lang import adapter_for
from ..lang.base import FileFacts, SymbolDef
from ..vcs.git import Repo
from .store import FactsStore

# Below this many uncached files a process pool costs more than it saves.
_PARALLEL_THRESHOLD = 250

# Third-party and generated trees. They are rarely tracked in git, but when they are,
# nobody wants a code review to wander into them.
DEFAULT_EXCLUDES = (
    "vendor/*",
    "node_modules/*",
    "*/vendor/*",
    "*/node_modules/*",
    "*/site-packages/*",
    ".venv/*",
    "*/migrations/*",
    "dist/*",
    "build/*",
    "*.min.js",
)


def is_excluded(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def uid(path: str, qualname: str) -> str:
    return f"{path}::{qualname}"


@dataclass
class RepoIndex:
    defs: dict[str, SymbolDef] = field(default_factory=dict)
    path_of: dict[str, str] = field(default_factory=dict)
    lang_of: dict[str, str] = field(default_factory=dict)
    by_name: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    callees_of: dict[str, list[tuple[str, str, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    """caller uid -> [(callee name, call form, named class)]."""
    callers_of_name: dict[str, list[tuple[str, str, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    """callee name -> [(caller uid, call form, named class)]."""
    bases_of: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    files_indexed: int = 0
    files_parsed: int = 0

    def add_file(self, path: str, facts: FileFacts) -> None:
        self.files_indexed += 1
        adapter = adapter_for(path)
        language = adapter.name if adapter else ""
        for d in facts.defs:
            key = uid(path, d.qualname)
            self.defs[key] = d
            self.path_of[key] = path
            self.lang_of[key] = language
            self.by_name[d.name].append(key)
        for c in facts.calls:
            if c.caller is None:
                continue
            caller_uid = uid(path, c.caller)
            self.callees_of[caller_uid].append((c.callee, c.form, c.scope))
            self.callers_of_name[c.callee].append((caller_uid, c.form, c.scope))
        for b in facts.bases:
            self.bases_of[uid(path, b.holder)].append(b.base)

    def candidates(self, name: str) -> list[str]:
        """Every definition that could be the target of a call to `name`."""
        return self.by_name.get(name, [])

    def fan_in(self, symbol_uid: str) -> int:
        """Distinct callers repo-wide.

        This is what node emphasis is derived from — never diff size. A one-line change
        to something called from forty places matters more than a rewrite of something
        nobody calls, and sizing by fan-in is what makes the graph say so.
        """
        definition = self.defs.get(symbol_uid)
        if definition is None:
            return 0
        return len({c for c, _f, _s in self.callers_of_name.get(definition.name, ())})

    def fan_in_is_shared(self, symbol_uid: str) -> bool:
        """True when other definitions share this name, so the count is spread over them.

        `generate` may have 14 call sites while three classes each define one — reporting
        a flat 14 on one node implies a precision name matching cannot deliver.
        """
        definition = self.defs.get(symbol_uid)
        if definition is None:
            return False
        return len(self.by_name.get(definition.name, ())) > 1


def build_index(
    repo: Repo, *, jobs: int | None = None, exclude: tuple[str, ...] = ()
) -> RepoIndex:
    store = FactsStore(repo.root / ".git")
    index = RepoIndex()

    patterns = DEFAULT_EXCLUDES + tuple(exclude)
    shas = repo.working_tree_shas()
    targets: list[tuple[str, str]] = [  # (path, blob_sha)
        (path, sha)
        for path, sha in shas.items()
        if adapter_for(path) is not None and not is_excluded(path, patterns)
    ]

    pending: list[tuple[str, str]] = []
    for path, sha in targets:
        cached = store.get(sha)
        if cached is not None:
            index.add_file(path, cached)
        else:
            pending.append((path, sha))

    if not pending:
        return index

    results: list[tuple[str, str, FileFacts]] | None = None
    if jobs != 1 and len(pending) >= _PARALLEL_THRESHOLD:
        # Multiprocessing is environment-sensitive — spawn vs fork, frozen executables,
        # restricted sandboxes, a __main__ that cannot be re-imported. None of that is
        # worth failing a review over, so fall back to parsing in-process.
        try:
            results = _parse_parallel(repo.root, pending, jobs)
        except Exception:
            results = None
    if results is None:
        results = _parse_serial(repo.root, pending)

    for path, sha, facts in results:
        index.files_parsed += 1
        store.put(sha, facts)
        index.add_file(path, facts)
    return index


def _parse_serial(root: Path, pending: list[tuple[str, str]]) -> list[tuple[str, str, FileFacts]]:
    out = []
    for path, sha in pending:
        facts = parse_path(root, path)
        if facts is not None:
            out.append((path, sha, facts))
    return out


def _parse_parallel(
    root: Path, pending: list[tuple[str, str]], jobs: int | None
) -> list[tuple[str, str, FileFacts]]:
    from concurrent.futures import ProcessPoolExecutor

    out = []
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        parsed = pool.map(_parse_worker, [(str(root), p) for p, _ in pending], chunksize=32)
        for (path, sha), facts in zip(pending, parsed, strict=True):
            if facts is not None:
                out.append((path, sha, facts))
    return out


def _parse_worker(args: tuple[str, str]) -> FileFacts | None:
    root, path = args
    return parse_path(Path(root), path)


def parse_path(root: Path, path: str) -> FileFacts | None:
    adapter = adapter_for(path)
    if adapter is None:
        return None
    try:
        source = (root / path).read_bytes()
    except OSError:
        return None
    return extract(adapter, source)


def extract(adapter, source: bytes) -> FileFacts | None:
    try:
        return adapter.extract(source)
    except Exception:
        # A file that will not parse is worth skipping, never worth crashing a review over.
        return None
