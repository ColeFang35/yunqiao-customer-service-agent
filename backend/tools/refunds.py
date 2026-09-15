# -*- coding: utf-8 -*-
"""售后 / 退款 / 取消订单工具。"""
from ..store.mock_store import now_cn, order_store, ticket_store

_REFUNDABLE_STATUS = {"待发货", "已发货", "已签收"}
_UNREFUNDABLE_REASON = {
    "待付款": "订单尚未付款，可直接取消订单，无需退款",
    "已完成": "订单已完成超过售后时效，如有质量问题可创建工单由专员处理",
    "已取消": "订单已取消，无需退款",
    "退款中": "该订单退款处理中，请耐心等待",
    "已退款": "该订单已完成退款，请勿重复申请",
}


async def check_refund_policy(order_id: str) -> dict:
    """检查指定订单是否满足退款 / 退货条件，并给出可操作的建议。发起退款前应先调用本工具确认资格。

    Args:
        order_id: 完整的订单号，例如 "SO20260810001"。
    """
    order = await order_store.find_by_id(order_id)
    if not order:
        return {"found": False, "message": f"未查询到订单 {order_id.strip().upper()}"}

    status = order.get("status", "")
    if status in _REFUNDABLE_STATUS:
        suggestion = (
            "订单未发货，可全额退款，预计 1-3 个工作日原路退回"
            if status == "待发货"
            else "可申请退货退款：商品完好且签收 7 天内支持七天无理由；商家签收退货并验收后 1-3 个工作日退款"
        )
        return {
            "found": True,
            "refundable": True,
            "order_status": status,
            "amount": order.get("amount"),
            "rule": suggestion,
        }
    return {
        "found": True,
        "refundable": False,
        "order_status": status,
        "reason": _UNREFUNDABLE_REASON.get(status, "当前状态不支持退款"),
    }


async def apply_refund(order_id: str, reason: str) -> dict:
    """为订单发起退款 / 退货退款申请（写操作）。仅当用户明确表达要退款时调用，调用前必须先告诉用户退款金额并征得同意。

    Args:
        order_id: 完整的订单号。
        reason: 用户说明的退款原因，例如 "拍错了" 或 "商品破损"。
    """
    order = await order_store.find_by_id(order_id)
    if not order:
        return {"found": False, "message": f"未查询到订单 {order_id.strip().upper()}"}

    status = order.get("status", "")
    if status not in _REFUNDABLE_STATUS:
        return {
            "success": False,
            "message": _UNREFUNDABLE_REASON.get(status, "当前状态不支持退款"),
        }

    refund_type = "仅退款" if status == "待发货" else "退货退款"
    ticket = await ticket_store.create(
        title=f"退款申请（{refund_type}）",
        detail=f"订单 {order['order_id']} 申请{refund_type}，原因：{reason}",
        category="refund",
        order_id=order["order_id"],
        priority="high",
    )
    await order_store.update(order_id, status="退款中")
    return {
        "success": True,
        "refund_type": refund_type,
        "amount": order.get("amount"),
        "ticket_id": ticket["ticket_id"],
        "message": (
            f"{refund_type}申请已提交（工单 {ticket['ticket_id']}），"
            f"金额 {order.get('amount')} 元。待商家审核后 1-3 个工作日原路退回"
        ),
    }


async def cancel_order(order_id: str, reason: str = "") -> dict:
    """取消未发货订单（写操作）。仅「待付款 / 待发货」状态可取消；已发货订单请改用退款流程。

    Args:
        order_id: 完整的订单号。
        reason: 取消原因，可为空。
    """
    order = await order_store.find_by_id(order_id)
    if not order:
        return {"found": False, "message": f"未查询到订单 {order_id.strip().upper()}"}

    status = order.get("status", "")
    if status not in {"待付款", "待发货"}:
        return {
            "success": False,
            "message": f"订单当前状态为「{status}」，无法直接取消，可走退款流程",
        }

    updated = await order_store.update(
        order_id,
        status="已取消",
        cancelled_at=now_cn(),
        cancel_reason=reason,
    )
    return {
        "success": True,
        "order_id": updated["order_id"],
        "message": (
            f"订单 {updated['order_id']} 已取消；"
            + ("实付金额将 1-3 个工作日原路退回" if status == "待发货" else "未付款无需退款")
        ),
    }