# -*- coding: utf-8 -*-
"""用户档案、工单与转人工工具。"""
from ..store.mock_store import order_store, ticket_store, user_store


async def query_user_profile(phone_tail: str) -> dict:
    """按手机号后四位查询用户档案（昵称、会员等级、积分、优惠券），用于个性化服务与会员问题解答。

    Args:
        phone_tail: 手机号后四位数字。
    """
    orders = await order_store.find_by_phone_tail(phone_tail)
    if not orders:
        return {"found": False, "message": f"未找到手机号后四位为 {phone_tail} 的用户"}
    user = await user_store.find_by_id(orders[0]["user_id"])
    if not user:
        return {"found": False, "message": "用户档案不存在"}
    return {
        "found": True,
        "nickname": user.get("nickname"),
        "member_level": user.get("member_level"),
        "points": user.get("points"),
        "coupons": user.get("coupons"),
    }


async def create_ticket(
    title: str,
    detail: str,
    category: str = "other",
    contact: str = "",
) -> dict:
    """创建售后 / 投诉 / 建议工单，由专人在工作时段跟进处理。遇到无法在线解决的问题（如丢件赔付、商品质量纠纷、信息变更）时使用。

    Args:
        title: 工单标题，一句话概括问题。
        detail: 问题详细描述，需要包含订单号等关键信息。
        category: 工单类型，取值: complaint(投诉) / suggestion(建议) / quality(质量问题) / logistics(物流异常) / other。
        contact: 用户留下的联系方式（手机号或邮箱），可为空。
    """
    if category not in {"complaint", "suggestion", "quality", "logistics", "other"}:
        category = "other"
    category_cn = {
        "complaint": "投诉",
        "suggestion": "建议",
        "quality": "质量问题",
        "logistics": "物流异常",
        "other": "其他",
    }[category]
    ticket = await ticket_store.create(
        title=title,
        detail=detail,
        category=category,
        contact=contact,
    )
    return {
        "success": True,
        "ticket_id": ticket["ticket_id"],
        "category": category_cn,
        "created_at": ticket["created_at"],
        "message": (
            f"工单 {ticket['ticket_id']} 已创建（{category_cn}类），"
            "专员将在工作时段（每日 09:00-22:00）2 小时内联系您处理"
        ),
    }


async def transfer_to_human(reason: str) -> dict:
    """转接人工客服。当用户明确要求人工服务，或问题超出智能客服能力范围（复杂纠纷、情绪激烈需要安抚）时调用。转接后请停止自动答复。

    Args:
        reason: 转人工的原因简述。
    """
    ticket = await ticket_store.create(
        title="转人工客服请求",
        detail=f"转人工原因：{reason}",
        category="other",
        priority="high",
    )
    return {
        "success": True,
        "ticket_id": ticket["ticket_id"],
        "queue_position": 2,
        "human_service_hours": "每日 09:00-22:00",
        "message": (
            f"已为您登记转人工服务（排队单号 {ticket['ticket_id']}，当前前方约 2 人）。"
            "人工客服服务时间为每日 09:00-22:00，接入前您仍可以继续向我提问"
        ),
    }