import pytest

from loopgraph_supervisor.loopspec import LoopNode, LoopSpec, default_coding_spec
from loopgraph_supervisor.loopspec_interpreter import LoopSpecInterpreter
from loopgraph_supervisor.spec_store import LoopSpecStore
from loopgraph_supervisor.store import SQLiteStore


def test_default_coding_spec_routes_the_current_graph():
    spec = default_coding_spec()

    assert spec.next_node("execute", "pass") == "verify"
    assert spec.next_node("verify", "fail") == "execute"
    assert spec.next_node("verify", "approve") == "hitl"
    assert spec.next_node("hitl", "approve") == "promote"
    assert spec.next_node("promote", "pass") == "complete"

    interpreted = LoopSpecInterpreter(spec).transition("verify", "approve", 1)
    assert interpreted.target == "hitl"
    assert interpreted.spec_hash == spec.content_hash()


def test_loopspec_interpreter_enforces_iteration_guard():
    spec = default_coding_spec()
    with pytest.raises(RuntimeError, match="iteration limit"):
        LoopSpecInterpreter(spec).transition("verify", "retry", spec.max_iterations)


def test_loopspec_is_canonical_and_rejects_ambiguous_graphs():
    spec = default_coding_spec()
    assert len(spec.content_hash()) == 64
    assert spec.content_hash() == LoopSpec(**spec.__dict__).content_hash()

    with pytest.raises(ValueError, match="unique"):
        LoopSpec("bad", 1, "a", (LoopNode("a", "terminal"), LoopNode("a", "terminal")), (), 1)


def test_loopspec_revision_round_trips_through_registry():
    store = SQLiteStore(":memory:")
    registry = LoopSpecStore(store)
    spec = default_coding_spec()
    registry.save(spec, status="ACTIVE")

    restored = registry.active(spec.spec_id)

    assert restored is not None
    assert restored.document() == spec.document()
    assert restored.content_hash() == spec.content_hash()


def test_loopspec_revisions_are_immutable_and_only_one_is_active():
    store = SQLiteStore(":memory:")
    registry = LoopSpecStore(store)
    first = default_coding_spec()
    registry.save(first, status="ACTIVE")
    second = LoopSpec(
        spec_id=first.spec_id,
        revision=2,
        predecessor_hash=first.content_hash(),
        entrypoint=first.entrypoint,
        max_iterations=4,
        nodes=first.nodes,
        edges=first.edges,
    )
    registry.save(second, status="CANDIDATE")

    active = registry.active(first.spec_id)
    assert active is not None
    assert active.revision == 1
    with pytest.raises(ValueError, match="immutable"):
        registry.save(LoopSpec(first.spec_id, 2, first.entrypoint, first.nodes, first.edges, 5, first.content_hash()), status="ACTIVE")


def test_registry_cannot_activate_v2_without_human_activation_path():
    store = SQLiteStore(":memory:")
    registry = LoopSpecStore(store)
    baseline = default_coding_spec()
    registry.save(baseline, status="ACTIVE")
    candidate = LoopSpec(
        spec_id=baseline.spec_id,
        revision=2,
        predecessor_hash=baseline.content_hash(),
        entrypoint=baseline.entrypoint,
        max_iterations=baseline.max_iterations,
        nodes=baseline.nodes,
        edges=baseline.edges,
    )
    registry.save(candidate, status="CANDIDATE")
    with pytest.raises(ValueError, match="human activation"):
        registry.save(candidate, status="ACTIVE")
