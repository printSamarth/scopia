"""Python adapter."""

from __future__ import annotations

from typing import Any

from ._ts import end_line_of, last_segment, line_of, parser_for, text, walk
from .base import (
    CLASS,
    FUNCTION,
    LITERAL,
    METHOD,
    PLAIN,
    RECEIVER,
    SELF,
    CallSite,
    FileFacts,
    SymbolDef,
    TypeRef,
)

_FUNC = "function_definition"
_CLASS = "class_definition"


class PythonAdapter:
    name = "python"
    grammar = "python"
    extensions = (".py", ".pyi")

    def extract(self, source: bytes) -> FileFacts:
        tree = parser_for("python").parse(source)
        # `from pipeline import run as pipeline_v3` then `pipeline_v3()` — the local
        # name is not the definition's name, so a call under the alias resolves to
        # nothing and the chain silently breaks at the import.
        aliases = _import_aliases(tree.root_node)
        imported_from = _import_modules(tree.root_node)
        # `controller = CheckoutController()` then `controller.apply_promo_code()`.
        # The assignment states the type, and without reading it a plain call chain
        # splits into one disconnected root per hop.
        var_types = _variable_types(tree.root_node)
        defs: list[SymbolDef] = []
        calls: list[CallSite] = []
        bases: list[TypeRef] = []

        # qualname is built from the lexical chain of enclosing classes, so it stays
        # path-independent and the blob cache can be reused across files.
        scopes: list[tuple[Any, str, str]] = []  # (node, qualname, kind)

        for node in walk(tree.root_node):
            while scopes and node.start_byte >= scopes[-1][0].end_byte:
                scopes.pop()

            if node.type in (_FUNC, _CLASS):
                name = text(node.child_by_field_name("name"))
                if not name:
                    continue
                parent = scopes[-1][1] if scopes else None
                parent_is_class = bool(scopes and scopes[-1][2] == CLASS)
                qualname = f"{parent}.{name}" if parent else name
                if node.type == _CLASS:
                    kind = CLASS
                else:
                    kind = METHOD if parent_is_class else FUNCTION
                defs.append(
                    SymbolDef(
                        name=name,
                        qualname=qualname,
                        kind=kind,
                        start_line=_start_line(node),
                        end_line=end_line_of(node),
                        parent=parent,
                        has_body=_has_body(node),
                    )
                )
                if node.type == _CLASS:
                    for base in _base_names(node):
                        bases.append(TypeRef(holder=qualname, base=base))
                scopes.append((node, qualname, kind))

            elif node.type == "call":
                caller = _innermost_callable(scopes)
                callee, form, scope = _callee(node, var_types)
                if callee:
                    calls.append(
                        CallSite(
                            callee=aliases.get(callee, callee),
                            line=line_of(node),
                            caller=caller,
                            form=form,
                            module=imported_from.get(callee, ""),
                            scope=scope,
                        )
                    )
                # `sorted(items, key=my_helper)` and `map('name', ...)` hide a reference
                # that no syntactic call expresses.
                for name in _callable_arguments(node):
                    calls.append(
                        CallSite(
                            callee=aliases.get(name, name),
                            line=line_of(node),
                            caller=caller,
                            form=LITERAL,
                            module=imported_from.get(name, ""),
                        )
                    )

        return FileFacts(defs=tuple(defs), calls=tuple(calls), bases=tuple(bases))


def _import_aliases(root: Any) -> dict[str, str]:
    """Local alias -> the name it actually refers to.

    Covers `from m import real as alias` and `import pkg.mod as alias`; for the latter
    the alias stands for the module, and calls through it already resolve on their last
    segment, so only the leaf matters.
    """
    aliases: dict[str, str] = {}
    for node in walk(root):
        if node.type != "aliased_import":
            continue
        parts = node.named_children
        if len(parts) != 2:
            continue
        original, alias = text(parts[0]), text(parts[1])
        leaf = last_segment(original)
        if alias and leaf and alias != leaf:
            aliases[alias] = leaf
    return aliases


def _import_modules(root: Any) -> dict[str, str]:
    """Local name -> the module it was imported from.

    `from app.users import render` says exactly which `render` is meant. Without reading
    it, a call to an unqualified name fans out across every definition sharing that name,
    which is the single most common source of false edges in Python.

    Relative imports (`from . import x`) are skipped: resolving them needs the importing
    file's own package position, and a wrong module here would narrow resolution to the
    wrong file — worse than leaving it ambiguous.
    """
    modules: dict[str, str] = {}
    for node in walk(root):
        if node.type != "import_from_statement":
            continue
        source = node.child_by_field_name("module_name")
        if source is None:
            continue
        dotted = text(source)
        if not dotted or dotted.startswith("."):
            continue
        for child in node.named_children:
            if child is source:
                continue
            if child.type == "dotted_name":
                modules[last_segment(text(child))] = dotted
            elif child.type == "aliased_import":
                parts = child.named_children
                if len(parts) == 2:
                    modules[text(parts[1])] = dotted
    return modules


def _start_line(node: Any) -> int:
    """Include decorators in the definition they decorate.

    A definition's span starts at `def`, which leaves `@abstractmethod` on the line
    above sitting inside the enclosing class instead — so adding one decorated method
    reported the whole class as changed.
    """
    parent = node.parent
    if parent is not None and parent.type == "decorated_definition":
        return line_of(parent)
    return line_of(node)


def _has_body(node: Any) -> bool:
    """A `.pyi` stub or a Protocol method whose body is only `...` or `pass`."""
    if node.type != _FUNC:
        return True
    body = node.child_by_field_name("body")
    if body is None:
        return False
    statements = [c for c in body.named_children if c.type != "comment"]
    if not statements:
        return False
    return not all(
        s.type == "pass_statement"
        or (s.type == "expression_statement" and text(s).strip() in ("...", '"""..."""'))
        for s in statements
    )


def _base_names(class_node: Any) -> list[str]:
    arglist = class_node.child_by_field_name("superclasses")
    if arglist is None:
        return []
    names = []
    for child in arglist.named_children:
        if child.type in ("identifier", "attribute"):
            names.append(last_segment(text(child)))
    return names


def _callee(call_node: Any, var_types: dict[str, str]) -> tuple[str, str, str]:
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return "", PLAIN, ""
    if fn.type == "identifier":
        return text(fn), PLAIN, ""
    if fn.type == "attribute":
        receiver = fn.child_by_field_name("object")
        name = last_segment(text(fn))
        holder = text(receiver)
        # `self.other()` resolves within this class; `obj.other()` could be anything,
        # unless an assignment nearby said what `obj` is.
        if holder in ("self", "cls"):
            return name, SELF, ""
        # `PricingEngine().recalculate(...)` — constructed and called in one breath,
        # so the class is stated even though no variable ever holds it.
        if receiver is not None and receiver.type == "call":
            built = last_segment(text(receiver.child_by_field_name("function") or receiver))
            if built[:1].isupper():
                return name, RECEIVER, built
        return name, RECEIVER, var_types.get(holder, "")
    return last_segment(text(fn)), RECEIVER, ""


def _variable_types(root: Any) -> dict[str, str]:
    """Variables assigned a constructor call: `x = SomeClass(...)`.

    Recognised by the callee being Capitalised, which is Python's convention for a class
    and all a syntactic pass can honestly go on. A wrong guess would narrow resolution to
    the wrong class, so the name has to look unambiguously like one.
    """
    types: dict[str, str] = {}
    for node in walk(root):
        if node.type != "assignment":
            continue
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None or right.type != "call":
            continue
        fn = right.child_by_field_name("function")
        if fn is None:
            continue
        cls = last_segment(text(fn))
        if not cls[:1].isupper():
            continue
        target = text(left)
        if left.type == "identifier" or (
            left.type == "attribute" and target.startswith("self.")
        ):
            types[target] = cls
    return types


def _callable_arguments(call_node: Any) -> list[str]:
    """Bare names and string literals passed as arguments — potential callables."""
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return []
    found = []
    for arg in args.named_children:
        if arg.type == "string":
            inner = text(arg).strip("\"'")
            if inner.isidentifier():
                found.append(inner)
        elif arg.type == "keyword_argument":
            value = arg.child_by_field_name("value")
            if value is not None and value.type == "identifier":
                found.append(text(value))
    return found


def _innermost_callable(scopes: list[tuple[Any, str, str]]) -> str | None:
    for _node, qualname, kind in reversed(scopes):
        if kind in (FUNCTION, METHOD):
            return qualname
    return None
