"""
FlowController — per-call flow execution state machine (agent side).

The controller:
  - Holds the flow definition, current node, and runtime state
  - Evaluates edge conditions locally (no network hop per event)
  - Emits FlowTransitionFrame into a shared asyncio.Queue when a transition fires
  - Accumulates events for bulk-posting to config-api at call end

FlowWatcherProcessor (a Pipecat FrameProcessor) wraps the controller and hooks
it into the pipeline.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger
from pipecat.frames.frames import (
    EndFrame,
    LLMFullResponseEndFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from frames import DTMFInputFrame, FlowTransitionFrame

# Import the stateless engine (copied into the agent container via Dockerfile COPY
# or mounted — we duplicate flow_engine.py for agent-side use).
from voiceai_common.flow_engine import apply_event, get_node, get_edges_from


CONFIG_API_URL = os.environ.get("CONFIG_API_URL", "http://config-api:8080")


class FlowController:
    """
    Manages flow execution state for one call.
    Thread-safe (asyncio only; all access is from the same event loop).
    """

    def __init__(
        self,
        flow_def: dict,
        call_uuid: str,
        execution_id: str,
        entry_node_id: str,
        transition_queue: asyncio.Queue,
    ) -> None:
        self.flow_def = flow_def
        self.call_uuid = call_uuid
        self.execution_id = execution_id
        self._current_node_id = entry_node_id
        self._state: dict[str, Any] = {}
        self._transition_queue = transition_queue
        self._events: list[dict] = []  # accumulated for bulk post at call end
        self._finished = False  # set after first end/transfer transition

        self._log_event("node_entered", node_id=entry_node_id, data={})

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def current_node_id(self) -> str:
        return self._current_node_id

    @property
    def current_node(self) -> dict | None:
        return get_node(self.flow_def, self._current_node_id)

    @property
    def state(self) -> dict:
        return dict(self._state)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    # ── Event handlers called by FlowWatcherProcessor ─────────────────────────

    async def on_transcription(self, text: str) -> None:
        if self._finished:
            return
        await self._process_event({"type": "transcription", "text": text})

    async def on_dtmf(self, digit: str) -> None:
        if self._finished:
            return
        await self._process_event({"type": "dtmf", "digit": digit})

    async def on_turn_end(self) -> None:
        if self._finished:
            return
        await self._process_event({"type": "turn_end"})

    async def on_tool_result(self, tool_name: str, result: dict) -> None:
        if self._finished:
            return
        await self._process_event({"type": "tool_result", "tool": tool_name, "result": result})

    async def on_silence_timeout(self) -> None:
        if self._finished:
            return
        await self._process_event({"type": "silence_timeout"})

    async def on_webhook_result(self, result: dict) -> None:
        if self._finished:
            return
        await self._process_event({"type": "webhook_result", "result": result})

    async def execute_instant_node(self, node_id: str, node_type: str, cfg: dict) -> None:
        """
        Execute a node that doesn't wait for user audio — set_variable, condition.
        Fires an appropriate event so the engine evaluates outgoing edges immediately.
        """
        if self._finished:
            return
        if node_type == "set_variable":
            var = cfg.get("variable", "")
            val = cfg.get("value", "")
            await self._process_event({"type": "set_variable", "var": var, "value": str(val)})
        elif node_type == "condition":
            # Fire a no-op event so the engine evaluates variable_equals edges
            await self._process_event({"type": "condition_evaluate"})

    # ── Core state machine ────────────────────────────────────────────────────

    async def _process_event(self, event: dict) -> None:
        self._log_event("event_received", node_id=self._current_node_id, data=event)

        new_node_id, new_state, edge = apply_event(
            self.flow_def, self._current_node_id, self._state, event
        )

        self._state = new_state

        if edge:
            old_node = self._current_node_id
            self._current_node_id = new_node_id
            self._log_event(
                "edge_taken",
                node_id=old_node,
                edge_id=edge.get("id"),
                data={"target": new_node_id, "condition": edge.get("condition")},
            )
            self._log_event("node_entered", node_id=new_node_id, data={})

            next_node = get_node(self.flow_def, new_node_id)
            if next_node:
                logger.info(
                    f"[flow:{self.call_uuid}] Transition: {old_node} → {new_node_id} "
                    f"(type={next_node.get('type')}, edge={edge.get('id')})"
                )
                await self._emit_transition(next_node, edge.get("id", ""))

    async def _emit_transition(self, node: dict, edge_id: str) -> None:
        frame = FlowTransitionFrame(
            node_id=node["id"],
            node_type=node.get("type", ""),
            node_config=node.get("config", {}),
            edge_id=edge_id,
        )
        await self._transition_queue.put(frame)

        # Mark finished for terminal nodes so we don't fire further transitions
        if node.get("type") in ("end", "transfer"):
            self._finished = True

    # ── Event log ─────────────────────────────────────────────────────────────

    def _log_event(
        self,
        event_type: str,
        node_id: str | None = None,
        edge_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        self._events.append({
            "event_type": event_type,
            "node_id": node_id,
            "edge_id": edge_id,
            "data": data or {},
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    # ── Finalization (called after call ends) ─────────────────────────────────

    async def finalize(self, status: str = "completed") -> None:
        """
        Bulk-post execution state and event log to config-api.
        Fire-and-forget from server.py after call_log.send().
        """
        payload = {
            "call_uuid": self.call_uuid,
            "status": status,
            "current_node_id": self._current_node_id,
            "state": self._state,
            "events": self._events,
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{CONFIG_API_URL}/api/flows/executions/complete",
                    json=payload,
                    timeout=10.0,
                )
                resp.raise_for_status()
            logger.info(f"[flow:{self.call_uuid}] Execution finalized: {status}")
        except Exception as exc:
            logger.warning(f"[flow:{self.call_uuid}] Failed to finalize execution: {exc}")


# ── Pipecat FrameProcessor ────────────────────────────────────────────────────

class FlowWatcherProcessor(FrameProcessor):
    """
    Inserted into the Pipecat pipeline (between stt and aggregators.user()).
    Passes all frames through unchanged while notifying FlowController of
    transcription, DTMF, and LLM turn-end events.
    """

    def __init__(self, controller: FlowController, **kwargs) -> None:
        super().__init__(**kwargs)
        self._controller = controller

    async def process_frame(self, frame: object, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        try:
            if isinstance(frame, TranscriptionFrame):
                if frame.text and frame.text.strip():
                    await self._controller.on_transcription(frame.text)

            elif isinstance(frame, DTMFInputFrame):
                if frame.digit:
                    await self._controller.on_dtmf(frame.digit)

            elif isinstance(frame, LLMFullResponseEndFrame):
                await self._controller.on_turn_end()

        except Exception as exc:
            logger.warning(f"FlowWatcherProcessor error: {exc}")

        await self.push_frame(frame, direction)
