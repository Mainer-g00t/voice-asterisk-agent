"""
CallLogger — collects metadata and transcript during a call, then POSTs
to config-api /api/calls when the call ends.

Usage in pipeline.py:
    logger = CallLogger(call_uuid, slug, providers, config_api_url)
    # pass logger into event handlers to record start/end times
    return task, logger

Usage in server.py (finally block):
    await call_logger.finalize(end_reason="hangup")
    await call_logger.send()
"""

import os
from datetime import datetime, timezone

import httpx
from loguru import logger as log


def _extract_text(content) -> str:
    """Normalise Pipecat message content — handles str, list-of-blocks, or dict."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "text" in block:
                    parts.append(block["text"])
        return " ".join(parts).strip()
    if isinstance(content, dict):
        return block.get("text", str(content))
    return str(content)


class CallLogger:
    def __init__(
        self,
        call_uuid: str,
        agent_slug: str,
        providers: dict,
        config_api_url: str,
        greeting_trigger: str = "Hello",
    ):
        self.call_uuid = call_uuid
        self.agent_slug = agent_slug
        self.providers = providers
        self.config_api_url = config_api_url
        self.greeting_trigger = greeting_trigger

        self.started_at: datetime | None = None
        self.ended_at: datetime | None = None
        self.end_reason: str = "unknown"
        self._context = None   # set by on_connected

    def on_connected(self, context) -> None:
        self.started_at = datetime.now(timezone.utc)
        self._context = context

    def on_disconnected(self, reason: str = "hangup") -> None:
        self.ended_at = datetime.now(timezone.utc)
        self.end_reason = reason

    def _build_transcript(self) -> list[dict]:
        """
        Extract conversation turns from LLMContext, skipping:
          - system messages
          - the synthetic greeting_trigger (first user message)
        """
        if self._context is None:
            return []

        messages = list(self._context.messages)
        transcript = []
        greeting_skipped = False

        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                continue
            text = _extract_text(msg.get("content", ""))
            if not text:
                continue
            # Skip the synthetic greeting trigger injected on connect
            if role == "user" and not greeting_skipped and text == self.greeting_trigger:
                greeting_skipped = True
                continue
            transcript.append({"role": role, "content": text})

        return transcript

    async def send(self) -> None:
        """POST call data to config-api. Fire-and-forget — logs errors but doesn't raise."""
        transcript = self._build_transcript()
        turn_count = sum(1 for m in transcript if m["role"] == "user")

        duration = None
        if self.started_at and self.ended_at:
            duration = max(0, int((self.ended_at - self.started_at).total_seconds()))

        payload = {
            "call_uuid": self.call_uuid,
            "agent_slug": self.agent_slug,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": duration,
            "turn_count": turn_count,
            "transcript": transcript,
            "stt_provider": self.providers.get("stt", {}).get("name"),
            "llm_provider": self.providers.get("llm", {}).get("name"),
            "tts_provider": self.providers.get("tts", {}).get("name"),
            "end_reason": self.end_reason,
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.config_api_url}/api/calls",
                    json=payload,
                    timeout=5.0,
                )
                resp.raise_for_status()
            log.info(f"Call log sent for {self.call_uuid} ({turn_count} turns, {duration}s)")
        except Exception as exc:
            log.error(f"Failed to send call log for {self.call_uuid}: {exc}")
