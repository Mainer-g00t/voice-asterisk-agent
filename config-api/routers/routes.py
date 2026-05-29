"""
Phone routes API — CRUD for DID → agent_slug mappings.
POST /api/routes/apply triggers container management + Asterisk reload.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db
import docker_manager

router = APIRouter()


class RouteCreate(BaseModel):
    did: str
    agent_slug: str
    description: str | None = None
    is_active: bool = True


class RouteUpdate(BaseModel):
    agent_slug: str | None = None
    description: str | None = None
    is_active: bool | None = None


# ── Route CRUD ────────────────────────────────────────────────────────────────

@router.get("")
async def list_routes():
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM phone_routes ORDER BY did"
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_route(body: RouteCreate):
    # Verify agent exists
    pool = db.get_pool()
    async with pool.acquire() as conn:
        agent = await conn.fetchrow("SELECT slug FROM agents WHERE slug=$1", body.agent_slug)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{body.agent_slug}' not found")
        try:
            row = await conn.fetchrow(
                "INSERT INTO phone_routes (did, agent_slug, description, is_active) "
                "VALUES ($1, $2, $3, $4) RETURNING *",
                body.did, body.agent_slug, body.description, body.is_active,
            )
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(status_code=409, detail=f"DID '{body.did}' already exists")
            raise
    return dict(row)


@router.put("/{route_id}")
async def update_route(route_id: str, body: RouteUpdate):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        updates, params = [], [route_id]
        for field in ("agent_slug", "description", "is_active"):
            val = getattr(body, field)
            if val is not None:
                params.append(val)
                updates.append(f"{field} = ${len(params)}")
        if not updates:
            raise HTTPException(status_code=422, detail="No fields to update")
        row = await conn.fetchrow(
            f"UPDATE phone_routes SET {', '.join(updates)} WHERE id=$1 RETURNING *",
            *params,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Route not found")
    return dict(row)


@router.delete("/{route_id}")
async def delete_route(route_id: str):
    pool = db.get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM phone_routes WHERE id=$1", route_id
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Route not found")
    return {"status": "deleted"}


# ── Apply ─────────────────────────────────────────────────────────────────────

@router.post("/apply")
async def apply_routes():
    """
    1. Fetch all active routes.
    2. Ensure a container is running for each unique agent slug referenced.
    3. Generate extensions.conf from the routes.
    4. Reload Asterisk dialplan.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        routes = await conn.fetch(
            "SELECT did, agent_slug, description FROM phone_routes WHERE is_active=true ORDER BY did"
        )
    routes = [dict(r) for r in routes]

    # Determine which slugs need containers
    active_slugs = {r["agent_slug"] for r in routes}

    container_results = {}
    errors = []

    for slug in active_slugs:
        try:
            result = docker_manager.ensure_agent_running(slug)
            container_results[slug] = result
        except Exception as e:
            errors.append(f"Container agent-{slug}: {e}")

    # Write extensions.conf
    try:
        docker_manager.write_extensions_conf(routes)
    except Exception as e:
        errors.append(f"extensions.conf write failed: {e}")

    # Reload Asterisk dialplan
    dialplan_output = None
    try:
        dialplan_output = docker_manager.reload_asterisk_dialplan()
    except Exception as e:
        errors.append(f"Asterisk reload failed: {e}")

    return {
        "routes": len(routes),
        "containers": container_results,
        "dialplan_reloaded": dialplan_output is not None,
        "dialplan_output": dialplan_output,
        "errors": errors,
    }


@router.get("/status")
async def containers_status():
    """Return current status of all agent-* Docker containers."""
    return docker_manager.list_running_agents()
