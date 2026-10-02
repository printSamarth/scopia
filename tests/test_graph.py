"""Behavioural tests over the fixture repos.

Assertions are against the graph, never against rendered output — rendering should be
free to change without breaking correctness tests.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from scopia.graph.build import build_graph
from scopia.graph.model import AMBIGUOUS, CALL
from scopia.vcs.git import parse_revisions

RANGE = "HEAD~1..HEAD"


@pytest.fixture(scope="module")
def py_graph(py_fixture):
    return build_graph(py_fixture, parse_revisions(RANGE))


@pytest.fixture(scope="module")
def php_graph(php_fixture):
    return build_graph(php_fixture, parse_revisions(RANGE))


def labels(graph, *, touched_only=True):
    return {n.label for n in (graph.touched if touched_only else graph.nodes)}


# --- requirement 3: a one-line change is a first-class member of the graph -----------


def test_one_line_change_in_high_fan_in_helper_is_present(py_graph):
    clamp = next((n for n in py_graph.touched if n.label == "clamp"), None)
    assert clamp is not None, "a 1-line change must still produce a node"
    assert clamp.changed_lines == 1


def test_one_line_change_outranks_larger_ones_by_fan_in(py_graph):
    clamp = next(n for n in py_graph.touched if n.label == "clamp")
    bigger = [n for n in py_graph.touched if n.changed_lines > clamp.changed_lines]
    assert bigger, "fixture should contain larger changes to compare against"
    assert all(clamp.fan_in > n.fan_in for n in bigger), (
        "a small change to a widely-called symbol must rank above larger, isolated ones"
    )


def test_one_line_change_is_reachable_through_the_call_chain(py_graph):
    """The whole point of requirement 3: the tiny edit is not an orphan."""
    clamp = next(n for n in py_graph.touched if n.label == "clamp")
    incoming = [e for e in py_graph.edges if e.dst == clamp.uid and e.kind == CALL]
    assert incoming, "the 1-line change must be connected to its callers"


def test_changed_lines_are_not_double_counted(py_graph):
    config = [n for n in py_graph.touched if n.path.startswith("config/")]
    assert config
    assert all(n.changed_lines == 1 for n in config), (
        "a modified line appears on both diff sides but is one change"
    )


# --- shape 1: the call chain ---------------------------------------------------------


def test_call_chain_is_connected_end_to_end(py_graph):
    by_label = {n.label: n for n in py_graph.nodes}
    chain = [
        "CheckoutForm.submit",
        "CheckoutController.apply_promo_code",
        "DiscountService.validate",
        "PricingEngine.recalculate",
    ]
    for label in chain:
        assert label in by_label, f"{label} missing from graph"

    edges = {(e.src, e.dst) for e in py_graph.edges}
    for src, dst in pairwise(chain):
        assert (by_label[src].uid, by_label[dst].uid) in edges, f"{src} -> {dst} missing"


def test_call_edges_are_numbered_in_reading_order(py_graph):
    orders = [e.order for e in py_graph.edges if e.kind == CALL and e.order]
    assert orders, "call edges should carry a reading order"
    assert len(orders) == len(set(orders)), "numbering must be unique"


# --- shape 2: the interface fan-out --------------------------------------------------


def test_every_implementation_is_present(py_graph):
    found = labels(py_graph)
    for provider in ("Stripe", "Paypal", "Braintree", "Klarna", "Affirm", "Applepay"):
        assert f"{provider}Provider.calculate_refund" in found


def test_deviating_implementation_differs_structurally(py_graph):
    """ApplePay omits the clamp call the other five share.

    The tool does not label this wrong — it simply has a different edge set, which is
    what makes it the one worth reading.
    """
    by_label = {n.label: n for n in py_graph.nodes}
    clamp = by_label["clamp"]
    calls_clamp = {
        e.src for e in py_graph.edges if e.dst == clamp.uid and e.kind == CALL
    }
    applepay = by_label["ApplepayProvider.calculate_refund"]
    stripe = by_label["StripeProvider.calculate_refund"]
    assert stripe.uid in calls_clamp
    assert applepay.uid not in calls_clamp


# --- shape 3: unconnected edits -------------------------------------------------------


def test_module_level_changes_are_not_lost(py_graph):
    """A config constant belongs to no function, but the file still changed."""
    config = {n.path for n in py_graph.touched if n.path.startswith("config/")}
    assert len(config) == 5


def test_unconnected_edits_have_no_edges(py_graph):
    linked = {e.src for e in py_graph.edges} | {e.dst for e in py_graph.edges}
    config = [n for n in py_graph.touched if n.path.startswith("config/")]
    assert all(n.uid not in linked for n in config)


# --- requirement 1: ambiguity is shown, not guessed -----------------------------------


def test_ambiguous_calls_fan_out_to_every_candidate(py_fixture):
    """A call through the base class cannot be resolved to one implementation."""
    graph = build_graph(py_fixture, parse_revisions(RANGE), hops=2)
    ambiguous = [e for e in graph.edges if e.provenance == AMBIGUOUS]
    for edge in ambiguous:
        siblings = [e for e in ambiguous if e.src == edge.src]
        assert len(siblings) > 1, "an ambiguous edge must come with its alternatives"


def test_same_file_calls_are_not_treated_as_ambiguous(py_graph):
    """`self.fees()` resolves within its own class despite six sibling definitions."""
    by_label = {n.label: n for n in py_graph.nodes}
    src = by_label["StripeProvider.calculate_refund"]
    fees_edges = [
        e
        for e in py_graph.edges
        if e.src == src.uid and by_label_of(py_graph, e.dst).endswith(".fees")
    ]
    assert fees_edges
    assert all(e.provenance != AMBIGUOUS for e in fees_edges)
    assert all(by_label_of(py_graph, e.dst).startswith("StripeProvider") for e in fees_edges)


def by_label_of(graph, uid: str) -> str:
    node = graph.node(uid)
    return node.label if node else ""


def test_python_receiver_types_keep_the_chain_whole(py_fixture):
    """`c = CheckoutController()` then `c.apply_promo_code()`.

    The assignment states the type. Ignoring it split one real path — form to controller
    to service to engine — into four disconnected roots, which is the opposite of the
    single picture the report exists to give.
    """
    graph = build_graph(py_fixture, parse_revisions(RANGE), hops=1)
    by_label = {n.display: n for n in graph.nodes}
    edges = {(e.src, e.dst): e for e in graph.edges}

    chain = [
        "checkout_form.CheckoutForm.submit",
        "checkout_controller.CheckoutController.apply_promo_code",
        "discount_service.DiscountService.validate",
    ]
    for src, dst in pairwise(chain):
        key = (by_label[src].uid, by_label[dst].uid)
        assert key in edges, f"{src} -> {dst} missing"
        assert edges[key].provenance == "static", "the assignment makes this certain"


def test_constructed_receiver_resolves(py_fixture):
    """`PricingEngine().recalculate()` names its class without ever naming a variable."""
    graph = build_graph(py_fixture, parse_revisions(RANGE), hops=1)
    by_label = {n.display: n for n in graph.nodes}
    edges = {(e.src, e.dst) for e in graph.edges}
    assert (
        by_label["discount_service.DiscountService.validate"].uid,
        by_label["pricing_engine.PricingEngine.recalculate"].uid,
    ) in edges


def test_untracked_files_are_reviewed(untracked_fixture):
    """`git diff` cannot see a file git has not been told about.

    A brand-new file is often the bulk of a change, and omitting it produced confident
    silence — the report claimed one file changed while an entire new module sat beside
    it, unmentioned.
    """
    graph = build_graph(untracked_fixture, parse_revisions(None), hops=1)
    labels = {n.display for n in graph.nodes}
    assert "feature.brand_new_feature" in labels, "untracked file must be reviewed"
    assert "old.existing" in labels

    by_label = {n.display: n for n in graph.nodes}
    edges = {(e.src, e.dst) for e in graph.edges}
    assert (
        by_label["feature.brand_new_feature"].uid,
        by_label["old.existing"].uid,
    ) in edges, "and its calls must resolve like any other file's"


def test_untracked_respects_gitignore(untracked_fixture):
    """Including untracked files must not drag in what the user chose to ignore."""
    graph = build_graph(untracked_fixture, parse_revisions(None))
    assert not any(n.path.endswith(".log") for n in graph.nodes)
    assert not any(path.endswith(".log") for path, _ in graph.unparsed)


def test_aliased_import_keeps_the_chain_intact(feedback_fixture):
    """`from pipeline import run as pipeline_v3` then `pipeline_v3()`.

    Reported break: the one path the report exists to show — scheduled job to new
    pipeline to data helper — split into three disconnected chains, because the call
    used the alias and the definition used its real name.
    """
    graph = build_graph(feedback_fixture, parse_revisions(RANGE), hops=1)
    by_label = {n.display: n for n in graph.nodes}
    edges = {(e.src, e.dst) for e in graph.edges}

    assert "jobs.process_configs" in by_label
    assert "pipeline.run" in by_label
    assert (by_label["jobs.process_configs"].uid, by_label["pipeline.run"].uid) in edges, (
        "the call through the alias must reach the real definition"
    )


def test_edits_to_pre_existing_code_are_flagged(feedback_fixture):
    """"Does this change anything that already exists?" is a reviewer's first question."""
    graph = build_graph(feedback_fixture, parse_revisions(RANGE))
    by_label = {n.display: n for n in graph.nodes}
    assert by_label["sql.sql_for"].pre_existing, "edits existing behaviour"
    assert not by_label["pipeline.run"].pre_existing, "brand new"


def test_unparseable_files_are_reported_not_ignored(feedback_fixture):
    """Silence about a changed file reads as 'nothing changed here'."""
    graph = build_graph(feedback_fixture, parse_revisions(RANGE))
    listed = {path for path, _ in graph.unparsed}
    assert "tools/config.json" in listed


def test_labels_do_not_repeat_the_filename(php_scope_fixture):
    """`Controller.Controller.respond` is noise, not context.

    One class per file named after the file is the PSR-4 norm, so the stem prefix has
    to stand down when the qualname already starts with it.
    """
    graph = build_graph(php_scope_fixture, parse_revisions(RANGE))
    for node in graph.nodes:
        head = node.display.split(".")[0]
        assert not node.display.startswith(f"{head}.{head}."), node.display
    labels = {n.display for n in graph.nodes}
    assert "Controller.show" in labels


def test_widely_called_helpers_are_hidden_as_plumbing(plumbing_fixture):
    """A logger every function calls says nothing about *this* change."""
    noisy = build_graph(plumbing_fixture, parse_revisions(RANGE), utility_fan_in=0)
    quiet = build_graph(plumbing_fixture, parse_revisions(RANGE), utility_fan_in=12)

    assert any(n.label == "audit" for n in noisy.nodes), "fixture should reach the logger"
    assert not any(n.label == "audit" for n in quiet.nodes)
    assert quiet.utilities_hidden > 0, "hiding must be reported, never silent"


def test_plumbing_filter_never_hides_a_changed_symbol(plumbing_fixture):
    """The same fan-in that marks plumbing marks importance once it is touched.

    Requirement 3 outranks tidiness: a small edit to something called from everywhere
    is the single most valuable thing the graph can show.
    """
    graph = build_graph(plumbing_fixture, parse_revisions(RANGE), utility_fan_in=1)
    touched = {n.label for n in graph.touched}
    assert "handle" in touched
    assert all(n.touched or n.fan_in <= 1 for n in graph.nodes)


def test_excluded_paths_are_never_indexed(plumbing_fixture):
    graph = build_graph(
        plumbing_fixture, parse_revisions(RANGE), utility_fan_in=0, exclude=("app/mod*",)
    )
    assert not any(n.path.startswith("app/mod") for n in graph.nodes)


def test_named_receiver_class_resolves_precisely(php_scope_fixture):
    """When the code names the receiver's class, use it.

    Regression from php-dummy: `app(OrderService::class)->show()` fanned out to all
    eight `show` methods in the repo — including three unrelated controllers — because
    resolution ignored the class written at the call site.
    """
    graph = build_graph(php_scope_fixture, parse_revisions(RANGE), hops=1)
    by_uid = {n.uid: n for n in graph.nodes}
    caller = next(n for n in graph.nodes if n.label == "Controller.show")

    show_targets = {
        by_uid[e.dst].label for e in graph.edges if e.src == caller.uid and e.dst in by_uid
    }
    # Each of the four call forms landed on exactly the class it named.
    assert "UserService.show" in show_targets, "::class argument"
    assert "ProductService.show" in show_targets, "new X()"
    assert "OutletService.show" in show_targets, "static X::y()"
    assert "OrderService.show" in show_targets, "typed property"
    # And nothing fanned out to the others.
    assert "Controller.show" not in show_targets


def test_named_class_resolution_is_certain(php_scope_fixture):
    graph = build_graph(php_scope_fixture, parse_revisions(RANGE), hops=1)
    by_uid = {n.uid: n for n in graph.nodes}
    caller = next(n for n in graph.nodes if n.label == "Controller.show")
    resolved = [
        e
        for e in graph.edges
        if e.src == caller.uid and by_uid.get(e.dst) and by_uid[e.dst].label.endswith(".show")
    ]
    assert resolved
    assert all(e.provenance == "static" for e in resolved), (
        "a class named at the call site is a declared fact, not a guess"
    )


def test_inherited_method_still_resolves(php_scope_fixture):
    """`app(OrderRepo::class)->find()` where `find` lives on the parent class.

    Regression: scoping to the exact class silently dropped every inherited call.
    """
    graph = build_graph(php_scope_fixture, parse_revisions(RANGE), hops=1)
    by_uid = {n.uid: n for n in graph.nodes}
    caller = next(n for n in graph.nodes if n.label == "Controller.show")
    targets = {by_uid[e.dst].label for e in graph.edges if e.src == caller.uid and e.dst in by_uid}
    assert "BaseRepo.find" in targets


def test_calls_never_cross_a_language_boundary(mixed_fixture):
    """Regression, found running against php-src: a Python gdb script was linked to a
    PHP class because both define a method with the same name. Names are shared freely
    across languages; such an edge is simply false."""
    graph = build_graph(mixed_fixture, parse_revisions(RANGE), hops=2)
    by_uid = {n.uid: n for n in graph.nodes}

    def language(uid: str) -> str:
        return by_uid[uid].path.rsplit(".", 1)[-1]

    crossings = [
        (by_uid[e.src].label, by_uid[e.dst].label)
        for e in graph.edges
        if language(e.src) != language(e.dst)
    ]
    assert not crossings, f"cross-language edges: {crossings}"
    # All three sides still resolve within their own language.
    for extension in ("py", "php", "ts"):
        assert any(language(e.src) == extension for e in graph.edges)


# --- requirement 2: bounds hold -------------------------------------------------------


def test_no_edge_dangles(py_graph):
    """Every edge endpoint must exist as a node.

    Regression: expanding inwards ("what calls this?") used to create edges from caller
    symbols that were never added to the graph. Renderers skip unknown endpoints
    silently, so whole call chains vanished while the header still counted them.
    """
    uids = {n.uid for n in py_graph.nodes}
    dangling = [(e.src, e.dst) for e in py_graph.edges if e.src not in uids or e.dst not in uids]
    assert not dangling


def test_callers_of_a_changed_leaf_are_shown(py_graph):
    """A changed symbol that calls nothing still needs its callers drawn."""
    clamp = next(n for n in py_graph.touched if n.label == "clamp")
    callers = [e.src for e in py_graph.edges if e.dst == clamp.uid]
    assert callers
    uids = {n.uid for n in py_graph.nodes}
    assert all(c in uids for c in callers)


def test_cap_reports_the_neighbours_it_severed(py_fixture):
    """A kept symbol whose caller was cut must say so.

    Regression from php-dummy: `UserService.list` looked unreferenced because the cap
    removed `UserController.index`. The edge was computed correctly — it was dropped at
    render time, and nothing said so, which reads as a bug in the analysis.
    """
    graph = build_graph(py_fixture, parse_revisions(RANGE), max_nodes=6)
    assert graph.truncated > 0
    severed = sum(n.hidden_in + n.hidden_out for n in graph.nodes)
    assert severed > 0, "cutting nodes must leave a visible trace on what remains"


def test_no_hidden_neighbours_when_nothing_is_cut(py_fixture):
    graph = build_graph(py_fixture, parse_revisions(RANGE), max_nodes=500)
    assert graph.truncated == 0
    assert all(n.hidden_in == 0 and n.hidden_out == 0 for n in graph.nodes)


def test_node_cap_is_respected_and_keeps_touched_symbols(py_fixture):
    graph = build_graph(py_fixture, parse_revisions(RANGE), max_nodes=8)
    assert len(graph.nodes) <= 8
    assert graph.truncated > 0
    assert all(e.src in {n.uid for n in graph.nodes} for e in graph.edges)


def test_expansion_is_bounded_not_transitive(py_fixture):
    one = build_graph(py_fixture, parse_revisions(RANGE), hops=1, max_nodes=500)
    two = build_graph(py_fixture, parse_revisions(RANGE), hops=2, max_nodes=500)
    assert len(two.nodes) >= len(one.nodes)


def test_index_cache_avoids_reparsing(py_fixture):
    import shutil

    shutil.rmtree(py_fixture.root / ".git" / "scopia", ignore_errors=True)
    cold = build_graph(py_fixture, parse_revisions(RANGE))
    warm = build_graph(py_fixture, parse_revisions(RANGE))
    assert cold.files_parsed > 0, "a cold run has to parse the repo"
    assert warm.files_parsed == 0, "a warm run must be served entirely from cache"
    assert warm.files_indexed == cold.files_indexed


# --- php: the language seam is real ---------------------------------------------------


def test_php_chain_and_implementations(php_graph):
    found = labels(php_graph)
    assert "CheckoutController.applyPromoCode" in found
    assert "DiscountService.validate" in found
    assert "PricingEngine.recalculate" in found
    for provider in ("Stripe", "Paypal", "ApplePay"):
        assert f"{provider}Provider.calculateRefund" in found


def test_php_one_line_change_is_high_fan_in(php_graph):
    clamp = next((n for n in php_graph.touched if n.label == "clamp"), None)
    assert clamp is not None
    assert clamp.changed_lines == 1
    assert clamp.fan_in >= 5


def test_php_interface_edge_is_recorded(php_graph):
    implements = [e for e in php_graph.edges if e.kind != CALL]
    assert implements, "implements/extends should produce edges"


# --- react: composition is the call graph ---------------------------------------------


@pytest.fixture(scope="module")
def react_graph(react_fixture):
    return build_graph(react_fixture, parse_revisions(RANGE), hops=1, max_nodes=500)


def test_react_chain_is_connected_end_to_end(react_graph):
    """Five layers, five files, three grammars — and a JSX prop holding it together.

    `onSubmit={handleSubmit}` is the first hop and no call expression states it; the
    second crosses `.tsx` to `.ts`, and the last lands in a shared helper.
    """
    by_label = {n.display: n for n in react_graph.nodes}
    chain = [
        "CheckoutForm",
        "CheckoutForm.handleSubmit",
        "useCheckout.applyPromoCode",
        "DiscountService.validate",
        "PricingEngine.recalculate",
        "util.clamp",
    ]
    for label in chain:
        assert label in by_label, f"{label} missing from graph"

    edges = {(e.src, e.dst) for e in react_graph.edges}
    for src, dst in pairwise(chain):
        assert (by_label[src].uid, by_label[dst].uid) in edges, f"{src} -> {dst} missing"


def test_jsx_element_is_a_call_edge(react_graph):
    """`<Field />` is how React invokes a component, and the only thing that says so.

    Without it a component that renders nothing but children looks like a leaf, and a
    change to a shared card reaches no screen at all.
    """
    by_label = {n.display: n for n in react_graph.nodes}
    edges = {(e.src, e.dst): e for e in react_graph.edges}
    key = (by_label["CheckoutForm"].uid, by_label["PromoField"].uid)
    assert key in edges, "rendering a component must be an edge"
    assert edges[key].provenance == "static"


def test_host_elements_are_not_symbols(react_graph):
    """`<form>` and `<input>` are DOM, not code in this repo.

    Capitalisation is not a convention to be guessed at here — it is how JSX itself
    decides, so reading it costs no confidence.
    """
    labels = {n.display for n in react_graph.nodes}
    assert not labels & {"form", "input", "span", "div"}


def test_default_import_under_another_name_still_resolves(react_graph):
    """`import Field from './PromoField'` then `<Field />`.

    The call says `Field`, the definition says `PromoField`, and only the module path
    connects them — the JSX twin of the aliased-import break.
    """
    by_label = {n.display: n for n in react_graph.nodes}
    assert "Field" not in by_label, "the local name is not a definition"
    incoming = [e for e in react_graph.edges if e.dst == by_label["PromoField"].uid]
    assert incoming, "the renamed import must reach the real component"


def test_components_written_as_const_are_definitions(react_graph):
    """`const Summary = () => …` and `memo(function …)` — how React is actually written.

    An adapter that only records `function` declarations reports an empty file here.
    """
    by_label = {n.display: n for n in react_graph.nodes}
    assert "Summary" in by_label, "arrow component"
    assert "Summary.Total" in by_label, "memo-wrapped component"
    edges = {(e.src, e.dst) for e in react_graph.edges}
    assert (by_label["Summary"].uid, by_label["Summary.Total"].uid) in edges
    assert (by_label["Summary.Total"].uid, by_label["util.clamp"].uid) in edges


def test_generic_styled_component_is_rendered(react_graph):
    """`styled.button<Props>\\`…\\`` — found writing a throwaway app, not a fixture.

    This grammar reads the type argument as two comparisons, so a check that matched
    the tree's shape missed it, and `<Button>` in the diff reached nothing.
    """
    by_label = {n.display: n for n in react_graph.nodes}
    assert "Button" in by_label
    edges = {(e.src, e.dst) for e in react_graph.edges}
    assert (by_label["CheckoutForm"].uid, by_label["Button"].uid) in edges


def test_store_built_by_a_factory_is_a_hook(react_graph):
    """`const useCart = create<CartState>(…)` — zustand, not a wrapper anyone listed.

    The `use` prefix on the binding is what rules-of-hooks enforces, so it is the one
    signal every store library shares. Its actions are symbols of their own.
    """
    by_label = {n.display: n for n in react_graph.nodes}
    assert "cart.useCart" in by_label
    edges = {(e.src, e.dst) for e in react_graph.edges}
    assert (by_label["CheckoutForm"].uid, by_label["cart.useCart"].uid) in edges
    assert "cart.useCart.total" in by_label, "a store action is reachable, not buried"


def test_hook_returning_a_value_is_not_a_definition(react_graph):
    """`const count = useCart((s) => s.total())` holds a number, not a function.

    Found by the test above: the `use` prefix on a *call* was read as "returns a
    callable", which made `count` a symbol and took the component's lines with it.
    """
    labels = {n.display for n in react_graph.nodes}
    assert "CheckoutForm.count" not in labels


def test_redux_toolkit_reducers_and_thunks_are_symbols(react_graph):
    """`createSlice({ reducers: { … } })` and `createAsyncThunk(…, async () => …)`.

    Found writing a throwaway Redux app: the new reducer read as "no connections found",
    and a one-line change to a helper looked unreferenced because its only caller was
    a thunk's arrow, owned by nothing.
    """
    by_label = {n.display: n for n in react_graph.nodes}
    edges = {(e.src, e.dst) for e in react_graph.edges}

    assert "todos.loadTodos" in by_label, "a thunk is a named unit of logic"
    assert (
        by_label["todos.loadTodos"].uid, by_label["useCheckout.applyPromoCode"].uid
    ) in edges

    assert "todos.todosSlice.completedCleared" in by_label
    assert (
        by_label["CheckoutForm.onClear"].uid,
        by_label["todos.todosSlice.completedCleared"].uid,
    ) in edges, "dispatching an action reaches the reducer that shares its name"


def test_hook_argument_is_followed_as_a_reference(react_graph):
    """`useSelector(selectTodos)` — the selector is the thing a reviewer follows next."""
    by_label = {n.display: n for n in react_graph.nodes}
    selector = by_label.get("todos.selectTodos")
    assert selector is not None
    incoming = [e for e in react_graph.edges if e.dst == selector.uid]
    assert incoming and all(e.provenance == "heuristic" for e in incoming)


def test_use_callback_handler_is_its_own_symbol(react_graph):
    """A one-line edit inside a `useCallback` handler is not an edit to the component."""
    handler = next(n for n in react_graph.touched if n.display == "CheckoutForm.handleSubmit")
    assert handler.changed_lines == 1


def test_declared_receiver_type_resolves_precisely(react_graph):
    """`const service: DiscountService = …` then `service.validate(…)`.

    Two classes in the fixture define `validate`. TypeScript says which one this is, and
    ignoring that would fan one precise call out across both.
    """
    by_label = {n.display: n for n in react_graph.nodes}
    by_uid = {n.uid: n for n in react_graph.nodes}
    caller = by_label["useCheckout.applyPromoCode"]
    targets = {
        by_uid[e.dst].display
        for e in react_graph.edges
        if e.src == caller.uid and e.dst in by_uid
    }
    assert "DiscountService.validate" in targets
    assert "AddressService.validate" not in targets


def test_react_one_line_change_in_high_fan_in_helper(react_graph):
    clamp = next((n for n in react_graph.touched if n.display == "util.clamp"), None)
    assert clamp is not None, "a 1-line change must still produce a node"
    assert clamp.changed_lines == 1
    assert clamp.fan_in >= 10


def test_react_every_implementation_is_present(react_graph):
    found = {n.label for n in react_graph.touched}
    for provider in ("Stripe", "Paypal", "Braintree", "Klarna", "Affirm", "Applepay"):
        assert f"{provider}Provider.calculateRefund" in found


def test_react_interface_edges_are_recorded(react_graph):
    """`implements Refundable` on six classes is the hub that makes a wide change readable."""
    by_uid = {n.uid: n for n in react_graph.nodes}
    implements = [
        e
        for e in react_graph.edges
        if e.kind != CALL and by_uid[e.dst].display == "Refundable.calculateRefund"
    ]
    assert len(implements) == 6


def test_react_deviating_implementation_differs_structurally(react_graph):
    by_label = {n.display: n for n in react_graph.nodes}
    clamp = by_label["util.clamp"]
    calls_clamp = {e.src for e in react_graph.edges if e.dst == clamp.uid and e.kind == CALL}
    assert by_label["stripe.StripeProvider.calculateRefund"].uid in calls_clamp
    assert by_label["applepay.ApplepayProvider.calculateRefund"].uid not in calls_clamp


def test_react_unconnected_edits_are_kept_and_unlinked(react_graph):
    config = [n for n in react_graph.touched if n.path.startswith("src/config/")]
    assert len(config) == 5, "a module-level constant belongs to no function, but it changed"
    linked = {e.src for e in react_graph.edges} | {e.dst for e in react_graph.edges}
    assert all(n.uid not in linked for n in config)


def test_jsx_and_ts_and_js_are_one_language(react_graph):
    """A React app imports across `.tsx`, `.ts` and `.jsx` freely.

    Three grammars parse them, and a call between them must not be read as a
    cross-language edge the way a Python call into PHP is.
    """
    by_uid = {n.uid: n for n in react_graph.nodes}
    crossings = {
        (by_uid[e.src].path.rsplit(".", 1)[-1], by_uid[e.dst].path.rsplit(".", 1)[-1])
        for e in react_graph.edges
    }
    assert ("tsx", "jsx") in crossings, "a .tsx component rendering a .jsx one"
    assert ("tsx", "ts") in crossings, "a component calling a hook"


# --- entry points: where does this change get triggered from? ------------------------


def test_climb_reaches_entry_points(entry_fixture):
    """A one-line change four levels down should name what triggers it.

    A fixed hop count cannot answer this — the answer is however deep the stack is.
    """
    near = build_graph(entry_fixture, parse_revisions(RANGE), hops=1)
    far = build_graph(entry_fixture, parse_revisions(RANGE), hops=1, to_entry=True)

    assert "controller.OrderController.show" not in {n.display for n in near.nodes}

    reached = {n.display for n in far.nodes}
    assert "controller.OrderController.show" in reached
    assert "job.nightly_sync" in reached, "every trigger, not just the first"


def test_entry_points_are_labelled(entry_fixture):
    """Entry point = nothing in the repo calls it, but it calls something.

    That is what makes it an entry: a router, scheduler or CLI invokes it from outside.
    """
    graph = build_graph(entry_fixture, parse_revisions(RANGE), hops=1, to_entry=True)
    by_label = {n.display: n for n in graph.nodes}

    assert by_label["controller.OrderController.show"].is_entry
    assert by_label["job.nightly_sync"].is_entry
    # The changed helper is called from inside the repo, so it is not an entry.
    assert not by_label["util.fmt"].is_entry


def test_climb_is_bounded(entry_fixture):
    """Climbing must terminate even on a long or cyclic chain."""
    graph = build_graph(entry_fixture, parse_revisions(RANGE), hops=1, to_entry=True)
    assert len(graph.nodes) < 50


# --- imports as a resolution source ---------------------------------------------------


def test_plain_import_disambiguates(import_fixture):
    """`from app.users import render` says which `render` is meant.

    Python's import system is the type system for this purpose. Ignoring it fanned the
    call out across every same-named definition — the commonest false edge in Python,
    and free to fix.
    """
    graph = build_graph(import_fixture, parse_revisions(RANGE), hops=1)
    by_label = {n.display: n for n in graph.nodes}
    caller = by_label["view.show_users"]

    targets = [e for e in graph.edges if e.src == caller.uid]
    assert len(targets) == 1, "must not fan out to the other `render`"

    hit = next(n for n in graph.nodes if n.uid == targets[0].dst)
    assert hit.path == "app/users.py"
    assert targets[0].provenance == "static", "an import is a statement, not a guess"


def test_import_from_a_package_resolves(import_fixture):
    """`from app.pkg import helper` where the definition lives in `app/pkg/__init__.py`."""
    graph = build_graph(import_fixture, parse_revisions(RANGE), hops=1)
    by_label = {n.display: n for n in graph.nodes}
    edges = {(e.src, e.dst) for e in graph.edges}
    assert (by_label["view.show_pkg"].uid, by_label["__init__.helper"].uid) in edges


def test_relative_import_is_left_alone(import_fixture):
    """`from . import x` needs the importing file's package position to resolve.

    Guessing would narrow to the wrong file, which is worse than staying ambiguous.
    """
    graph = build_graph(import_fixture, parse_revisions(RANGE), hops=1)
    by_label = {n.display: n for n in graph.nodes}
    rel = [e for e in graph.edges if e.src == by_label["rel_view.show_rel"].uid]
    assert len(rel) > 1, "unresolved, so still fanned out"
    assert all(e.provenance == "ambiguous" for e in rel)


# --- java ----------------------------------------------------------------------------


def _java_repo(make_repo, tmp_path_factory):
    def svc(name: str, body: str = "return id;") -> str:
        return (
            f"package app;\n\npublic class {name}Service {{\n"
            f"    public String show(String id) {{\n        {body}\n    }}\n}}\n"
        )

    base = {f"app/{n}Service.java": svc(n) for n in ("Order", "User", "Product")}
    base["app/Controller.java"] = (
        "package app;\n\npublic class Controller {\n"
        "    private OrderService orders;\n\n"
        "    public String handle(String id) {\n        return id;\n    }\n}\n"
    )
    head = dict(base)
    head["app/Controller.java"] = (
        "package app;\n\npublic class Controller {\n"
        "    private OrderService orders;\n\n"
        "    public String handle(String id, UserService users) {\n"
        "        users.show(id);\n"  # parameter type
        "        new ProductService().show(id);\n"  # constructed
        "        return orders.show(id);\n"  # field type
        "    }\n}\n"
    )
    return make_repo(tmp_path_factory, "javarepo", base, head)


def test_java_receiver_types_resolve_precisely(tmp_path_factory):
    """Three services each define `show`. Java declares every receiver's type, so none of
    these calls may fan out across the others."""
    from .conftest import _make_repo

    repo = _java_repo(_make_repo, tmp_path_factory)
    graph = build_graph(repo, parse_revisions(RANGE), hops=1)
    handle = "app/Controller.java::Controller.handle"
    targets = {
        e.dst: e.provenance for e in graph.edges if e.src == handle and e.dst.endswith(".show")
    }
    assert targets == {
        "app/UserService.java::UserService.show": "static",
        "app/ProductService.java::ProductService.show": "static",
        "app/OrderService.java::OrderService.show": "static",
    }


def test_java_interface_methods_and_inheritance():
    from scopia.lang.java import JavaAdapter

    facts = JavaAdapter().extract(
        b"interface Pay { void go(); }\n"
        b"abstract class Base implements Pay { abstract void x(); void y() {} }\n"
        b"class Card extends Base { void go() { y(); } void x() {} }\n"
    )
    bodies = {d.qualname: d.has_body for d in facts.defs}
    assert bodies["Pay.go"] is False and bodies["Base.x"] is False and bodies["Card.go"]
    assert {(b.holder, b.base) for b in facts.bases} == {("Base", "Pay"), ("Card", "Base")}


def test_java_constructor_does_not_collide_with_its_class():
    from scopia.lang.java import JavaAdapter

    facts = JavaAdapter().extract(b"class Foo { Foo() {} void a() { new Foo(); } }\n")
    assert {d.qualname for d in facts.defs} == {"Foo", "Foo.<init>", "Foo.a"}
    callees = {(c.callee, c.scope) for c in facts.calls}
    assert ("Foo", "") in callees and ("<init>", "Foo") in callees


def test_java_syntax_errors_are_detected():
    from scopia.lang import parses_cleanly

    assert parses_cleanly("A.java", b"class A { void f() {} }")
    assert not parses_cleanly("A.java", b"class A { void f( { }")


# --- working-tree hashing: these hashes key the fact cache ----------------------------


def test_a_deletion_does_not_poison_other_hashes(tmp_path):
    """`git hash-object` fails on a missing path and used to take the batch with it.

    Every other modified file then fell back to its stale index hash — and those hashes
    key the fact cache, so one deletion served stale analysis for the whole tree.
    """
    import subprocess

    from scopia.vcs.git import Repo

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    for name in ("a", "b"):
        (tmp_path / f"{name}.py").write_text(f"def {name}():\n    return 1\n", "utf-8")
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")

    repo = Repo(tmp_path)
    (tmp_path / "a.py").write_text("def a():\n    return 2\n", "utf-8")
    edited = repo.working_tree_shas()["a.py"]

    (tmp_path / "b.py").unlink()
    after = repo.working_tree_shas()

    assert after["a.py"] == edited, "an unrelated deletion must not change a.py's hash"
    assert "b.py" not in after, "a deleted file must be absent, not stale"


# --- containers only earn a node when nothing inside them is drawn --------------------


def _tiny_repo(tmp_path, base, head):
    import subprocess

    from scopia.vcs.git import Repo

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    for path, text in base.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, "utf-8")
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    for path, text in head.items():
        (tmp_path / path).write_text(text, "utf-8")
    graph = build_graph(Repo(tmp_path), parse_revisions(None), max_nodes=500)
    return sorted(n.display for n in graph.touched)


def test_class_node_drops_when_its_method_is_shown(tmp_path):
    """A new file produced three nodes — module, class, method — for one thing."""
    shown = _tiny_repo(
        tmp_path,
        {"s.py": "class S:\n    def go(self):\n        return 1\n"},
        {"s.py": "class S:\n    def go(self):\n        return 2\n"},
    )
    assert shown == ["s.S.go"]


def test_class_node_survives_when_only_the_declaration_changed(tmp_path):
    """Nothing inside is drawn, so the class is the only thing left to report."""
    shown = _tiny_repo(
        tmp_path,
        {"s.py": "class S:\n    def go(self):\n        return 1\n"},
        {"s.py": "class S(object):\n    def go(self):\n        return 1\n"},
    )
    assert shown == ["s.S"]


def test_module_node_survives_for_a_file_with_no_symbols(tmp_path):
    """A config constant belongs to no function; dropping it loses the whole file."""
    shown = _tiny_repo(tmp_path, {"cfg.py": 'V = "1"\n'}, {"cfg.py": 'V = "2"\n'})
    assert shown == ["cfg.py"]
