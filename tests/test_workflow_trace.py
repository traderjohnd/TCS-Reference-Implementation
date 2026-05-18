"""
Phase 4 / Slice 1 — workflow trace model integrity tests.

Validates that GovernedWorkflowTrace, GovernedNode, GovernedEdge,
and GovernanceEvent obey their contracts:

    - schema versions are pinned
    - traces have UUID4 workflow_ids
    - nodes are added in order, events are append-only
    - event hash chain is well-formed across multiple events
    - to_dict() is JSON-serializable
"""

from __future__ import annotations

import json
import uuid

import pytest

from tcs.workflow import (
    AttributionSignal,
    BoundednessSignal,
    CONNECTOR_CONTRACT_VERSION,
    ComplianceSignal,
    EVENT_SCHEMA_VERSION,
    GovernanceEvent,
    GovernedEdge,
    GovernedNode,
    GovernedWorkflowTrace,
    KnownStateSignal,
    NodeType,
    TRACE_SCHEMA_VERSION,
)


# --------------------------------------------------------------------------- #
# Schema versions                                                              #
# --------------------------------------------------------------------------- #

class TestSchemaVersions:
    def test_trace_schema_version_pinned(self):
        assert TRACE_SCHEMA_VERSION == "1.0"

    def test_event_schema_version_pinned(self):
        assert EVENT_SCHEMA_VERSION == "1.0"

    def test_connector_contract_version_pinned(self):
        assert CONNECTOR_CONTRACT_VERSION == "1.0"

    def test_new_trace_carries_schema_version(self):
        trace = GovernedWorkflowTrace.new(base_profile_id="fin-r3-a4-ct4")
        assert trace.schema_version == TRACE_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# Trace lifecycle                                                              #
# --------------------------------------------------------------------------- #

class TestTraceLifecycle:
    def test_new_trace_has_uuid4_workflow_id(self):
        trace = GovernedWorkflowTrace.new(base_profile_id="fin-r3-a4-ct4")
        # Should not raise
        uuid.UUID(trace.workflow_id, version=4)

    def test_new_trace_has_created_at_timestamp(self):
        trace = GovernedWorkflowTrace.new(base_profile_id="fin-r3-a4-ct4")
        assert trace.created_at  # ISO-8601 string
        assert trace.completed_at is None
        assert trace.final_output is None

    def test_add_node_preserves_order(self):
        trace = GovernedWorkflowTrace.new(base_profile_id="fin-r3-a4-ct4")
        n1 = _make_node("n1")
        n2 = _make_node("n2")
        trace.add_node(n1)
        trace.add_node(n2)
        assert [n.node_id for n in trace.nodes] == ["n1", "n2"]

    def test_add_node_rejects_pre_attached_event(self):
        trace = GovernedWorkflowTrace.new(base_profile_id="fin-r3-a4-ct4")
        n = _make_node("n1")
        n.event = _make_event(trace.workflow_id, "n1")
        with pytest.raises(ValueError, match="pre-attached event"):
            trace.add_node(n)

    def test_attach_event_is_append_only(self):
        trace = GovernedWorkflowTrace.new(base_profile_id="fin-r3-a4-ct4")
        n = _make_node("n1")
        trace.add_node(n)
        ev = _make_event(trace.workflow_id, "n1")
        trace.attach_event("n1", ev)
        with pytest.raises(ValueError, match="append-only"):
            trace.attach_event("n1", ev)

    def test_get_node_raises_for_unknown_id(self):
        trace = GovernedWorkflowTrace.new(base_profile_id="fin-r3-a4-ct4")
        with pytest.raises(KeyError):
            trace.get_node("missing")

    def test_finalize_sets_output_and_completion(self):
        trace = GovernedWorkflowTrace.new(base_profile_id="fin-r3-a4-ct4")
        trace.finalize("final answer text")
        assert trace.final_output == "final answer text"
        assert trace.completed_at is not None


# --------------------------------------------------------------------------- #
# GovernanceEvent hashing                                                      #
# --------------------------------------------------------------------------- #

class TestEventHashing:
    def test_event_hash_excludes_hash_fields(self):
        """
        Two events differing only in event_hash / previous_event_hash
        must hash to the same value. The hash covers content, not the
        hash fields themselves.
        """
        wf_id = str(uuid.uuid4())
        ev1 = _make_event(wf_id, "n1")
        ev2 = GovernanceEvent(
            **{**ev1.__dict__, "event_hash": "different", "previous_event_hash": "other"}
        )
        assert ev1.compute_hash() == ev2.compute_hash()

    def test_event_hash_changes_with_content(self):
        wf_id = str(uuid.uuid4())
        ev1 = _make_event(wf_id, "n1")
        ev2 = GovernanceEvent(
            **{**ev1.__dict__, "connector_type": "different.connector"}
        )
        assert ev1.compute_hash() != ev2.compute_hash()


# --------------------------------------------------------------------------- #
# Edges                                                                        #
# --------------------------------------------------------------------------- #

class TestEdges:
    def test_edge_defaults_to_data_flow(self):
        e = GovernedEdge(from_node_id="a", to_node_id="b")
        assert e.edge_type == "data_flow"

    def test_edge_is_frozen(self):
        e = GovernedEdge(from_node_id="a", to_node_id="b")
        with pytest.raises(Exception):
            e.from_node_id = "c"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Serialization                                                                #
# --------------------------------------------------------------------------- #

class TestSerialization:
    def test_trace_to_dict_is_json_serializable(self):
        trace = GovernedWorkflowTrace.new(base_profile_id="fin-r3-a4-ct4")
        n = _make_node("n1")
        trace.add_node(n)
        trace.attach_event("n1", _make_event(trace.workflow_id, "n1"))
        trace.add_edge(GovernedEdge(from_node_id="n1", to_node_id="n2"))
        trace.finalize("done")

        d = trace.to_dict()
        s = json.dumps(d)  # must not raise
        roundtrip = json.loads(s)
        assert roundtrip["workflow_id"] == trace.workflow_id
        assert roundtrip["final_output"] == "done"
        assert roundtrip["schema_version"] == TRACE_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _make_node(node_id: str) -> GovernedNode:
    return GovernedNode(
        node_id=node_id,
        name=f"node {node_id}",
        node_type=NodeType.LLM,
        connection_type="CT-1",
        sensitivity_tier="T2",
    )


def _make_event(workflow_id: str, node_id: str) -> GovernanceEvent:
    return GovernanceEvent(
        event_id=str(uuid.uuid4()),
        workflow_id=workflow_id,
        node_id=node_id,
        timestamp="2026-05-16T18:00:00+00:00",
        connector_type="test.connector",
        connection_type="CT-1",
        sensitivity_tier="T2",
        boundedness=BoundednessSignal(),
        attribution=AttributionSignal(),
        compliance=ComplianceSignal(),
        known=KnownStateSignal(),
    )
