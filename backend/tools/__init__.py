# -*- coding: utf-8 -*-
"""客服业务工具集。"""
from agentscope.tool import FunctionTool, Toolkit

from .knowledge import search_faq
from .orders import list_recent_orders, query_order, track_logistics
from .refunds import apply_refund, cancel_order, check_refund_policy
from .support import create_ticket, query_user_profile, transfer_to_human
from .shop import (
    add_to_cart,
    list_products,
    pay_order,
    place_order,
    query_product,
    update_cart_item,
    view_cart,
)


def build_toolkit() -> Toolkit:
    """构建面向客服 Agent 的 Toolkit（AgentScope 2.0 工具层）。"""
    return Toolkit(
        tools=[
            FunctionTool(query_order, is_read_only=True),
            FunctionTool(list_recent_orders, is_read_only=True),
            FunctionTool(track_logistics, is_read_only=True),
            FunctionTool(query_user_profile, is_read_only=True),
            FunctionTool(search_faq, is_read_only=True),
            FunctionTool(check_refund_policy, is_read_only=True),
            FunctionTool(apply_refund),
            FunctionTool(cancel_order),
            FunctionTool(create_ticket),
            FunctionTool(transfer_to_human),
            FunctionTool(list_products, is_read_only=True),
            FunctionTool(query_product, is_read_only=True),
            FunctionTool(view_cart, is_read_only=True),
            FunctionTool(add_to_cart),
            FunctionTool(update_cart_item),
            FunctionTool(place_order),
            FunctionTool(pay_order),
        ],
    )


__all__ = ["build_toolkit"]