# -*- coding: utf-8 -*-
"""FastAPI 服务入口：REST + SSE 聊天接口 + 静态前端。

toC 智能客服的 HTTP 层，基于 AgentScope 2.0 的 Agent / 事件流构建，
前端通过 SSE 实时接收思考过程、工具调用与回复增量。
"""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings, ROOT_DIR
from .session_manager import SessionManager
from .schemas import ChatRequest, ConfigResponse, HistoryMessage

WEB_DIR = ROOT_DIR / "web"

settings = get_settings()


def _agent_factory(session_id: str):
    from .agent_factory import build_customer_service_agent

    return build_customer_service_agent(settings, session_id)


manager = SessionManager(settings=settings, agent_factory=_agent_factory)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title="云雀商城智能客服 API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


from .admin import create_admin_router

app.include_router(create_admin_router(manager))

from .debug import create_debug_router

app.include_router(create_debug_router(manager))


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "provider": settings.model_provider, "model": settings.model_name}


@app.get("/api/config")
async def config() -> ConfigResponse:
    from .prompts import QUICK_PROMPTS

    return ConfigResponse(
        agent_name=settings.agent_name,
        brand_name=settings.brand_name,
        model_provider=settings.model_provider,
        model_name=settings.model_name,
        quick_prompts=QUICK_PROMPTS,
    )


@app.post("/api/sessions")
async def create_session() -> dict:
    runtime = await manager.create()
    return {"session_id": runtime.session_id}


@app.get("/api/sessions")
async def list_sessions() -> dict:
    items = []
    for runtime in manager.list():
        items.append({
            "session_id": runtime.session_id,
            "created_at": runtime.created_at,
            "last_active_at": runtime.last_active_at,
            "message_count": len(runtime.agent.state.context),
            "preview": runtime.preview,
            "handed_off": runtime.handed_off,
        })
    items.sort(key=lambda it: it["last_active_at"], reverse=True)
    return {"sessions": items}


@app.get("/api/sessions/{session_id}/history")
async def session_history(session_id: str) -> dict:
    runtime = manager.get(session_id)
    if not runtime:
        raise HTTPException(status_code=404, detail="会话不存在")
    from .service import build_history

    return {"messages": build_history(runtime)}


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    from .service import stream_chat

    async def gen():
        async for chunk in stream_chat(manager, settings, req.session_id, req.message):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "home.html")


@app.get("/chat")
async def chat_page() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
@app.get("/admin")
async def admin() -> FileResponse:
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/debug")
async def debug_platform() -> FileResponse:
    return FileResponse(WEB_DIR / "debug.html")


# 静态资源（app.js / style.css）
if (WEB_DIR / "index.html").exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
