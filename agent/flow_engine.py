"""
Stateless call flow execution engine.

Takes a flow definition (from the DB / Redis) plus a current state dict and an
incoming event, and returns whether a transition fired and what the next node is.

Designed to run inside the agent container (local evaluation, no DB round-trip
per event) as well as to be used by config-api for validation and analytics.

Flow definition shape
─────────────────────
{
  "entry_node_id": "n1",
  "nodes": [
    {
      "id": "n1",
      "type": "conversation",      # see NODE_TYPES
      "label": "Intro",
      "config": {
        "system_prompt": "...",    # conversation node only
        "greeting": "...",         # conversation node only (overrides agent greeting)
        "keywords": ["bye"],       # shorthand: adds keyword_matched edge inline
        "max_turns": 10,           # shorthand: adds turn_count_gte edge inline
        "silence_timeout": 30,     # seconds; 0 = disabled
        "message": "...",          # say node
        "dtmf_timeout": 10,        # gather_dtmf node
        "destination": "...",      # transfer node
        "url": "...",              # webhook node
        "variable": "...",         # set_variable / condition node
        "value": "..."             # set_variable: value to set
      }
    }
  ],
  "edges": [
    {
      "id": "e1",
      "source": "n1",
      "target": "n2",
      "condition": {
        "type": "keyword_matched",   # see CONDITION_TYPES
        "words": ["cancel", "bye"]
      }
    }
  ]
}

Condition types
───────────────
  default          – always matches (use as last edge / fallback)
  keyword_matched  – any of {"words": [...]} appear in STT transcript
  turn_count_gte   – state["turn_count"] >= {"n": N}
  dtmf_digit       – state["last_dtmf"] == {"digit": "X"}
  silence_timeout  – not checked here (handled by transport timeout)
  tool_result      – state["last_tool_results"][tool] field matches value
                     {"tool": "...", "field": "...", "value": "..."}
  intent_is        – state["last_intent"] == {"intent": "..."}
  variable_equals  – state["variables"][var] or any built-in state key (last_dtmf, last_transcript, turn_count, last_webhook_result, …)
  call_no_answer   – state["call_status"] == "no_answer" (set by originate API)
  webhook_field    – state["last_webhook_result"][field] == value
                     {"field": "...", "value": "..."}
"""

from __future__ import annotations

from typing import Any


# ── Node type registry (informational) ───────────────────────────────────────

NODE_TYPES = {
    "conversation",   # AI agent: STT → LLM → TTS loop
    "say",            # one-shot TTS message, then default edge
    "gather_dtmf",    # wait for keypress, branch on digit
    "transfer",       # Asterisk AMI redirect
    "webhook",        # HTTP POST with current state, branch on response
    "set_variable",   # write a value into state["variables"]
    "condition",      # branch on a variable value (no audio)
    "end",            # hang up
}

CONDITION_TYPES = {
    "default",
    "keyword_matched",
    "turn_count_gte",
    "dtmf_digit",
    "silence_timeout",
    "tool_result",
    "intent_is",
    "variable_equals",
    "call_no_answer",
    "webhook_field",
}


# ── Node helpers ──────────────────────────────────────────────────────────────

def get_node(flow_def: dict, node_id: str) -> dict | None:
    """Return the node dict with the given id, or None."""
    for node in flow_def.get("nodes", []):
        if node["id"] == node_id:
            return node
    return None


def get_edges_from(flow_def: dict, node_id: str) -> list[dict]:
    """Return all edges whose source is node_id, preserving definition order."""
    return [e for e in flow_def.get("edges", []) if e.get("source") == node_id]


def get_entry_node(flow_def: dict) -> dict | None:
    entry_id = flow_def.get("entry_node_id")
    if not entry_id:
        nodes = flow_def.get("nodes", [])
        return nodes[0] if nodes else None
    return get_node(flow_def, entry_id)


# ── Condition evaluators ──────────────────────────────────────────────────────

def _evaluate_condition(condition: dict, state: dict, event: dict) -> bool:
    """
    Return True if the edge condition matches the current event + state.
    `event` is {"type": <event_type>, **kwargs} e.g.:
      {"type": "transcription", "text": "I want to cancel"}
      {"type": "dtmf", "digit": "1"}
      {"type": "turn_end", "turn_count": 3}
      {"type": "tool_result", "tool": "check_crm", "result": {"eligible": "true"}}
      {"type": "intent", "intent": "transfer_to_human"}
    """
    ctype = condition.get("type", "default")

    if ctype == "default":
        return True

    if ctype == "keyword_matched":
        text = (event.get("text") or state.get("last_transcript", "")).lower()
        return any(w.lower() in text for w in condition.get("words", []))

    if ctype == "turn_count_gte":
        n = int(condition.get("n", 1))
        return int(state.get("turn_count", 0)) >= n

    if ctype == "dtmf_digit":
        digit = event.get("digit") or state.get("last_dtmf", "")
        return digit == str(condition.get("digit", ""))

    if ctype == "silence_timeout":
        # Silence timeouts are driven by the transport; event type must match.
        return event.get("type") == "silence_timeout"

    if ctype == "tool_result":
        tool = condition.get("tool")
        field = condition.get("field")
        expected = str(condition.get("value", ""))
        results = state.get("last_tool_results", {})
        result = results.get(tool, {})
        if field:
            actual = str(result.get(field, ""))
        else:
            actual = str(result)
        return actual == expected

    if ctype == "intent_is":
        return state.get("last_intent", "") == condition.get("intent", "")

    if ctype == "variable_equals":
        var = condition.get("var", "")
        expected = str(condition.get("value", ""))
        # Check user-defined variables first, then fall back to built-in state keys
        # (last_dtmf, last_transcript, turn_count, last_webhook_result, etc.)
        merged = {**state, **state.get("variables", {})}
        actual = str(merged.get(var, ""))
        return actual == expected

    if ctype == "call_no_answer":
        return state.get("call_status") == "no_answer"

    if ctype == "webhook_field":
        field = condition.get("field", "")
        expected = str(condition.get("value", ""))
        result = state.get("last_webhook_result", {})
        return str(result.get(field, "")) == expected

    return False


# ── Transition logic ──────────────────────────────────────────────────────────

def find_matching_edge(
    flow_def: dict,
    current_node_id: str,
    event: dict,
    state: dict,
) -> dict | None:
    """
    Evaluate outgoing edges from current_node_id in order.
    Return the first edge whose condition matches, or None.

    Non-default conditions are evaluated before default edges (which always match).
    The definition order of edges is preserved within each group.
    """
    edges = get_edges_from(flow_def, current_node_id)
    # Evaluate non-default conditions first, then fall through to default.
    non_defaults = [e for e in edges if e.get("condition", {}).get("type") != "default"]
    defaults = [e for e in edges if e.get("condition", {}).get("type") == "default"]

    for edge in non_defaults + defaults:
        if _evaluate_condition(edge.get("condition", {}), state, event):
            return edge
    return None


def apply_event(
    flow_def: dict,
    current_node_id: str,
    state: dict,
    event: dict,
) -> tuple[str, dict, dict | None]:
    """
    Process an event against the current node's outgoing edges.

    Returns:
      (new_node_id, new_state, edge_taken)
      - If a transition fired: new_node_id is the target, edge_taken is the edge.
      - If no transition: new_node_id == current_node_id, edge_taken is None.

    State updates applied here:
      - "last_transcript" updated on transcription events
      - "last_dtmf" updated on dtmf events
      - "turn_count" incremented on turn_end events
      - "last_tool_results[tool]" updated on tool_result events
      - "last_intent" updated on intent events
    """
    new_state = dict(state)

    # ── Update state from the event ──────────────────────────────────────────
    etype = event.get("type", "")

    if etype == "transcription":
        new_state["last_transcript"] = event.get("text", "")

    elif etype == "dtmf":
        new_state["last_dtmf"] = event.get("digit", "")

    elif etype == "turn_end":
        new_state["turn_count"] = int(new_state.get("turn_count", 0)) + 1

    elif etype == "tool_result":
        tool = event.get("tool", "")
        result = event.get("result", {})
        results = dict(new_state.get("last_tool_results", {}))
        results[tool] = result
        new_state["last_tool_results"] = results

    elif etype == "intent":
        new_state["last_intent"] = event.get("intent", "")

    elif etype == "webhook_result":
        new_state["last_webhook_result"] = event.get("result", {})

    elif etype == "set_variable":
        variables = dict(new_state.get("variables", {}))
        variables[event.get("var", "")] = event.get("value", "")
        new_state["variables"] = variables

    # ── Find matching edge ───────────────────────────────────────────────────
    edge = find_matching_edge(flow_def, current_node_id, event, new_state)
    if edge:
        return edge["target"], new_state, edge

    return current_node_id, new_state, None


# ── Validation ────────────────────────────────────────────────────────────────

def validate_flow(flow_def: dict) -> list[str]:
    """
    Basic structural validation. Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []
    nodes = flow_def.get("nodes", [])
    edges = flow_def.get("edges", [])
    node_ids = {n["id"] for n in nodes}

    if not nodes:
        errors.append("Flow has no nodes")

    entry = flow_def.get("entry_node_id")
    if entry and entry not in node_ids:
        errors.append(f"entry_node_id '{entry}' not found in nodes")

    for edge in edges:
        if edge.get("source") not in node_ids:
            errors.append(f"Edge '{edge.get('id')}' source '{edge.get('source')}' not found")
        if edge.get("target") not in node_ids:
            errors.append(f"Edge '{edge.get('id')}' target '{edge.get('target')}' not found")
        ctype = edge.get("condition", {}).get("type", "default")
        if ctype not in CONDITION_TYPES:
            errors.append(f"Edge '{edge.get('id')}' unknown condition type '{ctype}'")

    for node in nodes:
        ntype = node.get("type")
        if ntype not in NODE_TYPES:
            errors.append(f"Node '{node.get('id')}' unknown type '{ntype}'")

    return errors
