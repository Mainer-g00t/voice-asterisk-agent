"""
Tests for packages/voiceai_common/flow_engine.py

The engine is pure Python with no I/O — every function is deterministic
given its inputs, making this straightforward to test exhaustively.
"""

import pytest
from flow_engine import (
    apply_event,
    find_matching_edge,
    get_entry_node,
    get_edges_from,
    get_node,
    validate_flow,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_flow(*nodes, edges=None, entry=None):
    """Build a minimal flow definition for testing."""
    return {
        "entry_node_id": entry or (nodes[0]["id"] if nodes else None),
        "nodes": list(nodes),
        "edges": edges or [],
    }


def node(id, type="conversation", **config):
    return {"id": id, "type": type, "label": id, "config": config}


def edge(id, source, target, condition):
    return {"id": id, "source": source, "target": target, "condition": condition}


# ── get_node / get_edges_from / get_entry_node ────────────────────────────────

class TestHelpers:
    def test_get_node_found(self):
        flow = make_flow(node("n1"), node("n2"))
        assert get_node(flow, "n1")["id"] == "n1"

    def test_get_node_missing(self):
        flow = make_flow(node("n1"))
        assert get_node(flow, "nope") is None

    def test_get_edges_from(self):
        e1 = edge("e1", "n1", "n2", {"type": "default"})
        e2 = edge("e2", "n1", "n3", {"type": "keyword_matched", "words": ["bye"]})
        e3 = edge("e3", "n2", "n3", {"type": "default"})
        flow = make_flow(node("n1"), node("n2"), node("n3"), edges=[e1, e2, e3])
        result = get_edges_from(flow, "n1")
        assert len(result) == 2
        assert {e["id"] for e in result} == {"e1", "e2"}

    def test_get_entry_node_explicit(self):
        flow = make_flow(node("n1"), node("n2"), entry="n2")
        assert get_entry_node(flow)["id"] == "n2"

    def test_get_entry_node_fallback_to_first(self):
        flow = {"nodes": [node("n1"), node("n2")], "edges": []}
        assert get_entry_node(flow)["id"] == "n1"

    def test_get_entry_node_empty(self):
        flow = {"nodes": [], "edges": []}
        assert get_entry_node(flow) is None


# ── Edge condition evaluation ─────────────────────────────────────────────────

class TestConditions:

    def _match(self, condition, state=None, event=None):
        """Helper: build a minimal flow and call find_matching_edge."""
        flow = make_flow(
            node("n1"), node("n2"),
            edges=[edge("e1", "n1", "n2", condition)]
        )
        result = find_matching_edge(flow, "n1", event or {}, state or {})
        return result is not None

    # default
    def test_default_always_matches(self):
        assert self._match({"type": "default"})

    def test_default_matches_with_empty_event(self):
        assert self._match({"type": "default"}, event={})

    # keyword_matched
    def test_keyword_matched_hit(self):
        assert self._match(
            {"type": "keyword_matched", "words": ["bye", "goodbye"]},
            event={"type": "transcription", "text": "Ok, goodbye then"}
        )

    def test_keyword_matched_case_insensitive(self):
        assert self._match(
            {"type": "keyword_matched", "words": ["BYE"]},
            event={"type": "transcription", "text": "bye"}
        )

    def test_keyword_matched_substring(self):
        assert self._match(
            {"type": "keyword_matched", "words": ["cancel"]},
            event={"type": "transcription", "text": "I want to cancel my order"}
        )

    def test_keyword_matched_miss(self):
        assert not self._match(
            {"type": "keyword_matched", "words": ["bye"]},
            event={"type": "transcription", "text": "Hello there"}
        )

    def test_keyword_matched_falls_back_to_last_transcript(self):
        assert self._match(
            {"type": "keyword_matched", "words": ["stop"]},
            state={"last_transcript": "please stop"},
            event={"type": "turn_end"}  # no text in event
        )

    # turn_count_gte
    def test_turn_count_gte_exact(self):
        assert self._match(
            {"type": "turn_count_gte", "n": 5},
            state={"turn_count": 5},
            event={"type": "turn_end"}
        )

    def test_turn_count_gte_above(self):
        assert self._match(
            {"type": "turn_count_gte", "n": 3},
            state={"turn_count": 7},
            event={"type": "turn_end"}
        )

    def test_turn_count_gte_below(self):
        assert not self._match(
            {"type": "turn_count_gte", "n": 5},
            state={"turn_count": 4},
            event={"type": "turn_end"}
        )

    # dtmf_digit
    def test_dtmf_digit_match(self):
        assert self._match(
            {"type": "dtmf_digit", "digit": "1"},
            event={"type": "dtmf", "digit": "1"}
        )

    def test_dtmf_digit_no_match(self):
        assert not self._match(
            {"type": "dtmf_digit", "digit": "1"},
            event={"type": "dtmf", "digit": "2"}
        )

    def test_dtmf_digit_falls_back_to_state(self):
        assert self._match(
            {"type": "dtmf_digit", "digit": "3"},
            state={"last_dtmf": "3"},
            event={"type": "turn_end"}
        )

    # silence_timeout
    def test_silence_timeout_match(self):
        assert self._match(
            {"type": "silence_timeout"},
            event={"type": "silence_timeout"}
        )

    def test_silence_timeout_no_match_on_other_event(self):
        assert not self._match(
            {"type": "silence_timeout"},
            event={"type": "transcription", "text": "hello"}
        )

    # tool_result
    def test_tool_result_field_match(self):
        assert self._match(
            {"type": "tool_result", "tool": "check_crm", "field": "eligible", "value": "true"},
            state={"last_tool_results": {"check_crm": {"eligible": "true"}}}
        )

    def test_tool_result_field_no_match(self):
        assert not self._match(
            {"type": "tool_result", "tool": "check_crm", "field": "eligible", "value": "true"},
            state={"last_tool_results": {"check_crm": {"eligible": "false"}}}
        )

    def test_tool_result_missing_tool(self):
        assert not self._match(
            {"type": "tool_result", "tool": "missing_tool", "field": "x", "value": "y"},
            state={"last_tool_results": {}}
        )

    # variable_equals — built-in keys
    def test_variable_equals_builtin_last_dtmf(self):
        assert self._match(
            {"type": "variable_equals", "var": "last_dtmf", "value": "2"},
            state={"last_dtmf": "2"}
        )

    def test_variable_equals_builtin_turn_count(self):
        assert self._match(
            {"type": "variable_equals", "var": "turn_count", "value": "3"},
            state={"turn_count": 3}
        )

    def test_variable_equals_user_defined(self):
        assert self._match(
            {"type": "variable_equals", "var": "plan", "value": "premium"},
            state={"variables": {"plan": "premium"}}
        )

    def test_variable_equals_user_defined_overrides_builtin(self):
        # user-defined "turn_count" shadows the built-in
        assert self._match(
            {"type": "variable_equals", "var": "turn_count", "value": "custom"},
            state={"turn_count": 99, "variables": {"turn_count": "custom"}}
        )

    def test_variable_equals_miss(self):
        assert not self._match(
            {"type": "variable_equals", "var": "plan", "value": "premium"},
            state={"variables": {"plan": "basic"}}
        )

    # webhook_field
    def test_webhook_field_match(self):
        assert self._match(
            {"type": "webhook_field", "field": "action", "value": "transfer"},
            state={"last_webhook_result": {"action": "transfer", "score": 90}}
        )

    def test_webhook_field_no_match(self):
        assert not self._match(
            {"type": "webhook_field", "field": "action", "value": "transfer"},
            state={"last_webhook_result": {"action": "continue"}}
        )

    # call_no_answer
    def test_call_no_answer_match(self):
        assert self._match(
            {"type": "call_no_answer"},
            state={"call_status": "no_answer"}
        )

    def test_call_no_answer_no_match(self):
        assert not self._match(
            {"type": "call_no_answer"},
            state={"call_status": "answered"}
        )

    # unknown type
    def test_unknown_condition_type_never_matches(self):
        assert not self._match({"type": "invented_type_xyz"})


# ── Edge priority: non-default evaluated before default ───────────────────────

class TestEdgePriority:

    def test_non_default_wins_over_default(self):
        flow = make_flow(
            node("n1"), node("n2"), node("n3"),
            edges=[
                edge("e_default", "n1", "n2", {"type": "default"}),
                edge("e_kw", "n1", "n3", {"type": "keyword_matched", "words": ["bye"]}),
            ]
        )
        result = find_matching_edge(
            flow, "n1",
            event={"type": "transcription", "text": "bye"},
            state={}
        )
        assert result["id"] == "e_kw"

    def test_default_fires_when_no_other_matches(self):
        flow = make_flow(
            node("n1"), node("n2"), node("n3"),
            edges=[
                edge("e_kw", "n1", "n3", {"type": "keyword_matched", "words": ["bye"]}),
                edge("e_default", "n1", "n2", {"type": "default"}),
            ]
        )
        result = find_matching_edge(
            flow, "n1",
            event={"type": "transcription", "text": "hello"},
            state={}
        )
        assert result["id"] == "e_default"

    def test_first_matching_non_default_wins(self):
        """When two non-default edges both match, the first in definition order wins."""
        flow = make_flow(
            node("n1"), node("n2"), node("n3"),
            edges=[
                edge("e1", "n1", "n2", {"type": "keyword_matched", "words": ["bye"]}),
                edge("e2", "n1", "n3", {"type": "keyword_matched", "words": ["bye"]}),
            ]
        )
        result = find_matching_edge(
            flow, "n1",
            event={"type": "transcription", "text": "bye"},
            state={}
        )
        assert result["id"] == "e1"

    def test_no_matching_edge_returns_none(self):
        flow = make_flow(
            node("n1"), node("n2"),
            edges=[edge("e1", "n1", "n2", {"type": "keyword_matched", "words": ["bye"]})]
        )
        result = find_matching_edge(flow, "n1", event={}, state={})
        assert result is None


# ── apply_event: state updates ────────────────────────────────────────────────

class TestApplyEventStateUpdates:

    def _apply(self, event, state=None, flow=None):
        if flow is None:
            flow = make_flow(node("n1"), node("n2"))
        return apply_event(flow, "n1", state or {}, event)

    def test_transcription_updates_last_transcript(self):
        _, new_state, _ = self._apply({"type": "transcription", "text": "hello world"})
        assert new_state["last_transcript"] == "hello world"

    def test_dtmf_updates_last_dtmf(self):
        _, new_state, _ = self._apply({"type": "dtmf", "digit": "5"})
        assert new_state["last_dtmf"] == "5"

    def test_turn_end_increments_turn_count(self):
        _, new_state, _ = self._apply({"type": "turn_end"}, state={"turn_count": 2})
        assert new_state["turn_count"] == 3

    def test_turn_end_initializes_turn_count(self):
        _, new_state, _ = self._apply({"type": "turn_end"}, state={})
        assert new_state["turn_count"] == 1

    def test_tool_result_updates_last_tool_results(self):
        _, new_state, _ = self._apply(
            {"type": "tool_result", "tool": "get_weather", "result": {"temp": "22C"}}
        )
        assert new_state["last_tool_results"] == {"get_weather": {"temp": "22C"}}

    def test_tool_result_merges_existing(self):
        _, new_state, _ = self._apply(
            {"type": "tool_result", "tool": "b", "result": {"x": 1}},
            state={"last_tool_results": {"a": {"y": 2}}}
        )
        assert "a" in new_state["last_tool_results"]
        assert "b" in new_state["last_tool_results"]

    def test_intent_updates_last_intent(self):
        _, new_state, _ = self._apply({"type": "intent", "intent": "transfer_to_human"})
        assert new_state["last_intent"] == "transfer_to_human"

    def test_webhook_result_updates_state(self):
        _, new_state, _ = self._apply(
            {"type": "webhook_result", "result": {"action": "qualify"}}
        )
        assert new_state["last_webhook_result"] == {"action": "qualify"}

    def test_set_variable_stores_in_variables(self):
        _, new_state, _ = self._apply(
            {"type": "set_variable", "var": "plan", "value": "premium"}
        )
        assert new_state["variables"]["plan"] == "premium"

    def test_state_is_not_mutated(self):
        """apply_event must return a new state dict, never mutate the input."""
        original = {"turn_count": 0, "variables": {}}
        apply_event(make_flow(node("n1")), "n1", original, {"type": "turn_end"})
        assert original["turn_count"] == 0


# ── apply_event: transitions ──────────────────────────────────────────────────

class TestApplyEventTransitions:

    def test_fires_transition_when_edge_matches(self):
        flow = make_flow(
            node("n1"), node("n2"),
            edges=[edge("e1", "n1", "n2", {"type": "keyword_matched", "words": ["bye"]})]
        )
        new_node, _, taken_edge = apply_event(
            flow, "n1", {},
            {"type": "transcription", "text": "bye"}
        )
        assert new_node == "n2"
        assert taken_edge["id"] == "e1"

    def test_no_transition_when_no_edge_matches(self):
        flow = make_flow(
            node("n1"), node("n2"),
            edges=[edge("e1", "n1", "n2", {"type": "keyword_matched", "words": ["bye"]})]
        )
        new_node, _, taken_edge = apply_event(
            flow, "n1", {},
            {"type": "transcription", "text": "hello"}
        )
        assert new_node == "n1"
        assert taken_edge is None

    def test_state_updated_before_edge_evaluation(self):
        """turn_count must be incremented before evaluating turn_count_gte."""
        flow = make_flow(
            node("n1"), node("n2"),
            edges=[edge("e1", "n1", "n2", {"type": "turn_count_gte", "n": 1})]
        )
        # turn_count starts at 0; turn_end should bump to 1, then edge fires
        new_node, new_state, taken_edge = apply_event(
            flow, "n1", {"turn_count": 0},
            {"type": "turn_end"}
        )
        assert new_node == "n2"
        assert new_state["turn_count"] == 1
        assert taken_edge is not None


# ── validate_flow ─────────────────────────────────────────────────────────────

class TestValidateFlow:

    def test_valid_flow_no_errors(self):
        flow = make_flow(
            node("n1"), node("n2"),
            edges=[edge("e1", "n1", "n2", {"type": "default"})]
        )
        assert validate_flow(flow) == []

    def test_empty_flow_reports_error(self):
        errors = validate_flow({"nodes": [], "edges": []})
        assert any("no nodes" in e.lower() for e in errors)

    def test_bad_entry_node_id(self):
        flow = {
            "entry_node_id": "nonexistent",
            "nodes": [node("n1")],
            "edges": []
        }
        errors = validate_flow(flow)
        assert any("entry_node_id" in e for e in errors)

    def test_edge_with_bad_source(self):
        flow = make_flow(
            node("n1"), node("n2"),
            edges=[edge("e1", "bad_source", "n2", {"type": "default"})]
        )
        errors = validate_flow(flow)
        assert any("source" in e for e in errors)

    def test_edge_with_bad_target(self):
        flow = make_flow(
            node("n1"), node("n2"),
            edges=[edge("e1", "n1", "bad_target", {"type": "default"})]
        )
        errors = validate_flow(flow)
        assert any("target" in e for e in errors)

    def test_unknown_node_type(self):
        flow = make_flow({"id": "n1", "type": "invented_type", "label": "x", "config": {}})
        errors = validate_flow(flow)
        assert any("unknown type" in e for e in errors)

    def test_unknown_condition_type(self):
        flow = make_flow(
            node("n1"), node("n2"),
            edges=[edge("e1", "n1", "n2", {"type": "invented_condition"})]
        )
        errors = validate_flow(flow)
        assert any("unknown condition" in e for e in errors)
