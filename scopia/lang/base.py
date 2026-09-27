"""The language seam.

Everything above this module is language-neutral. Adding a language means adding one
adapter that produces `FileFacts`; nothing in the index, graph or render layers changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

FUNCTION = "function"
METHOD = "method"
CLASS = "class"


# How a call names its target — this is what confidence can honestly be based on.
PLAIN = "plain"  # foo(...) — an unqualified function call
SELF = "self"  # $this->foo() / self.foo() — receiver is the enclosing class
RECEIVER = "receiver"  # $x->foo() / a.b.foo() — receiver type is unknown to us
LITERAL = "literal"  # 'foo' passed as a string callable


@dataclass(frozen=True)
class SymbolDef:
    """A named definition. `qualname` is path-independent so facts can be cached by blob."""

    name: str
    qualname: str
    kind: str
    start_line: int
    end_line: int
    parent: str | None = None
    has_body: bool = True
    """False for an interface method or a stub declaration — something with no
    implementation is a target for `implements`, never an interesting call target."""

    def contains(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line

    @property
    def span(self) -> int:
        return self.end_line - self.start_line


@dataclass(frozen=True)
class CallSite:
    """A call, recorded by the *name* being called — resolution happens later, in the index.

    `form` records how the target was named. It is the only honest basis for confidence:
    an unqualified call with one definition in the repo really is that definition, while
    a call through an unknown receiver may well land on a class we cannot see at all.
    """

    callee: str
    line: int
    caller: str | None
    form: str = PLAIN
    scope: str = ""
    """Class the call site names for its receiver, when it names one.

    `app(OrderService::class)->show()`, `new Foo()->bar()`, `Foo::bar()` and a typed
    property all state the type outright. Ignoring that turns a precise call into a
    fan-out across every same-named method in the repo.
    """


@dataclass(frozen=True)
class TypeRef:
    """A class naming a base class or implemented interface."""

    holder: str
    base: str


@dataclass(frozen=True)
class FileFacts:
    defs: tuple[SymbolDef, ...] = ()
    calls: tuple[CallSite, ...] = ()
    bases: tuple[TypeRef, ...] = ()


@runtime_checkable
class LanguageAdapter(Protocol):
    name: str
    extensions: tuple[str, ...]

    def extract(self, source: bytes) -> FileFacts:
        """Parse once, return every fact the rest of the pipeline needs."""


def enclosing_symbol(defs: tuple[SymbolDef, ...], line: int) -> SymbolDef | None:
    """Smallest definition containing `line`.

    This is the whole answer to "a 1-line change must still appear in the call chain":
    the unit of analysis is the enclosing symbol, and how much of it changed never enters
    into it. Smallest-wins so a method is attributed to itself rather than to its class.
    """
    best: SymbolDef | None = None
    for d in defs:
        if d.contains(line) and (best is None or d.span < best.span):
            best = d
    return best
