"""
FastAPI server for the Avalon Q Controller (v2 - multi-miner).

Endpoint groups:
  /api/miners          - CRUD on miners
  /api/miners/{id}/...  - per-miner status/schedule/pools/history/events
  /api/discovery       - LAN scan
  /api/timezones       - IANA tz list for UI typeahead
"""

from __future__ import annotations

import logging
import os
import zoneinfo
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .controller import Controller, DEFAULT_POLL_SECONDS
from .db import DB
from .discovery import scan as discovery_scan, local_subnet_guess
from .scheduler import is_valid_action

log = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"
DATA_DIR.mkdir(parents=True, exist_ok=True)

db = DB(DATA_DIR / "controller.db")
controller = Controller(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await controller.start()
    try:
        yield
    finally:
        await controller.stop()


app = FastAPI(
    title="Avalon Q Controller",
    version="2.0.0",
    lifespan=lifespan,
)


# =====================================================================
# Pydantic schemas
# =====================================================================
class MinerCreate(BaseModel):
    name: str
    host: str
    port: int = 4028
    username: str = "root"
    password: str = "admin"
    poll_seconds: int = DEFAULT_POLL_SECONDS


class MinerUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    poll_seconds: int | None = None
    enabled: bool | None = None


class PoolCreate(BaseModel):
    name: str
    url: str
    worker: str
    worker_password: str = "x"
    notes: str = ""


class PoolUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    worker: str | None = None
    worker_password: str | None = None
    notes: str | None = None


class RuleModel(BaseModel):
    id: str
    name: str = ""
    action: str
    days: list[int]
    start: str
    end: str
    enabled: bool = True
    priority: int = 0
    season_start: list[int] | None = None
    season_end: list[int] | None = None
    reboot_after_pool_switch: bool = False
    chain_workmode: str | None = None


class ScheduleModel(BaseModel):
    timezone: str = "UTC"
    enabled: bool = True
    default_action: str = "none"
    rules: list[RuleModel] = Field(default_factory=list)


class PauseModel(BaseModel):
    paused: bool


class OverrideModel(BaseModel):
    action: str
    duration_minutes: int | None = None
    reason: str = "manual"


class DiscoveryRequest(BaseModel):
    cidr: str | None = None


class ApplyPoolRequest(BaseModel):
    reboot: bool = False


# =====================================================================
# Routes - HTML
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = TEMPLATES_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# =====================================================================
# Routes - miners
# =====================================================================
@app.get("/api/miners")
async def list_miners() -> list[dict]:
    items = []
    for m in db.list_miners():
        d = m.to_public_dict()
        state = db.get_state(m.id)
        d["last_status"] = state.get("last_status")
        d["paused"] = state.get("paused")
        d["override"] = state.get("override")
        items.append(d)
    return items


@app.post("/api/miners")
async def create_miner(body: MinerCreate) -> dict:
    if not body.host:
        raise HTTPException(400, "host is required")
    if not body.name:
        raise HTTPException(400, "name is required")
    try:
        m = db.create_miner(
            name=body.name,
            host=body.host,
            port=body.port,
            username=body.username,
            password=body.password,
            poll_seconds=body.poll_seconds,
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"Miner {body.host}:{body.port} already exists")
        raise
    controller.restart_miner(m.id)
    return m.to_public_dict()


@app.get("/api/miners/{miner_id}")
async def get_miner(miner_id: int) -> dict:
    m = db.get_miner(miner_id)
    if not m:
        raise HTTPException(404, "Miner not found")
    state = db.get_state(miner_id)
    d = m.to_public_dict()
    d["last_status"] = state.get("last_status")
    d["paused"] = state.get("paused")
    d["override"] = state.get("override")
    d["schedule"] = state.get("schedule")
    d["last_pool_id"] = state.get("last_pool_id")
    return d


@app.patch("/api/miners/{miner_id}")
async def update_miner(miner_id: int, body: MinerUpdate) -> dict:
    fields = body.model_dump(exclude_none=True)
    if "password" in fields and fields["password"] == "***":
        fields.pop("password")
    m = db.update_miner(miner_id, **fields)
    if not m:
        raise HTTPException(404, "Miner not found")
    controller.restart_miner(m.id)
    return m.to_public_dict()


@app.delete("/api/miners/{miner_id}")
async def delete_miner(miner_id: int) -> dict:
    if not db.delete_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    controller.restart_miner(miner_id)
    return {"ok": True}


@app.post("/api/miners/{miner_id}/refresh")
async def refresh_miner(miner_id: int) -> dict:
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    return await controller.poll_once(miner_id)


@app.get("/api/miners/{miner_id}/status")
async def miner_status(miner_id: int) -> dict:
    state = db.get_state(miner_id)
    return state.get("last_status") or {
        "online": False,
        "error": "No data yet",
        "miner_id": miner_id,
    }


# ---------- per-miner schedule / pause / override ---------------------
@app.put("/api/miners/{miner_id}/schedule")
async def set_schedule(miner_id: int, body: ScheduleModel) -> dict:
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    payload = body.model_dump()
    try:
        zoneinfo.ZoneInfo(payload["timezone"])
    except zoneinfo.ZoneInfoNotFoundError:
        raise HTTPException(400, f"Unknown timezone: {payload['timezone']}")
    if not is_valid_action(payload["default_action"]):
        raise HTTPException(400, f"Invalid default_action: {payload['default_action']}")
    for r in payload["rules"]:
        if not is_valid_action(r["action"]):
            raise HTTPException(400, f"Invalid rule action: {r['action']}")
        if r.get("chain_workmode") and r["chain_workmode"] not in (
            "eco", "standard", "super", None
        ):
            raise HTTPException(400, "chain_workmode must be eco/standard/super or null")
        for d in r["days"]:
            if not 0 <= int(d) <= 6:
                raise HTTPException(400, "Day index must be 0-6")
    db.set_schedule(miner_id, payload)
    return db.get_state(miner_id)


@app.post("/api/miners/{miner_id}/pause")
async def set_pause(miner_id: int, body: PauseModel) -> dict:
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    db.set_paused(miner_id, body.paused)
    return db.get_state(miner_id)


@app.post("/api/miners/{miner_id}/override")
async def set_override(miner_id: int, body: OverrideModel) -> dict:
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    if not is_valid_action(body.action):
        raise HTTPException(400, f"Invalid action: {body.action}")
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("UTC"))
    expires = None
    if body.duration_minutes:
        expires = (now + timedelta(minutes=body.duration_minutes)).isoformat()
    db.set_override(
        miner_id,
        {
            "action": body.action,
            "set_at": now.isoformat(),
            "expires_at": expires,
            "reason": body.reason,
        },
    )
    return db.get_state(miner_id)


@app.delete("/api/miners/{miner_id}/override")
async def clear_override(miner_id: int) -> dict:
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    db.set_override(miner_id, None)
    return db.get_state(miner_id)


VALID_COMMANDS = {
    "reboot", "lcd_on", "lcd_off", "soft_on", "soft_off",
    "eco", "standard", "super",
}


@app.post("/api/miners/{miner_id}/command/{name}")
async def manual_command(miner_id: int, name: str) -> dict:
    if name not in VALID_COMMANDS:
        raise HTTPException(400, f"Unknown command: {name}")
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    return await controller.manual_command(miner_id, name)


# ---------- pools -----------------------------------------------------
@app.get("/api/miners/{miner_id}/pools")
async def list_pools(miner_id: int) -> list[dict]:
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    return [p.to_dict() for p in db.list_pools(miner_id)]


@app.post("/api/miners/{miner_id}/pools")
async def create_pool(miner_id: int, body: PoolCreate) -> dict:
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    if not body.url:
        raise HTTPException(400, "url is required")
    if not body.worker:
        raise HTTPException(400, "worker is required")
    p = db.create_pool(
        miner_id=miner_id,
        name=body.name or body.url,
        url=body.url,
        worker=body.worker,
        worker_password=body.worker_password,
        notes=body.notes,
    )
    return p.to_dict()


@app.patch("/api/pools/{pool_id}")
async def update_pool(pool_id: int, body: PoolUpdate) -> dict:
    p = db.get_pool(pool_id)
    if not p:
        raise HTTPException(404, "Pool not found")
    fields = body.model_dump(exclude_none=True)
    p = db.update_pool(pool_id, **fields)
    return p.to_dict()


@app.delete("/api/pools/{pool_id}")
async def delete_pool(pool_id: int) -> dict:
    p = db.get_pool(pool_id)
    if not p:
        raise HTTPException(404, "Pool not found")
    db.delete_pool(pool_id)
    return {"ok": True}


@app.post("/api/pools/{pool_id}/apply")
async def apply_pool(pool_id: int, body: ApplyPoolRequest) -> dict:
    p = db.get_pool(pool_id)
    if not p:
        raise HTTPException(404, "Pool not found")
    return await controller.apply_pool(p.miner_id, pool_id, reboot=body.reboot)


# ---------- history (samples) ----------------------------------------
@app.get("/api/miners/{miner_id}/history")
async def history(miner_id: int) -> dict:
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    samples = db.get_samples(miner_id)
    return {"miner_id": miner_id, "samples": samples}


# ---------- events ----------------------------------------------------
@app.get("/api/miners/{miner_id}/events")
async def miner_events(miner_id: int, limit: int = 100) -> list[dict]:
    if not db.get_miner(miner_id):
        raise HTTPException(404, "Miner not found")
    return db.list_events(miner_id=miner_id, limit=limit)


@app.get("/api/events")
async def all_events(limit: int = 100) -> list[dict]:
    return db.list_events(miner_id=None, limit=limit)


# ---------- discovery -------------------------------------------------
@app.post("/api/discovery/scan")
async def discovery_endpoint(body: DiscoveryRequest) -> dict:
    try:
        results = await discovery_scan(body.cidr)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "subnet_used": body.cidr or local_subnet_guess(),
        "results": [r.__dict__ for r in results],
    }


@app.get("/api/discovery/subnet")
async def discovery_subnet() -> dict:
    return {"subnet": local_subnet_guess()}


# ---------- meta ------------------------------------------------------
@app.get("/api/timezones")
async def timezones() -> list[str]:
    return sorted(zoneinfo.available_timezones())


# ---------- static files ---------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("Unhandled error in %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )
