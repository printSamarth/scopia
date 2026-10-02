"""Blob-addressed fact cache.

Keyed by git blob SHA, so a file whose content did not change is never re-parsed —
this is what keeps repeat runs on a large repo cheap. The cache lives inside `.git/`
so it never shows up in `git status` and needs no cooperation from the user's
`.gitignore`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..lang.base import CallSite, FileFacts, SymbolDef, TypeRef

# Bump when the serialized shape or extraction logic changes; old entries are then
# simply never read again.
SCHEMA = "v5"


class FactsStore:
    def __init__(self, git_dir: Path) -> None:
        self.root = git_dir / "scopia" / SCHEMA

    def get(self, blob_sha: str) -> FileFacts | None:
        path = self._path(blob_sha)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return _decode(raw)

    def put(self, blob_sha: str, facts: FileFacts) -> None:
        path = self._path(blob_sha)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(_encode(facts), separators=(",", ":")), "utf-8")
            tmp.replace(path)
        except OSError:
            # A cache that cannot be written must never break a review.
            pass

    def _path(self, blob_sha: str) -> Path:
        return self.root / blob_sha[:2] / f"{blob_sha}.json"


def _encode(facts: FileFacts) -> dict:
    return {
        "d": [
            [d.name, d.qualname, d.kind, d.start_line, d.end_line, d.parent, d.has_body]
            for d in facts.defs
        ],
        "c": [[c.callee, c.line, c.caller, c.form, c.scope, c.module] for c in facts.calls],
        "b": [[b.holder, b.base] for b in facts.bases],
    }


def _decode(raw: dict) -> FileFacts:
    return FileFacts(
        defs=tuple(
            SymbolDef(name=n, qualname=q, kind=k, start_line=s, end_line=e, parent=p, has_body=b)
            for n, q, k, s, e, p, b in raw.get("d", ())
        ),
        calls=tuple(
            CallSite(callee=c, line=n, caller=w, form=f, scope=sc, module=m)
            for c, n, w, f, sc, m in raw.get("c", ())
        ),
        bases=tuple(TypeRef(holder=h, base=b) for h, b in raw.get("b", ())),
    )
