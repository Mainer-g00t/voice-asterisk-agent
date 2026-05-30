"""
Outbound call API — originates calls via Asterisk AMI.

Flow:
  1. POST /api/outbound/originate
  2. API generates call UUID, pre-creates a call_log record (direction='outbound')
  3. AMI Originate → Asterisk dials the destination
  4. When answered: Asterisk runs [outbound-agent] dialplan → AudioSocket → agent
  5. Agent runs STT→LLM→TTS as normal; posts call_log at end (upserts the pre-created row)

The campaign manager (or any caller) gets back a call_uuid immediately and can
poll GET /api/calls/{call_uuid} for status, transcript, and duration.
"""

import json
import os
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

import ami_client
import db
import docker_manager
import redis_client
from voiceai_common.flow_engine import get_entry_node

router = APIRouter()

# Channel format for outbound calls. {destination} is replaced with the dialed number/name.
# Examples:
#   PJSIP/{destination}               — dial a registered PJSIP endpoint by name (e.g. "softphone")
#   PJSIP/{destination}@my-trunk      — dial through a SIP trunk
#   SIP/{destination}@sip.provider    — legacy SIP channel
CHANNEL_FORMAT = os.environ.get("OUTBOUND_CHANNEL_FORMAT", "PJSIP/{destination}")


class OriginateRequest(BaseModel):
    destination: str                        # number or endpoint name, e.g. "+15551234567" or "softphone"
    agent_slug: str = "basic"              # which agent to use for the conversation
    caller_id: str = "Voice Agent <+10000000000>"
    timeout_seconds: int = 30              # ring timeout before giving up
    template_vars: dict[str, str] = {}    # substituted into {placeholders} in prompt and greeting
    metadata: dict[str, Any] = {}         # passed through to the CDR callback payload
    callback_url: str | None = None       # POSTed with the full CDR when the call ends
    flow_id: str | None = None            # optional flow to run for this call


@router.post("/originate", status_code=202)
async def originate_call(body: OriginateRequest):
    """
    Originate an outbound call. Returns immediately with a call_uuid.
    Poll GET /api/calls/{call_uuid} to track status and retrieve the transcript.
    """
    call_uuid = str(uuid.uuid4())
    channel = CHANNEL_FORMAT.format(destination=body.destination)

    # Pre-create the call_log so the campaign manager can poll for it immediately.
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # Verify agent exists
        agent_row = await conn.fetchrow(
            "SELECT slug FROM agents WHERE slug=$1 AND is_active=true", body.agent_slug
        )
        if not agent_row:
            raise HTTPException(status_code=404, detail=f"Agent '{body.agent_slug}' not found or inactive")

        await conn.execute(
            """INSERT INTO call_logs
                   (call_uuid, agent_slug, direction, destination, caller_id, end_reason)
               VALUES ($1, $2, 'outbound', $3, $4, 'pending')
               ON CONFLICT (call_uuid) DO NOTHING""",
            call_uuid, body.agent_slug, body.destination, body.caller_id,
        )

    # Store template vars in Redis so the agent can substitute them into the prompt/greeting.
    if body.template_vars:
        await redis_client.push_call_vars(call_uuid, body.template_vars)

    # If a flow is specified, pre-create the execution and cache it in Redis.
    if body.flow_id:
        try:
            flow = await db.get_flow(body.flow_id)
            if not flow:
                raise HTTPException(status_code=404, detail=f"Flow '{body.flow_id}' not found or inactive")
            entry_node = get_entry_node(flow["definition"])
            entry_node_id = entry_node["id"] if entry_node else None
            exec_id = await db.create_flow_execution(body.flow_id, call_uuid, entry_node_id or "")
            # Cache the full snapshot so the agent can load it instantly
            await redis_client.push_flow_execution(call_uuid, {
                "execution_id": exec_id,
                "flow_id": body.flow_id,
                "flow_def": flow["definition"],
                "current_node_id": entry_node_id,
                "state": {},
            })
            logger.info(f"Flow execution created: flow={body.flow_id} exec={exec_id} call={call_uuid}")
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(f"Failed to create flow execution for call {call_uuid}: {exc}")
            # Non-fatal: call proceeds without flow

    # Store call meta (callback_url, metadata) for retrieval when the call ends.
    if body.callback_url or body.metadata:
        await redis_client.push_call_meta(call_uuid, {
            "callback_url": body.callback_url,
            "metadata": body.metadata,
        })

    # Ensure the agent container is running before Asterisk tries to connect.
    try:
        status = docker_manager.ensure_agent_running(body.agent_slug)
        logger.info(f"Agent container for '{body.agent_slug}': {status}")
    except Exception as exc:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE call_logs SET end_reason='agent_start_failed' WHERE call_uuid=$1",
                call_uuid,
            )
        raise HTTPException(status_code=503, detail=f"Could not start agent container: {exc}")

    # Originate via AMI (non-blocking — Asterisk dials asynchronously).
    try:
        await ami_client.originate(
            channel=channel,
            call_uuid=call_uuid,
            agent_slug=body.agent_slug,
            destination=body.destination,
            caller_id=body.caller_id,
            timeout_ms=body.timeout_seconds * 1000,
        )
    except Exception as exc:
        # Mark the pre-created record as failed so it doesn't stay as 'pending'.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE call_logs SET end_reason='originate_failed' WHERE call_uuid=$1",
                call_uuid,
            )
        raise HTTPException(status_code=502, detail=f"AMI originate failed: {exc}")

    return {
        "call_uuid": call_uuid,
        "status": "originating",
        "channel": channel,
        "destination": body.destination,
        "agent_slug": body.agent_slug,
        "poll_url": f"/api/calls/{call_uuid}",
    }


@router.get("/status/{call_uuid}")
async def call_status(call_uuid: str):
    """Quick status check — returns direction, end_reason, duration for a call."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT call_uuid, agent_slug, direction, destination, dial_status, "
            "started_at, ended_at, duration_seconds, turn_count, end_reason "
            "FROM call_logs WHERE call_uuid=$1",
            call_uuid,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Call not found")
    return dict(row)
