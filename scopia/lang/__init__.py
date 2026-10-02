"""Adapter registry — the one place that knows which languages exist."""

from __future__ import annotations

import os
from functools import cache

from .base import (
    CLASS,
    FUNCTION,
    METHOD,
    CallSite,
    FileFacts,
    LanguageAdapter,
    SymbolDef,
    TypeRef,
    enclosing_symbol,
)

__all__ = [
    "CLASS",
    "FUNCTION",
    "METHOD",
    "CallSite",
    "FileFacts",
    "LanguageAdapter",
    "SymbolDef",
    "TypeRef",
    "adapter_for",
    "enclosing_symbol",
    "parses_cleanly",
]


@cache
def _registry() -> dict[str, LanguageAdapter]:
    # Imported lazily: a run that touches no PHP never loads the PHP grammar, and a
    # Python-only repo never pays for the three JavaScript ones.
    from .java import JavaAdapter
    from .php import PhpAdapter
    from .python import PythonAdapter
    from .react import adapters as react_adapters

    mapping: dict[str, LanguageAdapter] = {}
    for adapter in (PythonAdapter(), PhpAdapter(), JavaAdapter(), *react_adapters()):
        for ext in adapter.extensions:
            mapping[ext] = adapter
    return mapping


def adapter_for(path: str) -> LanguageAdapter | None:
    return _registry().get(os.path.splitext(path)[1].lower())


def parses_cleanly(path: str, source: bytes) -> bool:
    """False when the file is in a supported language but currently has syntax errors.

    tree-sitter never raises on bad input — it recovers and returns a tree with ERROR
    nodes, so `extract` succeeding says nothing about whether the file is finished. Watch
    mode needs that distinction: a half-written function is "not ready", not "no symbols".
    Files in no supported language count as clean; there is nothing to be wrong about.
    """
    adapter = adapter_for(path)
    grammar = getattr(adapter, "grammar", None)
    if grammar is None:
        return True
    from ._ts import parser_for

    return not parser_for(grammar).parse(source).root_node.has_error
