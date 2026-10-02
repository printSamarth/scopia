"""Shared tree-sitter plumbing.

Parsers are cached per language: constructing one per file is the dominant cost when
indexing a large repo.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import cache
from typing import Any

import tree_sitter


@cache
def parser_for(language_id: str) -> tree_sitter.Parser:
    if language_id == "python":
        import tree_sitter_python

        raw = tree_sitter_python.language()
    elif language_id == "php":
        import tree_sitter_php

        raw = tree_sitter_php.language_php()
    elif language_id == "java":
        import tree_sitter_java

        raw = tree_sitter_java.language()
    elif language_id == "javascript":
        import tree_sitter_javascript

        raw = tree_sitter_javascript.language()
    elif language_id == "typescript":
        import tree_sitter_typescript

        raw = tree_sitter_typescript.language_typescript()
    elif language_id == "tsx":
        import tree_sitter_typescript

        raw = tree_sitter_typescript.language_tsx()
    else:  # pragma: no cover - guarded by the registry
        raise ValueError(f"no grammar for {language_id}")
    return tree_sitter.Parser(tree_sitter.Language(raw))


def walk(node: Any) -> Iterator[Any]:
    """Depth-first over named nodes, parents before children."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def text(node: Any | None) -> str:
    return "" if node is None else node.text.decode("utf-8", "replace")


def line_of(node: Any) -> int:
    return node.start_point[0] + 1


def end_line_of(node: Any) -> int:
    return node.end_point[0] + 1


def last_segment(dotted: str, sep: str = ".") -> str:
    """`mod.deep.fn` -> `fn`. Receivers are unresolvable by name alone, so we key on the
    final segment and let the index fan out across every candidate with that name."""
    return dotted.rsplit(sep, 1)[-1].strip()
