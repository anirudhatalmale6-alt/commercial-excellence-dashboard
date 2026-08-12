"""
FastAPI application: routes, background sync loop, static frontend hosting.

Endpoints
    POST /api/auth/login          email + password -> token
    GET  /api/me                  viewer + who they can drill into
    GET  /api/dashboard           everything one screen needs, in one call
    GET  /api/actions             priority action list only (used by refresh)
    GET  /api/integrations        connector health strip
    POST /api/sync                force a refresh
    GET  /api/stream              server-sent events: push on every sync
    GET  /api/healthz             liveness

The dashboard is one call on purpose: an MVP that fires eight requests per page
load is an MVP that feels slow and is hard to hand over.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import auth_header, issue_token, user_from_header, verify_credentials
from .connectors.registry import SyncEngine
from .models import Role
from .services.actions import ActionEngine
from .services.kpi import KpiEngine
from .services.scope import build_scope, direct_reports

SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL_SECONDS", "30"))
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

engine = SyncEngine()
_subscribers: set[asyncio.Queue] = set()


def today() -> date:
    return datetime.now(timezone.utc).date()


async def _sync_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(engine.sync)
            payload = json.dumps(
                {
                    "event": "sync",
                    "at": datetime.now(timezone.utc).isoformat(),
                    "count": engine.sync_count,
                }
            )
            for q in list(_subscribers):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass
        except Exception as exc:  # keep the loop alive; health strip shows the failure
            print(f"[sync] failed: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(SYNC_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(engine.sync)  # first sync before serving traffic
    task = asyncio.create_task(_sync_loop())
    yield
    task.cancel()


app = FastAPI(title="Commercial Excellence MVP", version="0.9.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ helpers


def viewer(authorization: str | None = Depends(auth_header)):
    return user_from_header(engine.current(), authorization)


def _kpi_payload(k) -> dict:
    return {
        "key": k.key,
        "label": k.label,
        "value": k.value,
        "unit": k.unit,
        "target": k.target,
        "deltaPct": k.delta_pct,
        "hint": k.hint,
        "sources": k.sources,
        "status": k.status,
    }


# ------------------------------------------------------------------- routes


class LoginBody(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginBody):
    ds = engine.current()
    user = verify_credentials(ds, body.email, body.password)
    return {
        "token": issue_token(user.id),
        "user": {"id": user.id, "name": user.name, "role": user.role.value, "team": user.team},
    }


@app.get("/api/auth/demo-users")
def demo_users():
    """Only exposed so the test environment has a one-click login."""
    if os.getenv("EXPOSE_DEMO_USERS", "1") != "1":
        raise HTTPException(status_code=404, detail="disabled")
    ds = engine.current()
    return {
        "users": [
            {"id": u.id, "name": u.name, "email": u.email, "role": u.role.value, "team": u.team}
            for u in sorted(ds.users, key=lambda x: (x.role.value, x.name))
        ]
    }


@app.get("/api/me")
def me(user=Depends(viewer)):
    ds = engine.current()
    reports = direct_reports(ds, user.id) if user.role in (Role.MANAGER, Role.ADMIN) else []
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role.value,
        "team": user.team,
        "region": user.region,
        "reports": [{"id": r.id, "name": r.name} for r in reports],
    }


@app.get("/api/dashboard")
def dashboard(user=Depends(viewer), focus: str | None = Query(default=None)):
    ds = engine.current()
    try:
        scope = build_scope(ds, user, focus)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    k = KpiEngine(ds, today())
    a = ActionEngine(ds, today())

    payload = {
        "scope": {
            "label": scope.label,
            "isTeamView": scope.is_team_view,
            "focusUserId": scope.focus_user.id if scope.focus_user else None,
            "userCount": len(scope.user_ids),
        },
        "asOf": datetime.now(timezone.utc).isoformat(),
        "lastSyncAt": engine.last_sync_at.isoformat() if engine.last_sync_at else None,
        "kpis": [_kpi_payload(x) for x in k.headline(scope)],
        "revenueTrend": k.revenue_trend(scope),
        "funnel": k.funnel(scope),
        "leaderboard": k.leaderboard(scope) if scope.is_team_view else {"rows": []},
        "topAccounts": k.top_accounts(scope),
        "activity": k.activity(scope),
        "actions": [asdict(x) for x in a.build(scope)],
        "integrations": [h.model_dump(mode="json") for h in engine.health],
    }
    return payload


@app.get("/api/actions")
def actions(user=Depends(viewer), focus: str | None = Query(default=None), limit: int = 12):
    ds = engine.current()
    try:
        scope = build_scope(ds, user, focus)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return {"actions": [asdict(x) for x in ActionEngine(ds, today()).build(scope, limit=limit)]}


@app.get("/api/integrations")
def integrations():
    return {
        "connectors": [h.model_dump(mode="json") for h in engine.health],
        "lastSyncAt": engine.last_sync_at.isoformat() if engine.last_sync_at else None,
        "syncCount": engine.sync_count,
        "intervalSeconds": SYNC_INTERVAL,
    }


@app.post("/api/sync")
async def force_sync(user=Depends(viewer)):
    await asyncio.to_thread(engine.sync)
    return {"ok": True, "lastSyncAt": engine.last_sync_at.isoformat() if engine.last_sync_at else None}


@app.get("/api/stream")
async def stream(request: Request):
    """SSE: one message per completed sync. The UI refetches when it arrives."""
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    _subscribers.add(q)

    async def gen():
        try:
            yield "retry: 5000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _subscribers.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/healthz")
def healthz():
    return {
        "ok": True,
        "syncCount": engine.sync_count,
        "lastSyncAt": engine.last_sync_at.isoformat() if engine.last_sync_at else None,
    }


# --------------------------------------------------------------- frontend

if FRONTEND_DIR.exists():
    app.mount("/css", StaticFiles(directory=FRONTEND_DIR / "css"), name="css")
    app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="js")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/login")
    def login_page():
        return FileResponse(FRONTEND_DIR / "login.html")
