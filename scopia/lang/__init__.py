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
]


@cache
def _registry() -> dict[str, LanguageAdapter]:
    # Imported lazily: a run that touches no PHP should never load the PHP grammar.
    from .php import PhpAdapter
    from .python import PythonAdapter

    mapping: dict[str, LanguageAdapter] = {}
    for adapter in (PythonAdapter(), PhpAdapter()):
        for ext in adapter.extensions:
            mapping[ext] = adapter
    return mapping


def adapter_for(path: str) -> LanguageAdapter | None:
    return _registry().get(os.path.splitext(path)[1].lower())
