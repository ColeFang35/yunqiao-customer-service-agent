# -*- coding: utf-8 -*-
"""客服管理后台 API 路由。

面向运营 / 人工客服的管理端：会话监控、工单处理、FAQ 知识库管理、
订单与用户概览。数据复用 backend/store 的 JSON 仓库。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .store.mock_store import (
    cart_store,
    faq_store,
    order_store,
    product_store,
    order_store,
    ticket_store,
    user_store,
)

_CATEGORY_CN = {
    "complaint": "投诉",
    "suggestion": "建议",
    "quality": "质量问题",
    "logistics": "物流异常",
    "other": "其他",
}
_TICKET_STATUS = {"待处理", "处理中", "已完成", "已转人工"}


def create_admin_router(manager) -> APIRouter:
    """构造后台路由。manager 为 SessionManager 实例（会话监控用）。"""
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    # ------------------------------------------------------------------
    # 概览
    # ------------------------------------------------------------------
    @router.get("/stats")
    async def stats() -> dict:
        tickets = await ticket_store.all()
        sessions = manager.list()
        return {
            "sessions": len(sessions),
            "handed_off": sum(1 for s in sessions if s.handed_off),
            "orders": len(await order_store.all()),
            "users": len(await user_store.all()),
            "tickets": len(tickets),
            "tickets_pending": sum(1 for t in tickets if t.get("status") == "待处理"),
            "tickets_pending": sum(1 for t in tickets if t.get("status") == "待处理"),
            "tickets_done": sum(1 for t in tickets if t.get("status") == "已完成"),
            "products": len(await product_store.all()),
        }

    # ------------------------------------------------------------------
    # 会话监控（复用 SessionManager 内存数据）
    # ------------------------------------------------------------------
    @router.get("/sessions")
    async def sessions() -> dict:
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

    @router.get("/sessions/{session_id}/history")
    async def session_history(session_id: str) -> dict:
        runtime = manager.get(session_id)
        if not runtime:
            raise HTTPException(status_code=404, detail="会话不存在")
        from .service import build_history

        return {"messages": build_history(runtime)}

    # ------------------------------------------------------------------
    # 工单处理
    # ------------------------------------------------------------------
    @router.get("/tickets")
    async def tickets() -> dict:
        items = await ticket_store.all()
        for t in items:
            t["category_cn"] = _CATEGORY_CN.get(t.get("category"), t.get("category"))
        items.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        return {"tickets": items}

    class TicketUpdate(BaseModel):
        status: str | None = Field(default=None, description="状态：待处理/处理中/已完成/已转人工")
        assignee: str | None = None
        reply: str | None = None
        note: str | None = None

    @router.patch("/tickets/{ticket_id}")
    async def update_ticket(ticket_id: str, body: TicketUpdate) -> dict:
        updates: dict = {}
        if body.status is not None:
            if body.status not in _TICKET_STATUS:
                raise HTTPException(status_code=400, detail="非法状态")
            updates["status"] = body.status
        if body.assignee is not None:
            updates["assignee"] = body.assignee
        if body.note is not None:
            updates["note"] = body.note
        if body.reply is not None:
            updates["reply"] = body.reply
            updates["replied_at"] = now_cn_reply()
        ticket = await ticket_store.update(ticket_id, **updates) if updates else await ticket_store.find_by_id(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="工单不存在")
        ticket["category_cn"] = _CATEGORY_CN.get(ticket.get("category"), ticket.get("category"))
        return {"ticket": ticket}

    # ------------------------------------------------------------------
    # FAQ 知识库管理
    # ------------------------------------------------------------------
    @router.get("/faq")
    async def faq() -> dict:
        return {"entries": await faq_store.all()}

    class FaqCreate(BaseModel):
        title: str = Field(min_length=1)
        keywords: list[str] = Field(default_factory=list)
        content: str = Field(min_length=1)

    @router.post("/faq")
    async def create_faq(body: FaqCreate) -> dict:
        entry = await faq_store.create(body.title, body.keywords, body.content)
        return {"entry": entry}

    @router.put("/faq/{faq_id}")
    async def update_faq(faq_id: str, body: FaqCreate) -> dict:
        entry = await faq_store.update(
            faq_id,
            title=body.title,
            keywords=body.keywords,
            content=body.content,
        )
        if not entry:
            raise HTTPException(status_code=404, detail="FAQ 不存在")
        return {"entry": entry}

    @router.delete("/faq/{faq_id}")
    async def delete_faq(faq_id: str) -> dict:
        ok = await faq_store.delete(faq_id)
        if not ok:
            raise HTTPException(status_code=404, detail="FAQ 不存在")
        return {"ok": True}


    # ------------------------------------------------------------------
    # 商品管理
    # ------------------------------------------------------------------
    @router.get("/products")
    async def products(keyword: str = "") -> dict:
        items = await product_store.search(keyword=keyword)
        return {"products": items}

    class ProductCreate(BaseModel):
        name: str = Field(min_length=1)
        price: float = Field(gt=0)
        category: str = ""
        stock: int = Field(default=0, ge=0)
        spec: str = ""
        desc: str = ""

    @router.post("/products")
    async def create_product(body: ProductCreate) -> dict:
        p = await product_store.create(
            name=body.name,
            price=body.price,
            category=body.category,
            stock=body.stock,
            spec=body.spec,
            desc=body.desc,
        )
        return {"product": p}

    @router.put("/products/{sku}")
    async def update_product(sku: str, body: ProductCreate) -> dict:
        p = await product_store.update(
            sku,
            name=body.name,
            price=body.price,
            category=body.category,
            stock=body.stock,
            spec=body.spec,
            desc=body.desc,
        )
        if not p:
            raise HTTPException(status_code=404, detail="商品不存在")
        return {"product": p}

    @router.delete("/products/{sku}")
    async def delete_product(sku: str) -> dict:
        ok = await product_store.delete(sku)
        if not ok:
            raise HTTPException(status_code=404, detail="商品不存在")
        return {"ok": True}

    # ------------------------------------------------------------------
    # 订单 / 用户概览
    # ------------------------------------------------------------------
    @router.get("/orders")
    async def orders() -> dict:
        items = await order_store.all()
        items.sort(key=lambda o: o.get("created_at", ""), reverse=True)
        return {"orders": items}

    @router.get("/users")
    async def users() -> dict:
        return {"users": await user_store.all()}

    return router


def now_cn_reply() -> str:
    from .store.mock_store import now_cn

    return now_cn()
