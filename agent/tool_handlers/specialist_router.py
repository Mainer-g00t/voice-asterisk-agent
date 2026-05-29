"""
Specialist router handler — spawns a specialist subagent (separate LLM call)
and returns its response. Specialist prompts come from the agent config
snapshot fetched from Redis, so they're fully editable without a code deploy.
"""

import os

import anthropic
from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

_anthropic_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )
    return _anthropic_client


def make_specialist_handler(agent_config: dict):
    """
    Returns a closure that captures the specialists dict from the agent config
    snapshot. Called once per call in create_pipeline_task().
    """
    specialists: dict = agent_config.get("specialists", {})
    default_model = "claude-haiku-4-5-20251001"

    async def _handler(params: FunctionCallParams) -> None:
        specialist_key = params.arguments.get("specialist", "concierge")
        query = params.arguments.get("query", "")

        spec = specialists.get(specialist_key) or specialists.get("concierge") or {}
        system_prompt = spec.get("system_prompt", "You are a helpful specialist. Answer briefly.")
        model = spec.get("subagent_model") or default_model

        logger.info(f"[specialist_router] delegating to '{specialist_key}': {query!r}")

        try:
            response = await _get_client().messages.create(
                model=model,
                max_tokens=150,
                system=system_prompt,
                messages=[{"role": "user", "content": query}],
            )
            answer = response.content[0].text.strip()
            logger.info(f"[specialist_router] '{specialist_key}' replied: {answer!r}")
        except Exception as e:
            logger.error(f"[specialist_router] subagent call failed: {e}")
            answer = "I'm sorry, I couldn't reach that department. Please try again in a moment."

        await params.result_callback({"specialist_response": answer})

    return _handler
