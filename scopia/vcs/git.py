"""Thin wrapper over the git CLI. git is the only external binary scopia needs."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(root: Path | None, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root) if root else None,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip()
        raise GitError(detail or f"git {' '.join(args)} failed") from exc
    return proc.stdout.decode("utf-8", "replace")


@dataclass(frozen=True)
class Revisions:
    """Which two states are being compared, and where the 'new' content lives.

    `head` is None when the new side is the working tree — that is the zero-argument
    case, and it must read from disk rather than from a commit.
    """

    base: str
    head: str | None

    @property
    def diff_args(self) -> list[str]:
        return [f"{self.base}..{self.head}"] if self.head else [self.base]


def parse_revisions(spec: str | None) -> Revisions:
    if not spec:
        return Revisions(base="HEAD", head=None)
    if "..." in spec:
        base, _, head = spec.partition("...")
        return Revisions(base=base or "HEAD", head=head or "HEAD")
    if ".." in spec:
        base, _, head = spec.partition("..")
        return Revisions(base=base or "HEAD", head=head or "HEAD")
    return Revisions(base=spec, head=None)


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def discover(cls, start: Path | None = None) -> Repo:
        cwd = start or Path.cwd()
        out = _run(cwd, "rev-parse", "--show-toplevel")
        return cls(Path(out.strip()))

    def diff(self, revisions: Revisions) -> str:
        # -U0 is load-bearing: with context lines, a change would drag its innocent
        # neighbours into the touched set and the graph would overstate the blast radius.
        out = _run(
            self.root,
            "diff",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--find-renames",
            *revisions.diff_args,
        )
        return out

    def commit_count(self) -> str:
        return _run(self.root, "rev-list", "--count", "HEAD").strip()

    def untracked_files(self) -> list[str]:
        """New files git has not been told about yet.

        `git diff` cannot see these, so without asking for them separately a brand-new
        file — often the bulk of a change — is silently absent from the review.
        `--exclude-standard` keeps .gitignore honoured.
        """
        out = _run(self.root, "ls-files", "--others", "--exclude-standard", "-z")
        return [p for p in out.split("\0") if p]

    def dirty_paths(self) -> list[str]:
        """Every path with uncommitted state, untracked files listed individually.

        One cheap call (~10ms), which is what makes polling affordable. `-uall` matters:
        without it a new directory is reported as one entry and the files an agent just
        wrote inside it are invisible.
        """
        out = _run(self.root, "status", "--porcelain", "-z", "-uall")
        entries = out.split("\0")
        paths: list[str] = []
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            paths.append(entry[3:])
            # Renames and copies carry the old path as an extra NUL-separated field.
            if entry[0] in "RC" or entry[1] in "RC":
                i += 1
        return paths

    def working_tree_shas(self) -> dict[str, str]:
        """Blob SHA for every tracked file, in two git calls rather than one per file.

        Hashing files individually costs a subprocess each, which on a large repo
        dominates everything else and makes a warm, fully-cached run as slow as a cold
        one. `ls-files -s` gives the whole index at once; only files that differ in the
        working tree need re-hashing, and `hash-object --stdin-paths` does those in a
        single batch.
        """
        out = _run(self.root, "ls-files", "-s", "-z")
        shas: dict[str, str] = {}
        for entry in out.split("\0"):
            if not entry:
                continue
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if len(parts) >= 2 and path:
                shas[path] = parts[1]

        dirty = _run(self.root, "diff", "--name-only", "-z")
        modified = [p for p in dirty.split("\0") if p and p in shas]
        # Untracked files have no index entry, so they need hashing to be cached and
        # indexed at all — otherwise their symbols exist nowhere and nothing can link
        # to them.
        pending = modified + [p for p in self.untracked_files() if p not in shas]
        if pending:
            shas.update(self._hash_batch(pending))
        # A file deleted from the working tree still has an index entry, so it would
        # otherwise report its old hash and look untouched. Dropping it lets callers see
        # the deletion by its absence.
        return {path: sha for path, sha in shas.items() if (self.root / path).is_file()}

    def _hash_batch(self, paths: list[str]) -> dict[str, str]:
        """Content hashes for paths that exist on disk.

        Deleted files are dropped first: `git hash-object` fails on a missing path and
        takes the whole batch down with it, which silently reverted every *other*
        modified file to its stale index hash. Those hashes key the fact cache, so one
        deletion poisoned the analysis of every edited file in the tree.
        """
        present = [p for p in paths if (self.root / p).is_file()]
        if not present:
            return {}
        try:
            proc = subprocess.run(
                ["git", "hash-object", "--stdin-paths"],
                cwd=str(self.root),
                input="\n".join(present).encode(),
                capture_output=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return {}
        hashes = proc.stdout.decode().split()
        if len(hashes) != len(present):
            # A partial result would pair hashes with the wrong paths. Report nothing
            # rather than something quietly wrong.
            return {}
        return dict(zip(present, hashes, strict=True))

    def read_many(self, rev: str | None, paths: list[str]) -> dict[str, bytes]:
        """Read many blobs in one git call.

        Spawning `git show` per file is what made a warm run on a large diff as slow as a
        cold one — 400 subprocesses for a 200-file change. `cat-file --batch` streams all
        of them through a single process.
        """
        if not paths:
            return {}
        if rev is None:
            out: dict[str, bytes] = {}
            for path in paths:
                try:
                    out[path] = (self.root / path).read_bytes()
                except OSError:
                    continue
            return out

        specs = [f"{rev}:{path}" for path in paths]
        try:
            proc = subprocess.run(
                ["git", "cat-file", "--batch"],
                cwd=str(self.root),
                input=("\n".join(specs) + "\n").encode(),
                capture_output=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return {}

        results: dict[str, bytes] = {}
        buf, pos = proc.stdout, 0
        for path in paths:
            newline = buf.find(b"\n", pos)
            if newline == -1:
                break
            header = buf[pos:newline].decode("utf-8", "replace")
            pos = newline + 1
            parts = header.split()
            # "<spec> missing" — deleted, added, or never existed at this revision.
            if len(parts) < 3 or parts[1] == "missing":
                continue
            size = int(parts[2])
            results[path] = buf[pos : pos + size]
            pos += size + 1  # trailing newline after the payload
        return results

