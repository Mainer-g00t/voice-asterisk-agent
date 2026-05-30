"""
Redis connection pool + snapshot push helpers.
"""

import json

import redis.asyncio as aioredis
from loguru import logger

_redis: aioredis.Redis | None = None

AGENT_CONFIG_TTL = 300    # seconds
DEFAULTS_TTL = 600
CALL_VARS_TTL = 600       # 10 min — plenty of time for the call to connect


async def init_redis(url: str) -> None:
    global _redis
    _redis = aioredis.from_url(url, decode_responses=True)


async def close_redis() -> None:
    if _redis:
        await _redis.aclose()


def get_redis() -> aioredis.Redis:
    assert _redis is not None, "Redis not initialised"
    return _redis


async def push_agent_snapshot(snapshot: dict) -> bool:
    """
    Write the full denormalized snapshot to Redis.
    Returns True on success, False if Redis is unavailable.
    """
    slug = snapshot["slug"]
    key = f"agent:config:{slug}"
    try:
        r = get_redis()
        await r.setex(key, AGENT_CONFIG_TTL, json.dumps(snapshot))
        logger.info(f"Redis: pushed snapshot for agent '{slug}' (TTL {AGENT_CONFIG_TTL}s)")
        return True
    except Exception as exc:
        logger.warning(f"Redis push failed for agent '{slug}': {exc}")
        return False


async def delete_agent_snapshot(slug: str) -> None:
    try:
        await get_redis().delete(f"agent:config:{slug}")
    except Exception as exc:
        logger.warning(f"Redis delete failed for '{slug}': {exc}")


async def push_call_vars(call_uuid: str, vars: dict) -> None:
    """
    Store per-call template variables so the agent can substitute them into
    the system prompt and greeting at call start.
    Key: call:vars:{call_uuid}  TTL: CALL_VARS_TTL seconds
    """
    try:
        await get_redis().setex(
            f"call:vars:{call_uuid}", CALL_VARS_TTL, json.dumps(vars)
        )
    except Exception as exc:
        logger.warning(f"Redis: failed to store call vars for {call_uuid}: {exc}")


async def get_agent_snapshot(slug: str) -> dict | None:
    try:
        raw = await get_redis().get(f"agent:config:{slug}")
        return json.loads(raw) if raw else None
    except Exception:
        return None
