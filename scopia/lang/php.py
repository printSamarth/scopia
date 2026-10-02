"""PHP adapter.

Parsed through tree-sitter, so reviewing PHP needs no PHP runtime installed.
"""

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

_TYPE_DECLS = (
    "class_declaration",
    "interface_declaration",
    "trait_declaration",
    "enum_declaration",
)
_FUNC_DECLS = ("function_definition", "method_declaration")


class PhpAdapter:
    name = "php"
    grammar = "php"
    extensions = (".php", ".phtml")

    def extract(self, source: bytes) -> FileFacts:
        tree = parser_for("php").parse(source)
        defs: list[SymbolDef] = []
        calls: list[CallSite] = []
        bases: list[TypeRef] = []
        scopes: list[tuple[Any, str, str]] = []
        # property name -> declared class, so `$this->orders->show()` can be resolved.
        prop_types = _property_types(tree.root_node)

        for node in walk(tree.root_node):
            while scopes and node.start_byte >= scopes[-1][0].end_byte:
                scopes.pop()

            if node.type in _TYPE_DECLS or node.type in _FUNC_DECLS:
                name = text(node.child_by_field_name("name"))
                if not name:
                    continue
                parent = scopes[-1][1] if scopes else None
                parent_is_class = bool(scopes and scopes[-1][2] == CLASS)
                qualname = f"{parent}.{name}" if parent else name
                if node.type in _TYPE_DECLS:
                    kind = CLASS
                else:
                    kind = METHOD if parent_is_class else FUNCTION
                defs.append(
                    SymbolDef(
                        name=name,
                        qualname=qualname,
                        kind=kind,
                        start_line=line_of(node),
                        end_line=end_line_of(node),
                        parent=parent,
                        has_body=_has_body(node),
                    )
                )
                if node.type in _TYPE_DECLS:
                    for base in _base_names(node):
                        bases.append(TypeRef(holder=qualname, base=base))
                scopes.append((node, qualname, kind))

            else:
                caller = _innermost_callable(scopes)
                callee, form, scope = _callee(node, prop_types)
                if callee:
                    calls.append(
                        CallSite(
                            callee=callee,
                            line=line_of(node),
                            caller=caller,
                            form=form,
                            scope=scope,
                        )
                    )
                for name in _callable_arguments(node):
                    calls.append(
                        CallSite(callee=name, line=line_of(node), caller=caller, form=LITERAL)
                    )

        return FileFacts(defs=tuple(defs), calls=tuple(calls), bases=tuple(bases))


def _has_body(node: Any) -> bool:
    """Does this declaration actually implement anything?

    Two forms mean "no": an interface method ends at `;` with no block at all, and a
    stub declares `function implode(...): string {}` with an *empty* block. php-src
    declares every PHP builtin the second way, so treating an empty block as a real
    body buries every diff under `implode`, `trim` and friends.
    """
    if node.type not in _FUNC_DECLS:
        return True
    for child in node.named_children:
        if child.type == "compound_statement":
            return bool(child.named_children)
    return False


def _base_names(node: Any) -> list[str]:
    """`extends A` and `implements B, C` — both are edges to a shared type."""
    names: list[str] = []
    for child in node.named_children:
        if child.type in ("base_clause", "class_interface_clause"):
            for part in child.named_children:
                if part.type in ("name", "qualified_name"):
                    names.append(last_segment(text(part), "\\"))
    return names


def _callee(node: Any, prop_types: dict[str, str]) -> tuple[str, str, str]:
    kind = node.type

    if kind == "function_call_expression":
        return last_segment(text(node.child_by_field_name("function")), "\\"), PLAIN, ""

    if kind == "scoped_call_expression":
        # `OrderService::show()` states the class outright.
        name = last_segment(text(node.child_by_field_name("name")), "\\")
        scope = last_segment(text(node.child_by_field_name("scope")), "\\")
        if scope in ("self", "static", "parent"):
            return name, SELF, ""
        return name, RECEIVER, scope

    if kind in ("member_call_expression", "nullsafe_member_call_expression"):
        name = last_segment(text(node.child_by_field_name("name")), "\\")
        obj = node.child_by_field_name("object")
        receiver = text(obj)
        if receiver in ("$this", "self", "static"):
            return name, SELF, ""
        return name, RECEIVER, _receiver_class(obj, prop_types)

    if kind == "object_creation_expression":
        # `new StripeProvider()` couples the two symbols as surely as a call does.
        for child in node.named_children:
            if child.type in ("name", "qualified_name"):
                return last_segment(text(child), "\\"), PLAIN, ""
    return "", PLAIN, ""


def _receiver_class(obj: Any, prop_types: dict[str, str]) -> str:
    """The class of a receiver expression, when the code says what it is.

    Covers the three ways real PHP names a type at the call site — `Foo::class` passed
    to a container, `new Foo()`, and a typed property — which between them account for
    most service calls in a framework app.
    """
    if obj is None:
        return ""

    # `$this->orders->show()` — the property's declared type is authoritative.
    if obj.type == "member_access_expression":
        holder = obj.child_by_field_name("object")
        prop = text(obj.child_by_field_name("name"))
        if text(holder) == "$this" and prop in prop_types:
            return prop_types[prop]
        return ""

    for node in walk(obj):
        # `app(OrderService::class)` / `resolve(Foo::class)`
        if node.type == "class_constant_access_expression":
            parts = [c for c in node.named_children if c.type in ("name", "qualified_name")]
            if parts:
                return last_segment(text(parts[0]), "\\")
        # `(new ProductService())->show()`
        if node.type == "object_creation_expression":
            for child in node.named_children:
                if child.type in ("name", "qualified_name"):
                    return last_segment(text(child), "\\")
    return ""


def _property_types(root: Any) -> dict[str, str]:
    """Declared types of `$this->…` properties, including promoted constructor params."""
    types: dict[str, str] = {}
    for node in walk(root):
        if node.type == "property_promotion_parameter":
            name = text(node.child_by_field_name("name")).lstrip("$")
            declared = _named_type(node)
            if name and declared:
                types[name] = declared
        elif node.type == "property_declaration":
            declared = _named_type(node)
            if not declared:
                continue
            for child in walk(node):
                if child.type == "variable_name":
                    types[text(child).lstrip("$")] = declared
    return types


def _named_type(node: Any) -> str:
    for child in node.named_children:
        if child.type in ("named_type", "type_list", "union_type", "optional_type"):
            for part in walk(child):
                if part.type in ("name", "qualified_name"):
                    return last_segment(text(part), "\\")
        if child.type in ("name", "qualified_name"):
            return last_segment(text(child), "\\")
    return ""


def _callable_arguments(node: Any) -> list[str]:
    """String literals passed as arguments — `array_map('helper', …)` and friends.

    A reference through a string is invisible to a syntactic call search, and missing it
    makes a used function report zero callers, which reads as dead code.
    """
    if not node.type.endswith("call_expression"):
        return []
    args = node.child_by_field_name("arguments")
    if args is None:
        return []
    found = []
    for arg in args.named_children:
        target = arg.named_children[0] if arg.type == "argument" and arg.named_children else arg
        if target.type in ("string", "encapsed_string"):
            inner = text(target).strip("\"'")
            if inner.isidentifier():
                found.append(inner)
    return found


def _innermost_callable(scopes: list[tuple[Any, str, str]]) -> str | None:
    for _node, qualname, kind in reversed(scopes):
        if kind in (FUNCTION, METHOD):
            return qualname
    return None
