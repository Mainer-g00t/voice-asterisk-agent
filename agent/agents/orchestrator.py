"""
Orchestrator agent — demonstrates multi-agent delegation over voice.

The orchestrator is a hotel front-desk concierge. It listens to the guest,
determines what they need, then calls route_to_specialist() to delegate to
a specialist subagent (a separate LLM call with its own system prompt).
The specialist's answer is relayed back to the caller by the orchestrator.

Flow:
  caller speaks → orchestrator LLM decides intent
                → calls route_to_specialist(specialist, query) [tool call]
                → handler spawns a claude-haiku subagent
                → specialist answer returned to orchestrator
                → orchestrator speaks the answer to the caller

Requires: LLM_PROVIDER=anthropic  (tool calling + subagent API calls)
          ANTHROPIC_API_KEY set in .env
"""

import os

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

# ── Orchestrator prompt ───────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are the front-desk concierge at a luxury hotel, answering calls from guests. "
    "Listen to what the guest needs, then use the route_to_specialist tool to get a response "
    "from the right specialist — do not answer directly. "
    "Specialists handle: "
    "'room_service' for food and drink orders, "
    "'maintenance' for room problems (broken AC, no hot water, leaks, etc.), "
    "'concierge' for local recommendations, taxi bookings, or any other request. "
    "Once the specialist replies, relay their answer naturally in one or two sentences. "
    "Never use bullet points or markdown."
)

GREETING_TRIGGER = "A hotel guest is calling the front desk. Please answer the phone warmly."

# ── Specialist system prompts (subagent context) ──────────────────────────────

_SPECIALISTS = {
    "room_service": (
        "You are a room service specialist at a luxury hotel. "
        "A guest has been routed to you because they want food or drinks. "
        "Acknowledge their order, confirm it clearly, and give an estimated delivery time of 20-30 minutes. "
        "Be warm and professional. Two sentences maximum. No markdown."
    ),
    "maintenance": (
        "You are the hotel maintenance coordinator. "
        "A guest has been routed to you because of a room issue. "
        "Apologize sincerely for the inconvenience, acknowledge the specific problem, "
        "and promise a technician will arrive within 15 minutes. "
        "Two sentences maximum. No markdown."
    ),
    "concierge": (
        "You are a knowledgeable hotel concierge. "
        "A guest has been routed to you for local recommendations or general assistance. "
        "Give a brief, genuinely helpful answer based on their request. "
        "Two sentences maximum. No markdown."
    ),
}

# ── Tool definition ───────────────────────────────────────────────────────────

TOOLS = ToolsSchema(standard_tools=[
    FunctionSchema(
        name="route_to_specialist",
        description=(
            "Route the caller's request to the appropriate specialist subagent. "
            "Call this once you understand what the guest needs."
        ),
        properties={
            "specialist": {
                "type": "string",
                "enum": ["room_service", "maintenance", "concierge"],
                "description": "Which specialist to delegate to.",
            },
            "query": {
                "type": "string",
                "description": "The guest's request, quoted or summarized.",
            },
        },
        required=["specialist", "query"],
    )
])

# ── Tool handler (spawns the subagent) ────────────────────────────────────────

async def _handle_route_to_specialist(params: FunctionCallParams) -> None:
    specialist = params.arguments.get("specialist", "concierge")
    query = params.arguments.get("query", "")
    system_prompt = _SPECIALISTS.get(specialist, _SPECIALISTS["concierge"])

    logger.info(f"[orchestrator] delegating to '{specialist}' subagent: {query!r}")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system_prompt,
            messages=[{"role": "user", "content": query}],
        )
        answer = response.content[0].text.strip()
        logger.info(f"[orchestrator] '{specialist}' subagent replied: {answer!r}")
    except Exception as e:
        logger.error(f"[orchestrator] subagent call failed: {e}")
        answer = "I'm sorry, I couldn't reach that department right now. Please hold and I'll try again."

    await params.result_callback({"specialist_response": answer})


def register_tools(llm) -> None:
    """Called by pipeline.py to wire up tool handlers on the LLM service."""
    llm.register_function("route_to_specialist", _handle_route_to_specialist)
