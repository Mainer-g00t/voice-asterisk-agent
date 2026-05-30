"""
Phone routes API — CRUD for DID → agent_slug mappings.
POST /api/routes/apply triggers container management + Asterisk reload.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import auth
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
async def list_routes(request: Request):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT pr.* FROM phone_routes pr
               JOIN agents a ON pr.agent_slug = a.slug
               WHERE ($1::uuid IS NULL OR a.owner_id = $1::uuid OR a.owner_id IS NULL)
               ORDER BY pr.did""",
            owner_id,
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_route(request: Request, body: RouteCreate):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # Verify agent is accessible to this user
        agent = await conn.fetchrow(
            "SELECT slug FROM agents WHERE slug=$1 AND ($2::uuid IS NULL OR owner_id=$2::uuid OR owner_id IS NULL)",
            body.agent_slug, owner_id,
        )
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
async def update_route(request: Request, route_id: str, body: RouteUpdate):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # Build SET clause
        updates, params = [], [route_id, owner_id]
        for field in ("agent_slug", "description", "is_active"):
            val = getattr(body, field)
            if val is not None:
                params.append(val)
                updates.append(f"pr.{field} = ${len(params)}")
        if not updates:
            raise HTTPException(status_code=422, detail="No fields to update")
        row = await conn.fetchrow(
            f"""UPDATE phone_routes pr
                SET {', '.join(updates)}
                FROM agents a
                WHERE pr.id=$1
                AND pr.agent_slug = a.slug
                AND ($2::uuid IS NULL OR a.owner_id = $2::uuid)
                RETURNING pr.*""",
            *params,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Route not found")
    return dict(row)


@router.delete("/{route_id}")
async def delete_route(request: Request, route_id: str):
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """DELETE FROM phone_routes pr
               USING agents a
               WHERE pr.id=$1
               AND pr.agent_slug = a.slug
               AND ($2::uuid IS NULL OR a.owner_id = $2::uuid)""",
            route_id, owner_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Route not found")
    return {"status": "deleted"}


# ── Apply ─────────────────────────────────────────────────────────────────────

@router.post("/apply")
async def apply_routes(request: Request):
    """
    Apply routes owned by the current user to Asterisk.
    Admin backdoor (owner_id=None) applies all active routes.
    """
    owner_id = auth.get_owner_id(request)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        routes = await conn.fetch(
            """SELECT pr.did, pr.agent_slug, pr.description
               FROM phone_routes pr
               JOIN agents a ON pr.agent_slug = a.slug
               WHERE pr.is_active=true
               AND ($1::uuid IS NULL OR a.owner_id = $1::uuid OR a.owner_id IS NULL)
               ORDER BY pr.did""",
            owner_id,
        )
    routes = [dict(r) for r in routes]

    active_slugs = {r["agent_slug"] for r in routes}
    container_results = {}
    errors = []

    for slug in active_slugs:
        try:
            result = docker_manager.ensure_agent_running(slug)
            container_results[slug] = result
        except Exception as e:
            errors.append(f"Container agent-{slug}: {e}")

    try:
        docker_manager.write_extensions_conf(routes)
    except Exception as e:
        errors.append(f"extensions.conf write failed: {e}")

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
    return docker_manager.list_running_agents()
