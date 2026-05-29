"""
Internal endpoints — reachable only within the voiceai Docker network.
Used by agent/pipeline.py as a fallback when the Redis key is missing.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

import db
import redis_client

router = APIRouter()


@router.get("/agents/{slug}/snapshot")
async def get_snapshot(slug: str):
    """
    Return the full denormalized agent config snapshot.
    Also warms the Redis cache as a side-effect.
    """
    snapshot = await db.build_snapshot(slug)
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found or inactive")

    # Warm Redis (best-effort — don't fail if Redis is down)
    await redis_client.push_agent_snapshot(snapshot)

    return JSONResponse(content=snapshot)
