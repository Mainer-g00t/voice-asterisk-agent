"""
config-api — FastAPI service for agent configuration management.

Responsibilities:
  - CRUD for agent configs in Postgres
  - Push denormalized snapshots to Redis on every save
  - Serve /internal/agents/{slug}/snapshot for agent-side fallback
  - Serve /admin HTML UI (Jinja2 + htmx)
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import redis_client
from routers import agents, calls, internal, admin_ui, routes, tools

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool(os.environ["DATABASE_URL"])
    await redis_client.init_redis(os.environ["REDIS_URL"])
    yield
    await db.close_pool()
    await redis_client.close_redis()


app = FastAPI(title="Voice Agent Config API", lifespan=lifespan)

# Shared template engine — routers import this
templates = Jinja2Templates(directory="templates")

app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(tools.router,  prefix="/api/tools",  tags=["tools"])
app.include_router(routes.router, prefix="/api/routes", tags=["routes"])
app.include_router(calls.router,  prefix="/api/calls",  tags=["calls"])
app.include_router(internal.router, prefix="/internal", tags=["internal"])
app.include_router(admin_ui.router, prefix="/admin", tags=["admin"])


@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin/agents")
