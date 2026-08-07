"""FastAPI app: REST for state, one WebSocket for live updates.

Bound to loopback only and gated behind a per-run random token, so another
local process can't drive the app or read your source through it.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import fsbrowse, gitutil, recents
from .ai import AiAnnotator
from .config import Settings
from .engine import Engine

STATIC_DIR = Path(__file__).parent / "static"


class Hub:
    """Fan-out to every connected websocket."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, kind: str, payload: dict[str, Any]) -> None:
        message = {"type": kind, **payload}
        async with self._lock:
            targets = list(self._clients)
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                await self.leave(ws)

    @property
    def count(self) -> int:
        return len(self._clients)


def create_app(engine: Engine, settings: Settings, token: str) -> FastAPI:
    app = FastAPI(title="Agent Monitor", docs_url=None, redoc_url=None)
    hub = Hub()
    app.state.engine = engine
    app.state.settings = settings
    app.state.hub = hub
    app.state.token = token

    # The watcher fires on a worker thread; hop back onto the event loop to
    # broadcast. Captured once at startup so the thread doesn't have to guess.
    @app.on_event("startup")
    async def _capture_loop() -> None:
        loop = asyncio.get_running_loop()

        def notify() -> None:
            asyncio.run_coroutine_threadsafe(
                hub.broadcast("snapshot", engine.snapshot()), loop
            )

        engine.on_update = notify

        def ai_arrived(result: dict[str, Any]) -> None:
            engine.apply_ai(result)
            asyncio.run_coroutine_threadsafe(
                hub.broadcast("ai", {**result, "meta_ai": engine.meta().get("ai")}), loop
            )

        engine.attach_ai(AiAnnotator(settings, on_result=ai_arrived))
        # A repo opened via the CLI starts watching only once the loop exists.
        engine._start_watcher()

    @app.on_event("shutdown")
    async def _teardown() -> None:
        engine.close()

    # ---- auth ---------------------------------------------------------

    @app.middleware("http")
    async def token_gate(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api"):
            supplied = request.headers.get("x-agent-monitor-token") or request.query_params.get(
                "token"
            )
            if supplied != token:
                return JSONResponse({"error": "bad token"}, status_code=403)
        response = await call_next(request)
        # Local single-user app: never let a browser hold a stale asset across
        # an upgrade. There is nothing to gain from caching over loopback.
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    def _require_repo() -> Engine:
        if engine.target is None:
            raise HTTPException(status_code=409, detail="no repository open")
        return engine

    # ---- state --------------------------------------------------------

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return {
            "meta": engine.meta(),
            "settings": settings.public(),
            "recents": recents.listing(),
            "has_repo": engine.target is not None,
        }

    @app.get("/api/snapshot")
    async def snapshot() -> dict[str, Any]:
        _require_repo()
        return engine.snapshot()

    @app.post("/api/open")
    async def open_repo(body: dict[str, Any]) -> dict[str, Any]:
        path = (body or {}).get("path")
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        try:
            target = await asyncio.to_thread(engine.open, path, (body or {}).get("scope"))
        except gitutil.GitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        recents.record(target.abs_scope, target.to_dict()["name"])
        payload = engine.snapshot()
        await hub.broadcast("snapshot", payload)
        return {"ok": True, "meta": payload["meta"]}

    @app.post("/api/mode")
    async def set_mode(body: dict[str, Any]) -> dict[str, Any]:
        _require_repo()
        mode = (body or {}).get("mode", "live")
        ref = (body or {}).get("ref")
        try:
            await asyncio.to_thread(engine.set_mode, mode, ref)
        except gitutil.GitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = engine.snapshot()
        await hub.broadcast("snapshot", payload)
        return {"ok": True, "meta": payload["meta"]}

    @app.post("/api/refresh")
    async def refresh() -> dict[str, Any]:
        _require_repo()
        await asyncio.to_thread(engine.rescan)
        payload = engine.snapshot()
        await hub.broadcast("snapshot", payload)
        return {"ok": True}

    @app.post("/api/pause")
    async def pause(body: dict[str, Any]) -> dict[str, Any]:
        # Pausing server-side too, so a paused window stops doing analysis work
        # rather than just discarding the results.
        engine.paused = bool((body or {}).get("paused"))
        if not engine.paused and engine.target is not None:
            await asyncio.to_thread(engine.rescan)
            await hub.broadcast("snapshot", engine.snapshot())
        return {"paused": engine.paused}

    # ---- pickers ------------------------------------------------------

    @app.get("/api/fs")
    async def fs(path: str | None = None, hidden: bool = False) -> dict[str, Any]:
        return await asyncio.to_thread(fsbrowse.listing, path, hidden)

    @app.get("/api/recents")
    async def get_recents() -> dict[str, Any]:
        return {"recents": recents.listing()}

    @app.get("/api/commits")
    async def commits(limit: int = 25) -> dict[str, Any]:
        eng = _require_repo()
        assert eng.target
        if not eng.target.is_git:
            return {"commits": []}
        return {
            "commits": await asyncio.to_thread(
                gitutil.recent_commits, eng.target.root, limit
            )
        }

    @app.get("/api/branches")
    async def branch_list() -> dict[str, Any]:
        eng = _require_repo()
        assert eng.target
        if not eng.target.is_git:
            return {"branches": [], "default": None}
        return {
            "branches": await asyncio.to_thread(gitutil.branches, eng.target.root),
            "default": await asyncio.to_thread(gitutil.default_branch, eng.target.root),
            "current": eng.target.branch,
        }

    # ---- settings -----------------------------------------------------

    @app.post("/api/settings")
    async def update_settings(body: dict[str, Any]) -> dict[str, Any]:
        body = body or {}
        if "model" in body and body["model"]:
            settings.set_model(str(body["model"]))
        if "api_key" in body:
            settings.set_key(body.get("api_key"), bool(body.get("remember")))
        if "ai_enabled" in body:
            settings.ai_enabled = bool(body["ai_enabled"])
            if settings.ai_enabled and engine.target is not None:
                # Annotate what's on screen now, don't wait for the next edit.
                engine.request_ai()
        return {**settings.public(), "ai": engine.meta().get("ai")}

    # ---- websocket ----------------------------------------------------

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if ws.query_params.get("token") != token:
            await ws.close(code=4403)
            return
        await ws.accept()
        await hub.join(ws)
        try:
            if engine.target is not None:
                await ws.send_json({"type": "snapshot", **engine.snapshot()})
            else:
                await ws.send_json({"type": "idle", "recents": recents.listing()})
            while True:
                await ws.receive_text()  # client is send-only via REST for now
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await hub.leave(ws)

    # ---- static -------------------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app
