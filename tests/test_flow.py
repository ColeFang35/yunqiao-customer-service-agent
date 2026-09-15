# -*- coding: utf-8 -*-
"""端到端流程测试（使用 AgentScope 离线规则模型，无网络依赖）。"""
import json
import os
import shutil
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "backend" / "data"


@pytest.fixture(autouse=True)
def _preserve_data(tmp_path):
    """备份并在结束时还原 JSON 数据，避免测试污染演示数据。"""
    backup = tmp_path / "data_bak"
    backup.mkdir()
    for name in ("orders.json", "tickets.json", "faq.json", "users.json", "cart.json", "products.json"):
        shutil.copy(_DATA / name, backup / name)
    yield
    for name in ("orders.json", "tickets.json", "faq.json", "users.json", "cart.json", "products.json"):
        shutil.copy(backup / name, _DATA / name)


def _make_agent(session_id: str = "test_sess"):
    os.environ["MODEL_PROVIDER"] = "mock"
    from backend.config import get_settings
    from backend.agent_factory import build_customer_service_agent
    from backend.mock_model import OfflineChatModel

    settings = get_settings()
    return build_customer_service_agent(
        settings,
        session_id,
        model=OfflineChatModel(),
    )


async def _collect_reply(agent, message: str):
    """驱动 Agent 回复一轮，返回 (调用的工具列表, 最终文本)。"""
    from agentscope.message import UserMsg, TextBlock
    from agentscope.event import (
        TextBlockDeltaEvent,
        ToolCallStartEvent,
        ReplyEndEvent,
    )

    text: list[str] = []
    tools: list[str] = []
    async for evt in agent.reply_stream(
        UserMsg(name="用户", content=[TextBlock(text=message)])
    ):
        if isinstance(evt, TextBlockDeltaEvent):
            text.append(evt.delta)
        elif isinstance(evt, ToolCallStartEvent):
            tools.append(evt.tool_call_name)
        elif isinstance(evt, ReplyEndEvent):
            assert str(evt.finished_reason) == "completed"
    return tools, "".join(text)


async def test_query_logistics():
    agent = _make_agent()
    tools, reply = await _collect_reply(agent, "帮我查一下订单 SO20260810001 的物流")
    assert "track_logistics" in tools
    assert "云雀速运" in reply
    assert "SO20260810001" in reply or "YL8820472631" in reply


async def test_refund_chain_updates_store():
    agent = _make_agent()
    tools, reply = await _collect_reply(agent, "我想把 SO20260812003 这个订单退了")
    assert "check_refund_policy" in tools
    assert "apply_refund" in tools
    assert "提交" in reply or "TKF" in reply

    # 订单应被标记为退款中
    from backend.store.mock_store import order_store

    order = await order_store.find_by_id("SO20260812003")
    assert order["status"] == "退款中"


async def test_faq_search():
    agent = _make_agent()
    tools, reply = await _collect_reply(agent, "退货规则是什么？")
    assert "search_faq" in tools
    assert "七天" in reply


async def test_transfer_human():
    agent = _make_agent()
    tools, reply = await _collect_reply(agent, "我要转人工客服")
    assert "transfer_to_human" in tools
    assert "排队" in reply or "转人工" in reply




async def test_shopping_add_and_view_cart():
    agent = _make_agent()
    tools, reply = await _collect_reply(agent, "手机号后四位 3721，把 P1002 加进购物车")
    assert "add_to_cart" in tools
    assert "购物车" in reply

    agent2 = _make_agent("test_sess2")
    tools2, reply2 = await _collect_reply(agent2, "手机号后四位 3721，看看我的购物车")
    assert "view_cart" in tools2
    # 购物车渲染行小计：保温焖烧杯 单价129 × 2 = ¥258
    assert "保温焖烧杯" in reply2
    assert "258" in reply2


async def test_shopping_checkout_and_pay():
    agent = _make_agent()
    # 先加一件商品进购物车
    await _collect_reply(agent, "手机号后四位 3721，把 P1001 加进购物车")

    # 按购物车下单
    tools, reply = await _collect_reply(agent, "手机号后四位 3721，按购物车下单，地址用文三路 199 号")
    assert "place_order" in tools
    assert "SO" in reply

    from backend.store.mock_store import order_store
    orders = await order_store.find_by_phone_tail("3721")
    new_order = max((o for o in orders if o.get("status") == "待付款"), key=lambda o: o["created_at"])
    oid = new_order["order_id"]

    # 支付
    tools2, reply2 = await _collect_reply(agent, f"支付订单 {oid}")
    assert "pay_order" in tools2
    assert "支付" in reply2 or "发货" in reply2
    paid = await order_store.find_by_id(oid)
    assert paid["status"] == "待发货"


# ---------------- HTTP 接口冒烟 ----------------

@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


async def test_config_api():
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_import_app()),
        base_url="http://test",
    ) as client:
        r = await client.get("/api/config")
        assert r.status_code == 200
        data = r.json()
        assert data["agent_name"]
        assert data["quick_prompts"]


async def test_chat_api_sse():
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_import_app()),
        base_url="http://test",
    ) as client:
        r = await client.post(
            "/api/chat",
            json={"message": "运费怎么算？"},
        )
        assert r.status_code == 200
        body = r.text
        assert "event: meta" in body
        assert "event: delta" in body
        assert "event: done" in body


async def test_debug_api_trace_pipeline():
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_import_app()),
        base_url="http://test",
    ) as client:
        # 先跑一轮对话产生 trace（物流查询必走工具调用）
        r = await client.post(
            "/api/chat",
            json={"message": "帮我查一下订单 SO20260810001 的物流"},
        )
        assert r.status_code == 200

        s = await client.get("/api/debug/summary")
        assert s.status_code == 200
        assert s.json()["total"] >= 1

        t = await client.get("/api/debug/traces")
        traces = t.json()["traces"]
        assert traces

        detail = await client.get(f"/api/debug/traces/{traces[0]['id']}")
        assert detail.status_code == 200
        trace = detail.json()["trace"]
        kinds = {sp["kind"] for sp in trace["spans"]}
        assert "tool" in kinds
        assert any(e["name"] == "reply_end" for e in trace["events"])

        # 火焰图：单条与聚合两种模式
        f_one = await client.get(f"/api/debug/flamegraph?trace_id={traces[0]['id']}")
        assert f_one.status_code == 200
        assert f_one.json()["flame"]["children"]
        f_all = await client.get("/api/debug/flamegraph")
        assert f_all.json()["flame"]["children"]

        c = await client.post("/api/debug/clear")
        assert c.status_code == 200
        assert (await client.get("/api/debug/summary")).json()["total"] == 0


def _import_app():
    os.environ["MODEL_PROVIDER"] = "mock"
    import backend.server  # noqa: F401
    return backend.server.app
async def test_multi_turn_coreference_refund():
    """多轮追问：第二轮用指代（不带订单号），应继承上文订单号进入退款链路。"""
    agent = _make_agent("sess_multi_refund")
    tools1, _reply1 = await _collect_reply(agent, "帮我查一下订单 SO20260810001 的物流")
    assert "track_logistics" in tools1

    tools2, _reply2 = await _collect_reply(agent, "帮我退了")
    assert "check_refund_policy" in tools2
    assert "apply_refund" in tools2

    from backend.store.mock_store import order_store

    order = await order_store.find_by_id("SO20260810001")
    assert order["status"] == "退款中"


async def test_multi_turn_ack_confirm_refund():
    """简短确认：上一轮给出「确认退款」的挂起意图，回"好的"应推进退款。"""
    agent = _make_agent("sess_multi_ack")
    tools1, reply1 = await _collect_reply(agent, "SO20260812003 能退吗？")
    assert "check_refund_policy" in tools1
    assert "确认退款" in reply1

    tools2, _reply2 = await _collect_reply(agent, "好的")
    assert "apply_refund" in tools2


async def test_multi_turn_bare_phone_answer():
    """裸数字回答：客服上一轮索要手机号后四位，用户只回四位数字应继续购物车上下文。"""
    agent = _make_agent("sess_multi_phone")
    tools1, reply1 = await _collect_reply(agent, "看看我的购物车")
    assert "view_cart" not in tools1
    assert "手机号" in reply1

    tools2, _reply2 = await _collect_reply(agent, "3721")
    assert "view_cart" in tools2


async def test_multi_turn_inherit_phone_for_logistics():
    """实体继承：手机号在上一轮给出，第二轮直接追问物流不再索要手机号。"""
    agent = _make_agent("sess_multi_inherit")
    tools1, _reply1 = await _collect_reply(agent, "手机号后四位 3721，看下我最近的订单")
    assert "list_recent_orders" in tools1

    tools2, _reply2 = await _collect_reply(agent, "在途那笔到哪儿了？")
    assert "track_logistics" in tools2

async def test_shop_price_filter_under_budget():
    """多条件：让 mock 按价格上限 (500以内) 过滤商品并复述。"""
    agent = _make_agent("sess_price_fit")
    tools, reply = await _collect_reply(agent, "推荐 500 以内的耳机")
    assert "list_products" in tools
    assert "500" in reply
    assert ("P1001" in reply and "以内" in reply) or "暂无" in reply


async def test_shop_price_filter_no_result():
    """预算过低时的兜底：明确告知并推荐最接近的在售款。"""
    agent = _make_agent("sess_price_none")
    tools, reply = await _collect_reply(agent, "推荐 300 以内的耳机")
    assert "list_products" in tools
    assert "300" in reply
    assert "实在" not in reply  # mock 不应假装存在
    assert ("P1001" in reply)  # 推荐最接近的耳机
