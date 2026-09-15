# -*- coding: utf-8 -*-
"""调试平台 API：读取 TraceStore 数据，供 web/debug.html 可视化火焰图 / 调用树。

全部走内存 TraceStore，不依赖数据库。
"""
from fastapi import APIRouter, HTTPException, Query

from .tracing import trace_store


def create_debug_router(manager) -> APIRouter:
    router = APIRouter(prefix="/api/debug", tags=["debug"])

    @router.get("/summary")
    async def summary() -> dict:
        return trace_store.stats()

    @router.get("/traces")
    async def list_traces(
        limit: int = Query(50, ge=1, le=200),
        q: str | None = Query(None, description="模糊搜索：消息文字/工具名/trace_id"),
        session_id: str | None = Query(None),
    ) -> dict:
        items = trace_store.list_recent(limit=limit, search=q, session_id=session_id)
        return {"traces": [t.summary() for t in items]}

    @router.get("/traces/{trace_id}")
    async def trace_detail(trace_id: str) -> dict:
        rec = trace_store.get(trace_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="trace 不存在或已被淘汰")
        return {"trace": rec.detail()}

    @router.get("/flamegraph")
    async def flamegraph(
        trace_id: str | None = Query(None),
        limit: int = Query(100, ge=1, le=200),
    ) -> dict:
        tree = trace_store.flamegraph(trace_id=trace_id or None, limit=limit)
        root = dict(tree)
        root["generated_from"] = ("trace:" + trace_id) if trace_id else f"最近 {limit} 条 trace"
        return {"flame": root}

    @router.post("/clear")
    async def clear() -> dict:
        n = trace_store.clear()
        return {"cleared": n}

    @router.get("/latest")
    async def latest() -> dict:
        rec = trace_store.latest_trace()
        if rec is None:
            return {"trace": None}
        return {"trace": rec.detail()}

    return router
