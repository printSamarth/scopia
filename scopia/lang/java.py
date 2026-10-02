"""Java adapter.

Parsed through tree-sitter, so reviewing Java needs no JDK installed.

Java is the most precisely resolvable language scopia reads: every parameter, field and
local declares its type, so a receiver's class can usually be read straight off the
declaration instead of guessed from the method name.
"""

from __future__ import annotations

from typing import Any

from ._ts import end_line_of, line_of, parser_for, text, walk
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
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
)
_FUNC_DECLS = ("method_declaration", "constructor_declaration")

#: A constructor is named for its class, which would collide with the class definition
#: itself — `new Foo()` would then be "ambiguous" between `Foo` and `Foo.Foo`.
CONSTRUCTOR = "<init>"


class JavaAdapter:
    name = "java"
    grammar = "java"
    extensions = (".java",)

    def extract(self, source: bytes) -> FileFacts:
        tree = parser_for("java").parse(source)
        defs: list[SymbolDef] = []
        calls: list[CallSite] = []
        bases: list[TypeRef] = []
        scopes: list[tuple[Any, str, str]] = []  # (node, qualname, kind)
        fields: dict[str, dict[str, str]] = {}  # class qualname -> field name -> type
        locals_: dict[str, dict[str, str]] = {}  # callable qualname -> variable -> type
        supers: dict[str, str] = {}  # class qualname -> superclass name

        for node in walk(tree.root_node):
            while scopes and node.start_byte >= scopes[-1][0].end_byte:
                scopes.pop()

            if node.type in _TYPE_DECLS or node.type in _FUNC_DECLS:
                name = text(node.child_by_field_name("name"))
                if node.type == "constructor_declaration":
                    name = CONSTRUCTOR
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
                    extends, implements = _base_names(node)
                    for base in extends + implements:
                        bases.append(TypeRef(holder=qualname, base=base))
                    if node.type == "class_declaration" and extends:
                        supers[qualname] = extends[0]
                    fields[qualname] = _field_types(node)
                else:
                    locals_[qualname] = _variable_types(node)
                scopes.append((node, qualname, kind))
                continue

            caller = _innermost_callable(scopes)
            klass = _innermost_class(scopes)
            env = _Env(
                locals_.get(caller or "", {}),
                fields.get(klass or "", {}),
                supers.get(klass or "", ""),
            )
            for callee, form, scope in _calls(node, env):
                calls.append(
                    CallSite(
                        callee=callee, line=line_of(node), caller=caller, form=form, scope=scope
                    )
                )

        return FileFacts(defs=tuple(defs), calls=tuple(calls), bases=tuple(bases))


class _Env:
    """What the declarations in scope say about names."""

    def __init__(self, local: dict[str, str], field: dict[str, str], parent: str) -> None:
        self.local = local
        self.field = field
        self.parent = parent

    def type_of(self, name: str) -> str:
        return self.local.get(name) or self.field.get(name, "")


def _has_body(node: Any) -> bool:
    """An abstract or interface method ends at `;` with no block — a target for
    `implements`, never an interesting call target."""
    if node.type not in _FUNC_DECLS:
        return True
    return node.child_by_field_name("body") is not None


def _type_name(node: Any | None) -> str:
    """The class a type expression names. `List<Foo>` is `List`; `a.b.Foo` is `Foo`."""
    if node is None:
        return ""
    if node.type == "type_identifier":
        return text(node)
    if node.type == "generic_type":
        return _type_name(node.named_children[0]) if node.named_children else ""
    if node.type == "scoped_type_identifier":
        parts = [c for c in node.named_children if c.type == "type_identifier"]
        return text(parts[-1]) if parts else ""
    return ""  # primitives, arrays and `var` say nothing useful


def _base_names(node: Any) -> tuple[list[str], list[str]]:
    """(extends, implements). Both are edges to a shared type."""
    extends: list[str] = []
    implements: list[str] = []
    for child in node.named_children:
        if child.type == "superclass":
            extends += [n for n in map(_type_name, child.named_children) if n]
        elif child.type in ("super_interfaces", "extends_interfaces"):
            bucket = extends if node.type == "interface_declaration" else implements
            for part in walk(child):
                if part.type in ("type_identifier", "scoped_type_identifier"):
                    name = _type_name(part)
                    if name:
                        bucket.append(name)
    # `walk` also descends into a scoped type's inner identifiers; keep one of each.
    return list(dict.fromkeys(extends)), list(dict.fromkeys(implements))


def _field_types(klass: Any) -> dict[str, str]:
    types: dict[str, str] = {}
    body = klass.child_by_field_name("body")
    if body is None:
        return types
    for member in body.named_children:
        if member.type != "field_declaration":
            continue
        declared = _type_name(member.child_by_field_name("type"))
        if not declared:
            continue
        for declarator in member.named_children:
            if declarator.type == "variable_declarator":
                types[text(declarator.child_by_field_name("name"))] = declared
    # A record's components are its fields.
    params = klass.child_by_field_name("parameters")
    if params is not None:
        for param in params.named_children:
            declared = _type_name(param.child_by_field_name("type"))
            if declared:
                types[text(param.child_by_field_name("name"))] = declared
    return types


def _variable_types(callable_node: Any) -> dict[str, str]:
    """Parameters and locals of one method. Shadowing inside one body is rare enough that
    last-declaration-wins is the honest simplification."""
    types: dict[str, str] = {}
    for node in walk(callable_node):
        if node.type in ("formal_parameter", "spread_parameter", "catch_formal_parameter"):
            declared = _type_name(node.child_by_field_name("type"))
            name = text(node.child_by_field_name("name"))
            if declared and name:
                types[name] = declared
        elif node.type in ("local_variable_declaration", "enhanced_for_statement"):
            declared = _type_name(node.child_by_field_name("type"))
            if not declared:
                continue
            if node.type == "enhanced_for_statement":
                types[text(node.child_by_field_name("name"))] = declared
                continue
            for declarator in node.named_children:
                if declarator.type == "variable_declarator":
                    types[text(declarator.child_by_field_name("name"))] = declared
    return types


def _calls(node: Any, env: _Env) -> list[tuple[str, str, str]]:
    kind = node.type

    if kind == "method_invocation":
        name = text(node.child_by_field_name("name"))
        if not name:
            return []
        obj = node.child_by_field_name("object")
        if obj is None:
            return [(name, SELF, "")]  # `helper()` — an implicit `this`
        if obj.type == "this":
            return [(name, SELF, "")]
        if obj.type == "super":
            return [(name, RECEIVER, env.parent)] if env.parent else [(name, SELF, "")]
        return [(name, RECEIVER, _receiver_class(obj, env))]

    if kind == "object_creation_expression":
        # `new Foo()` couples the two symbols as surely as a call does — to the class, and
        # to its constructor when it declares one.
        created = _type_name(node.child_by_field_name("type"))
        if created:
            return [(created, PLAIN, ""), (CONSTRUCTOR, RECEIVER, created)]
        return []

    if kind == "method_reference":
        # `Foo::bar` / `this::bar` — a reference, not a call we can be sure runs.
        names = [c for c in node.named_children if c.type == "identifier"]
        if not names:
            return []
        owner = node.named_children[0]
        scope = text(owner) if owner.type in ("identifier", "type_identifier") else ""
        return [(text(names[-1]), LITERAL, scope if scope[:1].isupper() else "")]
    return []


def _receiver_class(obj: Any, env: _Env) -> str:
    """The class of a receiver expression, when the code says what it is."""
    if obj.type == "identifier":
        name = text(obj)
        declared = env.type_of(name)
        if declared:
            return declared
        # `Svc.stat()` — an unresolved capitalised name is a class, by universal convention.
        return name if name[:1].isupper() else ""

    # `this.svc.go()` — the field's declared type is authoritative.
    if obj.type == "field_access":
        holder = obj.child_by_field_name("object")
        member = text(obj.child_by_field_name("field"))
        if holder is not None and holder.type == "this" and member in env.field:
            return env.field[member]
        # `com.acme.Svc.stat()` — a qualified class name.
        return member if member[:1].isupper() else ""

    if obj.type == "object_creation_expression":
        return _type_name(obj.child_by_field_name("type"))

    if obj.type == "parenthesized_expression" and obj.named_children:
        inner = obj.named_children[0]
        if inner.type == "cast_expression":
            return _type_name(inner.child_by_field_name("type"))
        return _receiver_class(inner, env)
    return ""


def _innermost_callable(scopes: list[tuple[Any, str, str]]) -> str | None:
    for _node, qualname, kind in reversed(scopes):
        if kind in (FUNCTION, METHOD):
            return qualname
    return None


def _innermost_class(scopes: list[tuple[Any, str, str]]) -> str | None:
    for _node, qualname, kind in reversed(scopes):
        if kind == CLASS:
            return qualname
    return None
