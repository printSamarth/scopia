"""React adapter — JSX/TSX, and the JavaScript and TypeScript around it.

Parsed through tree-sitter, so reviewing a React app needs no Node runtime, no
`tsc`, and no `node_modules` on disk.

Three things distinguish a React codebase from plain JavaScript, and all three are
what a reviewer actually follows:

* **`<Button />` is a call.** React invokes the component; composition *is* the call
  graph. A diff that renames a prop on a shared `<Card />` reaches every screen that
  draws one, and no syntactic call expression says so.
* **Components and handlers are `const`, not `function`.** `const Panel = () => …`,
  `const handleSubmit = useCallback(…)`, `export default memo(forwardRef(…))`. An
  adapter that only records `function` declarations sees an empty file.
* **A hook is an ordinary call.** `useAuth()` needs no special case — which is the
  point: custom hooks join the chain by themselves.

One adapter, three grammars. `.ts` cannot be parsed as `.tsx` (`<T>expr` is a cast in
one and an element in the other), so the extension picks the grammar — but all of them
report the same `name`, because a React app imports across `.js`, `.ts` and `.tsx`
freely and a call between them is not a cross-language edge.
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
    "abstract_class_declaration",
    "interface_declaration",
)
_FUNC_DECLS = (
    "function_declaration",
    "generator_function_declaration",
    "method_definition",
    # Bodiless: an interface method, an `abstract` member, a `.d.ts` entry. Real
    # definitions for `implements`, never call targets.
    "method_signature",
    "abstract_method_signature",
    "function_signature",
)
_BODILESS = ("method_signature", "abstract_method_signature", "function_signature")
_FUNCTION_VALUES = ("arrow_function", "function_expression", "function")

_JSX_OPENERS = ("jsx_opening_element", "jsx_self_closing_element")

# Calls that wrap a component or a handler and hand back something still callable, so
# the `const` they are assigned to is a definition rather than a value. Plus `with*`,
# the HOC convention.
#
# Hooks are listed by name, never matched by the `use` prefix: `useCallback` returns
# its function, but `useSelector(s => s.total)` and `useMemo(() => …)` return a value,
# and treating `const total = useSelector(…)` as a definition steals every change on
# that line from the component it belongs to.
_WRAPPERS = frozenset(
    {
        "memo", "forwardRef", "observer", "connect", "lazy", "styled", "createSelector",
        "useCallback", "useEffectEvent", "useEvent", "useMemoizedFn",
    }
)

# Methods whose identifier argument is a callable by definition rather than by naming
# guess. `find` is deliberately absent: `repo.find(id)` is far more common in an
# application than `rows.find(predicate)`, and it would record `id` as a callable.
_HIGHER_ORDER = frozenset(
    {"map", "filter", "forEach", "reduce", "reduceRight", "findIndex", "findLast",
     "some", "every", "sort", "flatMap", "then", "catch", "finally"}
)

_MAX_WRAPPER_DEPTH = 4


class ReactAdapter:
    """One instance per grammar; they share a name so calls between them resolve."""

    name = "react"

    def __init__(self, grammar: str, extensions: tuple[str, ...]) -> None:
        self.grammar = grammar
        self.extensions = extensions

    def extract(self, source: bytes) -> FileFacts:
        tree = parser_for(self.grammar).parse(source)
        root = tree.root_node
        # `import { run as pipelineV3 }` then `pipelineV3()` — the call names something
        # no definition is called, so the chain breaks silently at the import.
        aliases = _import_aliases(root)
        # `const svc: OrderService = …`, `new OrderService()`, `(props: Props)`,
        # `private repo: UserRepo`. TypeScript states receiver types outright; ignoring
        # that turns one precise call into a fan-out across every same-named method.
        var_types = _variable_types(root)

        defs: list[SymbolDef] = []
        calls: list[CallSite] = []
        bases: list[TypeRef] = []
        scopes: list[tuple[Any, str, str]] = []  # (node, qualname, kind)

        for node in walk(root):
            while scopes and node.start_byte >= scopes[-1][0].end_byte:
                scopes.pop()

            declared = _declaration_name(node, top_level=not scopes)
            if declared is not None:
                name, kind, is_def = declared
                parent = scopes[-1][1] if scopes else None
                if parent is None and node.type in ("pair", "method_definition"):
                    # A member of an object literal that is not bound to a name —
                    # `register({ handler() {…} })`. There is nothing for a reviewer
                    # to call it, and hoisting it to `handler` would invent a symbol.
                    continue
                qualname = f"{parent}.{name}" if parent else name
                if kind is None:  # a `const` holding an object literal: scope, not symbol
                    scopes.append((node, qualname, CLASS))
                    continue
                if kind == FUNCTION and scopes and scopes[-1][2] == CLASS:
                    kind = METHOD
                if is_def:
                    defs.append(
                        SymbolDef(
                            name=name,
                            qualname=qualname,
                            kind=kind,
                            start_line=line_of(node),
                            end_line=end_line_of(node),
                            parent=parent,
                            has_body=node.type not in _BODILESS,
                        )
                    )
                if kind == CLASS:
                    for base in _base_names(node):
                        bases.append(TypeRef(holder=qualname, base=base))
                scopes.append((node, qualname, kind))
                continue

            caller = _innermost_callable(scopes)
            callee, form, scope = _callee(node, var_types)
            if callee:
                calls.append(
                    CallSite(
                        callee=aliases.get(callee, callee),
                        line=line_of(node),
                        caller=caller,
                        form=form,
                        scope=aliases.get(scope, scope) if scope else "",
                    )
                )
            for name in _referenced_callables(node):
                calls.append(
                    CallSite(
                        callee=aliases.get(name, name),
                        line=line_of(node),
                        caller=caller,
                        form=LITERAL,
                    )
                )

        return FileFacts(defs=tuple(defs), calls=tuple(calls), bases=tuple(bases))


def adapters() -> tuple[ReactAdapter, ...]:
    """Every extension a React project actually contains.

    `.ts` and `.tsx` need different grammars for the same reason TypeScript ships two
    parsers: `<T>value` is a type assertion in one and an element in the other.
    """
    return (
        ReactAdapter("javascript", (".js", ".jsx", ".mjs", ".cjs")),
        ReactAdapter("typescript", (".ts", ".mts", ".cts")),
        ReactAdapter("tsx", (".tsx",)),
    )


def _declaration_name(node: Any, *, top_level: bool) -> tuple[str, str | None, bool] | None:
    """`(name, kind, is_definition)` for a node that introduces a scope, else None.

    A `kind` of None means "a scope with no symbol": `const api = { fetchUser() {…} }`
    is not itself callable, but `fetchUser` is, and it belongs to `api`.
    """
    if node.type in _TYPE_DECLS:
        name = text(node.child_by_field_name("name"))
        return (name, CLASS, True) if name else None

    if node.type in _FUNC_DECLS:
        name = text(node.child_by_field_name("name"))
        return (name, FUNCTION, True) if name else None

    if node.type == "pair" and _is_function(node.child_by_field_name("value")):
        # `{ onSave: () => … }` inside an object literal.
        name = text(node.child_by_field_name("key"))
        return (name, FUNCTION, True) if name.isidentifier() else None

    if node.type == "variable_declarator":
        target = node.child_by_field_name("name")
        if target is None or target.type != "identifier":
            # `const [total, setTotal] = useState(0)` binds no single symbol.
            return None
        name = text(target)
        value = node.child_by_field_name("value")
        if _is_function(value) or _is_hook_binding(name, value):
            return (name, FUNCTION, True)
        if value is not None and value.type == "object":
            return (name, None, False)
        if top_level and _call_carries_logic(value):
            return (name, FUNCTION, True)
        return None

    return None


def _is_function(value: Any | None) -> bool:
    """Does this expression evaluate to something `<Thing />` or `thing()` can reach?

    Direct `() => …` and `function () {}`, plus the wrapped forms React is written in:
    `memo(…)`, `forwardRef(…)`, `useCallback(…)`, `styled.div\\`…\\``. The wrapper has to
    be recognisable — unwrapping every call would make `const ids = rows.map(r => r.id)`
    look like a function definition.
    """
    if value is None:
        return False
    if value.type in _FUNCTION_VALUES:
        return True
    if _is_styled(value):
        return True
    if value.type == "call_expression":
        return _wraps_function(value, 0)
    return False


def _call_carries_logic(value: Any | None) -> bool:
    """`export const loadTodos = createAsyncThunk('load', async () => api.get(…))`.

    Redux Toolkit, React Router loaders, reselect — a module-level `const` built by a
    factory that is handed functions is a named unit of logic, and its reducers and
    callbacks call real code. Leaving it unnamed drops every one of those calls, which
    made a one-line change to a helper look unreferenced.

    Module level only. Inside a component, `const count = useSelector(s => s.n)` has
    the same shape and holds a value; there the enclosing component already owns the
    calls, and a symbol for `count` would only take its lines away.
    """
    if value is None or value.type != "call_expression":
        return False
    args = value.child_by_field_name("arguments")
    if args is None:
        return False
    return any(
        n.type in _FUNCTION_VALUES or n.type == "method_definition" for n in walk(args)
    )


def _is_hook_binding(name: str, value: Any | None) -> bool:
    """`const useCart = create<CartState>(…)` — a hook, whatever built it.

    zustand, jotai, `useSelector.withTypes()` and a dozen others hand back a hook from a
    factory no wrapper list could keep up with. The binding's name is what React's
    rules-of-hooks lint enforces, so it is the one signal every such library shares.
    """
    return value is not None and value.type == "call_expression" and _is_hook_name(name)


def _is_hook_name(name: str) -> bool:
    """`useCart`, not `user` or `usage` — the rules-of-hooks naming."""
    return len(name) > 3 and name.startswith("use") and name[3].isupper()


def _is_styled(value: Any) -> bool:
    """`styled.div\\`…\\``, `styled(Button)\\`…\\``, `styled.a.attrs(…)\\`…\\`` — components.

    Keyed on the leftmost identifier rather than the tree's shape, because the shape is
    not stable: `styled.div<Props>\\`…\\`` is valid TypeScript that this grammar reads as
    `(styled.div < Props) > \\`…\\``, or as an error-recovered instantiation, depending on
    what the type argument looks like. The leftmost name survives every one of them.
    """
    node = value
    while node.named_children:
        node = node.named_children[0]
    return node.type == "identifier" and text(node) == "styled"


def _wraps_function(call: Any, depth: int) -> bool:
    if depth >= _MAX_WRAPPER_DEPTH:
        return False
    fn = call.child_by_field_name("function")
    wrapper = last_segment(text(fn)) if fn is not None else ""
    if not (wrapper in _WRAPPERS or (wrapper.startswith("with") and wrapper[4:5].isupper())):
        return False
    args = call.child_by_field_name("arguments")
    if args is None:
        return False
    for arg in args.named_children:
        if arg.type in _FUNCTION_VALUES:
            return True
        if arg.type == "call_expression" and _wraps_function(arg, depth + 1):
            return True
    return False


def _base_names(node: Any) -> list[str]:
    """`extends Base`, `implements Contract`, `interface A extends B` — all shared types.

    Read one level down and no further. `extends React.Component<Props>` names exactly
    one base; descending into it would also harvest `React` and `Props` and claim the
    class extends three things.
    """
    names: list[str] = []
    for child in node.named_children:
        if child.type == "class_heritage":
            for clause in child.named_children:  # extends_clause, implements_clause
                names.extend(_clause_names(clause))
        elif child.type == "extends_type_clause":
            names.extend(_clause_names(child))
    return names


def _clause_names(clause: Any) -> list[str]:
    out: list[str] = []
    for part in clause.named_children:
        if part.type in ("identifier", "type_identifier"):
            out.append(text(part))
        elif part.type in ("member_expression", "nested_type_identifier"):
            # `React.Component` — the name is the last segment.
            out.append(last_segment(text(part)))
        elif part.type == "generic_type":
            name = part.child_by_field_name("name")
            if name is not None:
                out.append(last_segment(text(name)))
    return out


def _callee(node: Any, var_types: dict[str, str]) -> tuple[str, str, str]:
    kind = node.type

    if kind in _JSX_OPENERS:
        return _jsx_callee(node)

    if kind == "new_expression":
        # `new OrderService()` couples the two symbols as surely as a call does.
        constructor = node.child_by_field_name("constructor")
        if constructor is not None and constructor.type in ("identifier", "member_expression"):
            return last_segment(text(constructor)), PLAIN, ""
        return "", PLAIN, ""

    if kind == "call_expression":
        fn = node.child_by_field_name("function")
        if fn is None:
            return "", PLAIN, ""
        if fn.type == "identifier":
            return text(fn), PLAIN, ""
        if fn.type == "member_expression":
            return _member_callee(fn, var_types)
        # Anything else is an expression producing the callee — `(a || b)()`, or a
        # generic this grammar mis-reads. Keep its name only if it has one.
        name = last_segment(text(fn))
        return (name, RECEIVER, "") if name.isidentifier() else ("", PLAIN, "")

    return "", PLAIN, ""


def _jsx_callee(node: Any) -> tuple[str, str, str]:
    """`<Button />` is a call; `<div />` is not.

    Capitalisation is not a convention here — it is how JSX itself decides, so reading
    it costs no confidence. `<Cart.Line />` names its holder outright, exactly like a
    static method call does.
    """
    name = node.child_by_field_name("name")
    if name is None:
        return "", PLAIN, ""  # a `<>…</>` fragment
    if name.type == "member_expression":
        return last_segment(text(name)), RECEIVER, text(name).split(".", 1)[0]
    if name.type != "identifier":
        return "", PLAIN, ""  # `<svg:rect />` and friends
    tag = text(name)
    return (tag, PLAIN, "") if tag[:1].isupper() else ("", PLAIN, "")


def _member_callee(fn: Any, var_types: dict[str, str]) -> tuple[str, str, str]:
    name = text(fn.child_by_field_name("property"))
    obj = fn.child_by_field_name("object")
    if obj is None:
        return name, RECEIVER, ""
    if obj.type in ("this", "super"):
        return name, SELF, ""
    if obj.type == "new_expression":
        # `new PricingEngine().recalculate(…)` — constructed and called in one breath.
        constructor = obj.child_by_field_name("constructor")
        return name, RECEIVER, last_segment(text(constructor)) if constructor else ""
    holder = text(obj)
    if holder in var_types:
        return name, RECEIVER, var_types[holder]
    # `Cart.Line`, `Api.fetchUser` — a capitalised receiver names a type or a namespace,
    # and honouring it is what keeps `React.memo` from linking to a local `memo`.
    if obj.type == "identifier" and holder[:1].isupper():
        return name, RECEIVER, holder
    return name, RECEIVER, ""


def _referenced_callables(node: Any) -> list[str]:
    """Callables passed rather than called — invisible to any search for `(`.

    Deliberately narrow. JavaScript passes data positionally all day (`setTotal(total)`,
    `dispatch(action)`), so treating every bare identifier argument as a callable would
    inflate fan-in counts on ordinary names. These five shapes are the ones where the
    position itself says "callable":

    * `onClick={handleSubmit}` — a JSX prop, which is how React wires every handler
    * `useQuery({ queryFn: fetchUser })` — the object-argument form of a keyword
    * `rows.map(renderRow)` — a higher-order method, callable by definition
    * `useSelector(selectTodos)` — a hook argument
    * `on('save', 'persist')` — a string callable, as in the other adapters
    """
    found: list[str] = []

    if node.type == "jsx_attribute":
        # `name={value}` — the attribute has no `value` field, just two children.
        for value in node.named_children:
            if value.type != "jsx_expression":
                continue
            for child in value.named_children:
                if child.type == "identifier":
                    found.append(text(child))
        return found

    if node.type != "call_expression":
        return found

    args = node.child_by_field_name("arguments")
    if args is None:
        return found

    fn = node.child_by_field_name("function")
    higher_order = fn is not None and (
        (fn.type == "member_expression"
         and text(fn.child_by_field_name("property")) in _HIGHER_ORDER)
        # `useSelector(selectTodos)`, `useQuery(key, fetchTodos)` — a hook takes
        # selectors, fetchers and reducers far more often than it takes plain data.
        or (fn.type == "identifier" and _is_hook_name(text(fn)))
    )
    for arg in args.named_children:
        if arg.type == "string":
            inner = _string_value(arg)
            if inner.isidentifier():
                found.append(inner)
        elif arg.type == "identifier" and higher_order:
            found.append(text(arg))
        elif arg.type == "object":
            for pair in arg.named_children:
                if pair.type != "pair":
                    continue
                value = pair.child_by_field_name("value")
                if value is not None and value.type == "identifier":
                    found.append(text(value))
    return found


def _string_value(node: Any) -> str:
    for child in node.named_children:
        if child.type == "string_fragment":
            return text(child)
    return ""


def _import_aliases(root: Any) -> dict[str, str]:
    """Local name -> the name the definition actually carries.

    Two shapes matter. `import { run as pipelineV3 }` renames outright. A default
    import names itself — `import Btn from './ui/Button'` — and only the module path
    says what it is; for a *relative* path the file stem is that answer, and a package
    import is not in the repo to resolve against anyway.
    """
    aliases: dict[str, str] = {}
    for node in walk(root):
        if node.type != "import_statement":
            continue
        source = _string_value(node.child_by_field_name("source") or node)
        stem = _module_stem(source) if source.startswith(".") else ""
        for child in walk(node):
            if child.type == "import_specifier":
                original = text(child.child_by_field_name("name"))
                alias = text(child.child_by_field_name("alias"))
                if alias and original and alias != original:
                    aliases[alias] = original
            elif child.type == "import_clause":
                for part in child.named_children:
                    # A bare identifier directly under the clause is the default import.
                    if part.type == "identifier" and stem and text(part) != stem:
                        aliases[text(part)] = stem
    return aliases


def _module_stem(source: str) -> str:
    leaf = source.rsplit("/", 1)[-1]
    stem = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
    return stem if stem.isidentifier() else ""


def _variable_types(root: Any) -> dict[str, str]:
    """Receiver name -> the class it holds, wherever the code states it.

    File-scoped and flat, like the other adapters: a name reused for two types in two
    functions resolves to whichever was seen last. Narrowing that would mean scope
    analysis, and the cost of being wrong is one edge pointing at a sibling class —
    which is why a declared type is only ever read, never inferred from usage.
    """
    types: dict[str, str] = {}

    def record(name: str, node: Any) -> None:
        declared = _declared_type(node)
        if name and declared:
            types[name] = declared

    for node in walk(root):
        if node.type == "variable_declarator":
            target = node.child_by_field_name("name")
            if target is None or target.type != "identifier":
                continue
            name = text(target)
            value = node.child_by_field_name("value")
            if value is not None and value.type == "object":
                # `const api = { fetchUser() {…} }` — the receiver is its own namespace,
                # so `api.fetchUser()` resolves to the member rather than fanning out
                # across every `fetchUser` in the repo.
                types[name] = name
            else:
                record(name, node)

        elif node.type in ("required_parameter", "optional_parameter"):
            target = node.child_by_field_name("pattern")
            if target is not None and target.type == "identifier":
                record(text(target), node)
                if any(c.type == "accessibility_modifier" for c in node.named_children):
                    # `constructor(private repo: OrderRepo)` declares a field as well as
                    # a parameter, and the body reaches it as `this.repo`.
                    record(f"this.{text(target)}", node)

        elif node.type in ("public_field_definition", "property_signature"):
            name = text(node.child_by_field_name("name"))
            if name:
                record(f"this.{name}", node)

        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left is not None and text(left).startswith("this."):
                record(text(left), node)

    return types


def _declared_type(node: Any) -> str:
    """A `: Type` annotation, or the class of a `new Type()` initialiser."""
    for child in node.named_children:
        if child.type == "type_annotation":
            for part in walk(child):
                if part.type in ("type_identifier", "identifier"):
                    return text(part)
                if part.type == "nested_type_identifier":
                    return last_segment(text(part))
        if child.type == "new_expression":
            constructor = child.child_by_field_name("constructor")
            if constructor is not None:
                return last_segment(text(constructor))
    value = node.child_by_field_name("value") if node.type != "new_expression" else None
    if value is not None and value.type == "new_expression":
        constructor = value.child_by_field_name("constructor")
        if constructor is not None:
            return last_segment(text(constructor))
    return ""


def _innermost_callable(scopes: list[tuple[Any, str, str]]) -> str | None:
    for _node, qualname, kind in reversed(scopes):
        if kind in (FUNCTION, METHOD):
            return qualname
    return None
