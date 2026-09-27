"""Diff -> touched symbols -> bounded expansion -> graph."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..index.repo import RepoIndex, build_index, extract, uid
from ..lang import adapter_for, enclosing_symbol
from ..lang.base import LITERAL, METHOD, PLAIN, RECEIVER, SELF, FileFacts, SymbolDef
from ..vcs.diff import ADDED, DELETED, FileChange, parse_diff
from ..vcs.git import Repo, Revisions
from .model import AMBIGUOUS, CALL, HEURISTIC, IMPLEMENTS, INFERRED, STATIC, Edge, Graph, Node

DEFAULT_HOPS = 1
DEFAULT_MAX_NODES = 60

# An *unchanged* symbol called from more than this many places is plumbing — a response
# helper, a logger, a base repository. Everything calls it, so its presence says nothing
# about this particular change and it crowds out what does.
#
# The same number on a *changed* symbol means the opposite, and is never hidden: a small
# edit to something called from everywhere is the most important thing a review can show.
DEFAULT_UTILITY_FAN_IN = 12

# Beyond this many callers, the fan-in number on the node says it better than boxes do.
MAX_DRAWN_CALLERS = 12


@dataclass
class TouchedSymbol:
    path: str
    definition: SymbolDef
    new_lines: int = 0
    old_lines: int = 0
    gone: bool = False
    added: bool = False
    pre_existing: bool = False

    @property
    def uid(self) -> str:
        return uid(self.path, self.definition.qualname)

    @property
    def changed_lines(self) -> int:
        """A modified line appears on both sides of the diff; counting both would report
        every one-line edit as two."""
        return self.new_lines or self.old_lines


def build_graph(
    repo: Repo,
    revisions: Revisions,
    *,
    hops: int = DEFAULT_HOPS,
    max_nodes: int = DEFAULT_MAX_NODES,
    jobs: int | None = None,
    utility_fan_in: int = DEFAULT_UTILITY_FAN_IN,
    exclude: tuple[str, ...] = (),
) -> Graph:
    changes = parse_diff(repo.diff(revisions))
    if revisions.head is None:
        # Reviewing the working tree: `git diff` cannot see files git does not know
        # about, so a brand-new file would be missing entirely with nothing to say so.
        changes.extend(_untracked_changes(repo, {c.path for c in changes}))
    unparsed: list[tuple[str, int]] = []
    touched = collect_touched(repo, revisions, changes, unparsed)

    index = build_index(repo, jobs=jobs, exclude=exclude)
    graph = Graph(
        files_changed=len(changes),
        files_indexed=index.files_indexed,
        files_parsed=index.files_parsed,
        unparsed=sorted(unparsed),
    )

    nodes: dict[str, Node] = {}
    for t in touched:
        nodes[t.uid] = Node(
            uid=t.uid,
            label=t.definition.qualname,
            path=t.path,
            kind=t.definition.kind,
            touched=True,
            changed_lines=t.changed_lines,
            gone=t.gone,
            added=t.added,
            pre_existing=t.pre_existing,
        )

    edges: dict[tuple[str, str, str], Edge] = {}
    frontier = set(nodes)
    skipped: set[str] = set()
    for _ in range(max(hops, 0)):
        frontier = _expand(index, nodes, edges, frontier, utility_fan_in, skipped)
        if not frontier:
            break

    # Edges between nodes already in the set but not discovered during expansion
    # (for example two touched symbols that call each other).
    _link_existing(index, nodes, edges)

    for node in nodes.values():
        node.fan_in = index.fan_in(node.uid)
        node.fan_in_shared = index.fan_in_is_shared(node.uid)
    for edge in edges.values():
        if edge.src in nodes:
            nodes[edge.src].degree += 1
        if edge.dst in nodes:
            nodes[edge.dst].degree += 1

    graph.nodes = list(nodes.values())
    graph.edges = list(edges.values())
    graph.utilities_hidden = len(skipped)
    _cap(graph, max_nodes)
    _number_calls(graph)
    return graph


def collect_touched(
    repo: Repo,
    revisions: Revisions,
    changes: list[FileChange],
    unparsed: list[tuple[str, int]] | None = None,
) -> list[TouchedSymbol]:
    """Map every changed line to its smallest enclosing definition.

    Line count never gates inclusion: one changed line makes its enclosing function a
    first-class member of the graph, exactly like a hundred would.
    """
    unparsed = [] if unparsed is None else unparsed
    existing: set[str] = set()
    touched: dict[str, TouchedSymbol] = {}
    relevant = [c for c in changes if adapter_for(c.path) is not None]
    # Files in no supported language. Saying nothing about them reads as "nothing
    # changed here", so they are carried through and listed explicitly.
    unparsed.extend(
        (c.path, len(c.new_lines) or len(c.old_lines))
        for c in changes
        if adapter_for(c.path) is None
    )

    # Both sides are fetched up front in two git calls rather than one per file.
    new_blobs = repo.read_many(
        revisions.head,
        [c.path for c in relevant if c.status != DELETED and c.new_lines],
    )
    old_blobs = repo.read_many(
        revisions.base, [c.old_path for c in relevant if c.old_lines]
    )

    for change in relevant:
        source = new_blobs.get(change.path)
        if change.status != DELETED and change.new_lines and source is not None:
            parsed = _parse_blob(change.path, source)
            if parsed is not None:
                facts, lines = parsed
                _attribute(
                    touched,
                    change.path,
                    facts,
                    change.new_lines,
                    lines,
                    side="new",
                    gone=False,
                    added=change.status == ADDED,
                )

        source = old_blobs.get(change.old_path)
        if source is not None:
            parsed = _parse_blob(change.old_path, source)
            if parsed is not None:
                existing.update(q.qualname for q in parsed[0].defs)
        if change.old_lines and source is not None:
            parsed = _parse_blob(change.old_path, source)
            if parsed is not None:
                facts, lines = parsed
                _attribute(
                    touched,
                    change.path,
                    facts,
                    change.old_lines,
                    lines,
                    side="old",
                    gone=change.status == DELETED,
                )

    for symbol in touched.values():
        symbol.pre_existing = symbol.definition.qualname in existing
    return list(touched.values())


def _untracked_changes(repo: Repo, known: set[str]) -> list[FileChange]:
    """Untracked files, presented as what they are: wholly new."""
    out: list[FileChange] = []
    paths = [p for p in repo.untracked_files() if p not in known]
    for path, blob in repo.read_many(None, paths).items():
        line_count = len(blob.decode("utf-8", "replace").splitlines())
        if not line_count:
            continue
        out.append(
            FileChange(
                path=path,
                old_path=path,
                status=ADDED,
                new_lines=list(range(1, line_count + 1)),
            )
        )
    return out


def _attribute(
    touched: dict[str, TouchedSymbol],
    path: str,
    facts: FileFacts,
    lines: list[int],
    source: list[str],
    *,
    side: str,
    gone: bool,
    added: bool = False,
) -> None:
    counts: dict[str, int] = defaultdict(int)
    found: dict[str, SymbolDef] = {}
    for line in lines:
        # A blank separator line sits inside the enclosing class but inside no method,
        # so counting it would invent a "the class changed" node every time a method is
        # added. Whitespace carries no meaning worth drawing.
        if 1 <= line <= len(source) and not source[line - 1].strip():
            continue
        definition = enclosing_symbol(facts.defs, line)
        if definition is None:
            # Module scope: a constant, an import, a config value. It has no callers to
            # expand into, but it did change, and dropping it would silently lose whole
            # files from the review.
            definition = _module_symbol(path, len(source))
        counts[definition.qualname] += 1
        found[definition.qualname] = definition

    for qualname, definition in found.items():
        key = uid(path, qualname)
        existing = touched.get(key)
        if existing is None:
            existing = TouchedSymbol(
                path=path, definition=definition, gone=gone, added=added
            )
            touched[key] = existing
        else:
            existing.gone = existing.gone and gone
        if side == "new":
            existing.new_lines += counts[qualname]
        else:
            existing.old_lines += counts[qualname]


MODULE_KIND = "module"


def _module_symbol(path: str, line_count: int) -> SymbolDef:
    name = path.rsplit("/", 1)[-1]
    return SymbolDef(
        name=name,
        qualname=name,
        kind=MODULE_KIND,
        start_line=1,
        end_line=max(line_count, 1),
    )


def _parse_blob(path: str, source: bytes) -> tuple[FileFacts, list[str]] | None:
    adapter = adapter_for(path)
    if adapter is None:
        return None
    facts = extract(adapter, source)
    if facts is None:
        return None
    return facts, source.decode("utf-8", "replace").splitlines()


def _expand(
    index: RepoIndex,
    nodes: dict[str, Node],
    edges: dict[tuple[str, str, str], Edge],
    frontier: set[str],
    utility_fan_in: int,
    skipped: set[str],
) -> set[str]:
    """One hop outward — callers in, callees out. Never a transitive closure."""
    discovered: set[str] = set()

    for src in frontier:
        for callee_name, form, scope in index.callees_of.get(src, ()):
            reachable = []
            for target in index.candidates(callee_name):
                if _is_utility(index, nodes, target, utility_fan_in):
                    skipped.add(target)
                    continue
                reachable.append(target)
            _add_edges(index, nodes, edges, discovered, src, reachable, CALL, form, scope)

        definition = index.defs.get(src)
        if definition is not None:
            callers = {
                (c, f, sc)
                for c, f, sc in index.callers_of_name.get(definition.name, ())
                if c != src
            }
            # Past a dozen or so, drawing each caller adds nothing the node's own fan-in
            # count does not already say, and on a heavily-used helper it would build
            # tens of thousands of boxes only for the cap to throw them away.
            if len(callers) <= MAX_DRAWN_CALLERS:
                for caller, caller_form, caller_scope in callers:
                    _add_edges(
                        index, nodes, edges, discovered, caller, [src], CALL,
                        caller_form, caller_scope,
                    )

        for base_name in index.bases_of.get(src, ()):
            targets = index.candidates(base_name)
            _add_edges(index, nodes, edges, discovered, src, targets, IMPLEMENTS)

        for target in _overridden(index, src):
            _add_edges(index, nodes, edges, discovered, src, [target], IMPLEMENTS)

    return discovered


def _overridden(index: RepoIndex, src: str) -> list[str]:
    """Link a touched method to the base/interface method it implements.

    The hub this produces — one interface method with every implementation hanging off
    it — is the shape that makes a wide "same edit applied N times" change readable. It
    is tied to the method rather than the class because the method is what changed.
    """
    definition = index.defs.get(src)
    if definition is None or definition.kind != METHOD or not definition.parent:
        return []

    class_uid = uid(index.path_of.get(src, ""), definition.parent)
    targets: list[str] = []
    for base_name in index.bases_of.get(class_uid, ()):
        for sibling in index.candidates(definition.name):
            sibling_def = index.defs.get(sibling)
            if sibling_def is None or sibling == src:
                continue
            if sibling_def.parent == base_name:
                targets.append(sibling)
    return targets


def _add_edges(
    index: RepoIndex,
    nodes: dict[str, Node],
    edges: dict[tuple[str, str, str], Edge],
    discovered: set[str],
    src: str,
    targets: list[str],
    kind: str,
    form: str = PLAIN,
    scope: str = "",
) -> None:
    targets = [t for t in targets if t != src]
    if kind == CALL:
        # A declaration with no body — an interface method, or a `.stub.php` entry for a
        # language builtin — is a target for `implements`, never an interesting call
        # target. Without this, every `implode()` in the diff drags a stub file in.
        targets = [t for t in targets if _has_body(index, t)]
    if not targets:
        # Unresolved: the callee is stdlib, a vendored package, or genuinely absent.
        # Silently dropping is right here — drawing edges to things outside the repo
        # would bury the change under noise.
        return

    targets, provenance = _narrow(index, src, targets, form, scope)
    if not targets:
        return

    # The source needs a node too. When expanding *inwards* — "what calls this changed
    # function" — the source is a caller discovered from the index, not something already
    # in the graph, and an edge whose endpoint does not exist is silently dropped by every
    # renderer downstream.
    if not _ensure_node(index, nodes, discovered, src):
        return

    for target in targets:
        if not _ensure_node(index, nodes, discovered, target):
            continue
        edge = Edge(src=src, dst=target, kind=kind, provenance=provenance)
        edges.setdefault(edge.key, edge)


def _ensure_node(
    index: RepoIndex, nodes: dict[str, Node], discovered: set[str], symbol_uid: str
) -> bool:
    if symbol_uid in nodes:
        return True
    definition = index.defs.get(symbol_uid)
    if definition is None:
        return False
    nodes[symbol_uid] = Node(
        uid=symbol_uid,
        label=definition.qualname,
        path=index.path_of.get(symbol_uid, ""),
        kind=definition.kind,
        touched=False,
    )
    discovered.add(symbol_uid)
    return True


_MAX_SCOPE_DEPTH = 6


def _is_utility(
    index: RepoIndex, nodes: dict[str, Node], target: str, threshold: int
) -> bool:
    """Shared plumbing pulled in as context, rather than part of this change.

    Only ever applies to symbols that did not change and are not already in the graph —
    a changed symbol keeps its place no matter how widely it is called.
    """
    if threshold <= 0 or target in nodes:
        return False
    return index.fan_in(target) > threshold


def _scope_chain(index: RepoIndex, scope: str) -> set[str]:
    """A class name plus everything it extends or implements, transitively.

    Depth-bounded: a malformed or cyclic hierarchy must not hang a review.
    """
    seen = {scope}
    frontier = [scope]
    for _ in range(_MAX_SCOPE_DEPTH):
        nxt: list[str] = []
        for name in frontier:
            for uid_ in index.candidates(name):
                for base in index.bases_of.get(uid_, ()):
                    if base not in seen:
                        seen.add(base)
                        nxt.append(base)
        if not nxt:
            break
        frontier = nxt
    return seen


def _owner(index: RepoIndex, symbol_uid: str) -> str:
    """The class a method belongs to, or "" for a free function."""
    definition = index.defs.get(symbol_uid)
    return definition.parent or "" if definition else ""


def _has_body(index: RepoIndex, symbol_uid: str) -> bool:
    definition = index.defs.get(symbol_uid)
    return definition is None or definition.has_body


def _narrow(
    index: RepoIndex, src: str, targets: list[str], form: str = PLAIN, scope: str = ""
) -> tuple[list[str], str]:
    """Pick the plausible targets of a call, and say how confident that is.

    Confidence follows from *how the call names its target*, not from how many
    definitions happen to share the name:

    - `helper()` — unqualified. One definition in the repo really is the target: STATIC.
    - `$this->helper()` — the receiver is this class, so a same-file match is the real
      resolution: STATIC. This also stops six sibling classes defining `fees` from
      turning every `self.` call into a fan-out.
    - `$obj->helper()` — the receiver's type is unknown, so a name match is a guess even
      when it is the only one; the true target may be a vendor class never indexed:
      INFERRED.
    - `'helper'` passed as a string — a callable reference we recognised by pattern:
      HEURISTIC.

    Several surviving candidates means dispatch of some kind (interface, duck typing,
    reflection, plain collision): fan out to all of them and label it AMBIGUOUS, rather
    than picking one and looking certain about it.
    """
    # A call never crosses a language boundary. Without this, a Python script calling
    # `open()` links to a PHP class that happens to define an `open` method — names are
    # shared freely across languages, and such an edge is simply false.
    src_lang = index.lang_of.get(src)
    if src_lang:
        same_lang = [t for t in targets if index.lang_of.get(t) == src_lang]
        if not same_lang:
            return [], STATIC
        targets = same_lang

    # The call site named the receiver's class — `app(OrderService::class)->show()`,
    # `new Foo()->bar()`, `Foo::bar()`, or a typed property. That is a declared fact, so
    # honour it: without it, one precise call fans out across every same-named method in
    # the repo, which is how `OrderController::show` ended up pointing at three other
    # controllers.
    if scope:
        # A method called on `OrderRepository` may well be declared on a class it
        # extends, so the whole ancestor chain counts as in-scope — otherwise every
        # inherited call is silently dropped.
        family = _scope_chain(index, scope)
        in_scope = [t for t in targets if _owner(index, t) in family]
        if in_scope:
            return in_scope, STATIC if len(in_scope) == 1 else AMBIGUOUS
        # The named class is not in this repo (a vendor or framework type). Claiming a
        # same-named method here would be a guess dressed as a fact.
        return [], STATIC

    if len(targets) > 1:
        same_file = [t for t in targets if index.path_of.get(t) == index.path_of.get(src)]
        if len(same_file) == 1 and form in (PLAIN, SELF):
            return same_file, STATIC

    if len(targets) > 1:
        return targets, AMBIGUOUS
    if form == RECEIVER:
        return targets, INFERRED
    if form == LITERAL:
        return targets, HEURISTIC
    return targets, STATIC


def _link_existing(
    index: RepoIndex,
    nodes: dict[str, Node],
    edges: dict[tuple[str, str, str], Edge],
) -> None:
    for src in list(nodes):
        for callee_name, form, scope in index.callees_of.get(src, ()):
            # Same body filter as the expansion path: an interface declaration is not a
            # call target, and applying the rule in only one place made the two paths
            # disagree about the very same call.
            reachable = [t for t in index.candidates(callee_name) if _has_body(index, t)]
            candidates, provenance = _narrow(index, src, reachable, form, scope)
            for target in candidates:
                if target not in nodes or target == src:
                    continue
                edge = Edge(src=src, dst=target, kind=CALL, provenance=provenance)
                edges.setdefault(edge.key, edge)


def _cap(graph: Graph, max_nodes: int) -> None:
    """Keep the graph readable. Touched symbols always survive; context is what gets cut."""
    if max_nodes <= 0 or len(graph.nodes) <= max_nodes:
        return

    ranked = sorted(
        graph.nodes,
        key=lambda n: (not n.touched, -n.fan_in, -n.degree, n.uid),
    )
    keep = {n.uid for n in ranked[:max_nodes]}
    graph.truncated = len(graph.nodes) - len(keep)

    # Record what the cut severed. A kept symbol whose caller was dropped would
    # otherwise look unreferenced, which reads as a missing edge rather than a hidden
    # one — the graph must not appear complete when it is not.
    by_uid = {n.uid: n for n in graph.nodes}
    for edge in graph.edges:
        if edge.src in keep and edge.dst not in keep:
            by_uid[edge.src].hidden_out += 1
        elif edge.dst in keep and edge.src not in keep:
            by_uid[edge.dst].hidden_in += 1

    graph.nodes = [n for n in graph.nodes if n.uid in keep]
    graph.edges = [e for e in graph.edges if e.src in keep and e.dst in keep]


def _number_calls(graph: Graph) -> None:
    """Number call edges along each path so a chain reads 1, 2, 3.

    The order is the lexical order of call sites, which is what a reader follows; it is
    not an execution trace and is not presented as one.
    """
    outgoing: dict[str, list[Edge]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind == CALL:
            outgoing[edge.src].append(edge)
    if not outgoing:
        return

    has_incoming = {e.dst for e in graph.edges if e.kind == CALL}
    roots = [n.uid for n in graph.nodes if n.uid in outgoing and n.uid not in has_incoming]
    if not roots:
        roots = sorted(outgoing)

    counter = 0
    seen: set[tuple[str, str]] = set()

    def visit(uid_: str) -> None:
        nonlocal counter
        for edge in sorted(outgoing.get(uid_, ()), key=lambda e: (e.line or 0, e.dst)):
            if (edge.src, edge.dst) in seen:
                continue
            seen.add((edge.src, edge.dst))
            counter += 1
            edge.order = counter
            visit(edge.dst)

    for root in sorted(roots):
        visit(root)
