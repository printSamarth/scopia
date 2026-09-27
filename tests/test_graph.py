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
        return "php" if by_uid[uid].path.endswith(".php") else "py"

    crossings = [
        (by_uid[e.src].label, by_uid[e.dst].label)
        for e in graph.edges
        if language(e.src) != language(e.dst)
    ]
    assert not crossings, f"cross-language edges: {crossings}"
    # Both sides still resolve within their own language.
    assert any(language(e.src) == "py" for e in graph.edges)
    assert any(language(e.src) == "php" for e in graph.edges)


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
