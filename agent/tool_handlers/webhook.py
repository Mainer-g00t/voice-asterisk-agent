"""
Webhook handler — POSTs the LLM's tool arguments to a configurable HTTP endpoint
and returns the response body to the LLM.

handler_config (stored in tool_definitions.handler_config):
  {
    "url":     "https://example.com/webhook",   # required
    "timeout": 10                                # optional, default 10 s
  }
"""

import httpx
from loguru import logger
from pipecat.services.llm_service import FunctionCallParams


def make_webhook_handler(agent_config: dict, tool_config: dict | None = None):
    """
    Returns an async handler that POSTs tool arguments to the configured webhook URL.
    """
    hc = (tool_config or {}).get("handler_config") or {}
    url = hc.get("url", "")
    timeout = float(hc.get("timeout", 10))
    tool_name = (tool_config or {}).get("tool_name", "webhook")

    if not url:
        logger.warning(f"[webhook:{tool_name}] No URL configured in handler_config — calls will fail")

    async def _handler(params: FunctionCallParams) -> None:
        if not url:
            await params.result_callback({"error": "Webhook URL not configured"})
            return

        payload = {"tool": tool_name, "arguments": params.arguments}
        logger.info(f"[webhook:{tool_name}] POST {url} args={params.arguments}")

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=timeout)
                resp.raise_for_status()
                try:
                    result = resp.json()
                except Exception:
                    result = {"response": resp.text}
                logger.info(f"[webhook:{tool_name}] response {resp.status_code}: {result}")
                await params.result_callback(result)
        except httpx.TimeoutException:
            logger.error(f"[webhook:{tool_name}] timed out after {timeout}s")
            await params.result_callback({"error": f"Webhook timed out after {timeout}s"})
        except Exception as e:
            logger.error(f"[webhook:{tool_name}] request failed: {e}")
            await params.result_callback({"error": str(e)})

    return _handler
