# -*- coding: utf-8 -*-
"""订单与物流查询工具。"""
from ..store.mock_store import order_store


def _brief(order: dict) -> dict:
    """将订单精简为适合大模型阅读的字段。"""
    return {
        "order_id": order.get("order_id"),
        "items": order.get("items"),
        "amount": order.get("amount"),
        "status": order.get("status"),
        "created_at": order.get("created_at"),
        "signed_at": order.get("signed_at"),
        "address": order.get("address"),
    }


async def query_order(order_id: str) -> dict:
    """按订单号查询订单详情（商品、金额、状态、收货地址）。订单号形如 SO20260810001，可从用户消息中获取；若没有订单号，请先用 list_recent_orders 找到对应订单。

    Args:
        order_id: 完整的订单号，例如 "SO20260810001"。
    """
    order = await order_store.find_by_id(order_id)
    if not order:
        return {
            "found": False,
            "message": f"未查询到订单 {order_id.strip().upper()}，请核对订单号后重试",
        }
    return {"found": True, "order": _brief(order)}


async def list_recent_orders(phone_tail: str) -> dict:
    """按收货手机号后四位列出该用户最近的订单（最多 5 条），用于在用户提供手机号后定位订单。

    Args:
        phone_tail: 收货手机号后四位数字，例如 "3721"。
    """
    orders = await order_store.find_by_phone_tail(phone_tail)
    orders = sorted(orders, key=lambda o: o.get("created_at", ""), reverse=True)
    if not orders:
        return {
            "found": False,
            "message": f"未找到手机号后四位为 {phone_tail} 的订单，请确认后重试",
        }
    return {
        "found": True,
        "count": len(orders),
        "orders": [_brief(o) for o in orders[:5]],
    }


async def track_logistics(order_id: str) -> dict:
    """查询订单的物流信息与轨迹（承运公司、运单号、最新物流节点）。仅已发货订单有物流信息。

    Args:
        order_id: 完整的订单号，例如 "SO20260810001"。
    """
    order = await order_store.find_by_id(order_id)
    if not order:
        return {"found": False, "message": f"未查询到订单 {order_id.strip().upper()}"}

    logistics = order.get("logistics")
    if not logistics:
        return {
            "found": True,
            "has_logistics": False,
            "order_status": order.get("status"),
            "message": f"订单当前状态为「{order.get('status')}」，暂无物流信息",
        }

    timeline = logistics.get("timeline") or []
    return {
        "found": True,
        "has_logistics": True,
        "order_status": order.get("status"),
        "company": logistics.get("company"),
        "tracking_no": logistics.get("tracking_no"),
        "latest": timeline[-1] if timeline else None,
        "timeline": timeline[-5:],
    }