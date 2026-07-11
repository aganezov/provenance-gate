"""core.walk.closure — the pure BFS the seeded readers share. No DB; ``expand`` is a plain dict."""

from provenance_gate.core.walk import closure


def _expand(adj):
    """A one-hop expander over adjacency {id: [neighbours]} — stands in for a reader query."""
    return lambda frontier: {n for v in frontier for n in adj.get(v, ())}


def test_closure_walks_transitively_to_fixpoint():
    # a -> b -> c -> d ; seed a, reach all
    adj = {"a": ["b"], "b": ["c"], "c": ["d"]}
    assert closure(["a"], _expand(adj)) == {"a", "b", "c", "d"}


def test_closure_includes_seeds_and_merges_diamond():
    # a -> {b, c} -> d : seed a reaches the shared descendant once
    adj = {"a": ["b", "c"], "b": ["d"], "c": ["d"]}
    assert closure(["a"], _expand(adj)) == {"a", "b", "c", "d"}


def test_closure_terminates_on_a_cycle():
    # a <-> b cycle must not loop forever
    adj = {"a": ["b"], "b": ["a"]}
    assert closure(["a"], _expand(adj)) == {"a", "b"}


def test_closure_depth_bounds_the_walk():
    adj = {"a": ["b"], "b": ["c"], "c": ["d"]}
    assert closure(["a"], _expand(adj), max_depth=0) == {"a"}            # seeds only
    assert closure(["a"], _expand(adj), max_depth=1) == {"a", "b"}       # one hop
    assert closure(["a"], _expand(adj), max_depth=2) == {"a", "b", "c"}


def test_closure_multi_seed_union():
    adj = {"a": ["x"], "b": ["y"]}
    assert closure(["a", "b"], _expand(adj)) == {"a", "b", "x", "y"}
