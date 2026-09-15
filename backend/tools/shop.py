# -*- coding: utf-8 -*-
"""售前购买工具：商品浏览 / 购物车 / 下单 / 支付。"""
from ..store.mock_store import cart_store, order_store, product_store, user_store


def _product_brief(p: dict) -> dict:
    """商品精简字段。"""
    return {
        "sku": p.get("sku"),
        "name": p.get("name"),
        "category": p.get("category"),
        "price": p.get("price"),
        "stock": p.get("stock"),
        "spec": p.get("spec", ""),
    }


async def list_products(keyword: str = "", category: str = "") -> dict:
    """浏览 / 搜索在售商品列表。用户想买东西、问有没有某类商品时使用；可给出关键词或商品分类缩小范围。

    Args:
        keyword: 商品名称或描述中的关键词，例如 "耳机"、"保温杯"，可为空。
        category: 商品分类，如 "数码影音" / "女装" / "家居日用" / "美妆个护" / "家居家纺"，可为空。
    """
    products = await product_store.search(keyword=keyword, category=category)
    if not products:
        return {
            "found": False,
            "message": "抱歉，没有找到符合条件的商品，您可以换个关键词，或提供手机号后四位由客服为您推荐",
        }
    return {
        "found": True,
        "count": len(products),
        "products": [_product_brief(p) for p in products],
    }


async def query_product(sku: str) -> dict:
    """查询单个商品的详细信息（价格、库存、规格、介绍）。用户在选商品、问价格 / 库存时使用。

    Args:
        sku: 商品货号，例如 "P1001"，可从商品列表中获得。
    """
    p = await product_store.find_by_sku(sku)
    if not p:
        return {"found": False, "message": f"未找到货号 {sku.strip().upper()} 的商品，请核对后重试"}
    return {"found": True, "product": p}


async def view_cart(phone_tail: str) -> dict:
    """查看用户购物车：汇总商品、数量与总金额。购物车按收货手机号后四位定位，需先取得该信息。

    Args:
        phone_tail: 收货手机号后四位，例如 "3721"。
    """
    phone_tail = phone_tail.strip()[-4:]
    rows = await cart_store.get(phone_tail)
    if not rows:
        return {"found": True, "empty": True, "message": "购物车还是空的呢，需要我先帮您挑几件商品吗？"}
    detail = []
    total = 0.0
    for row in rows:
        p = await product_store.find_by_sku(row["sku"])
        if not p:
            continue
        subtotal = p["price"] * row["qty"]
        total += subtotal
        detail.append({
            "sku": p["sku"],
            "name": p["name"],
            "price": p["price"],
            "qty": row["qty"],
            "subtotal": round(subtotal, 2),
        })
    return {
        "found": True,
        "empty": False,
        "total": round(total, 2),
        "items": detail,
    }


async def add_to_cart(phone_tail: str, sku: str, qty: int = 1) -> dict:
    """把商品加入购物车（写操作）。用户确认要购买某商品时使用，需商品货号与手机号后四位。

    Args:
        phone_tail: 收货手机号后四位。
        sku: 商品货号，例如 "P1001"。
        qty: 购买数量，默认 1。
    """
    p = await product_store.find_by_sku(sku)
    if not p:
        return {"found": False, "message": f"未找到货号 {sku.strip().upper()} 的商品"}
    qty = max(1, int(qty))
    if p.get("stock", 0) < qty:
        return {"success": False, "message": f"「{p['name']}」库存仅剩 {p['stock']} 件，请减少数量"}
    await cart_store.add(phone_tail, sku, qty)
    return {
        "success": True,
        "sku": p["sku"],
        "name": p["name"],
        "qty": qty,
        "unit_price": p["price"],
        "message": f"已将「{p['name']}」x{qty} 加入购物车，需要的话我可以帮您结账下单",
    }


async def update_cart_item(phone_tail: str, sku: str, qty: int) -> dict:
    """修改购物车内某商品的数量；数量设为 0 则移除该商品（写操作）。

    Args:
        phone_tail: 收货手机号后四位。
        sku: 商品货号。
        qty: 新数量，0 表示移除。
    """
    await cart_store.update(phone_tail, sku, qty)
    cart = await view_cart(phone_tail)
    return {"success": True, "cart": cart}


async def place_order(
    phone_tail: str,
    address: str,
    items: list[dict] | None = None,
    use_coupon: bool = False,
) -> dict:
    """提交订单（写操作）。把购物车（或指定商品）转为待付款订单并返回订单号。下单前应确认收货地址与商品明细、金额。

    Args:
        phone_tail: 收货手机号后四位，用于定位用户与购物车。
        address: 收货地址，下单必需。
        items: 若为空则使用当前购物车；也可传入 [{\"sku\": \"P1001\", \"qty\": 1}] 直接指定商品。
        use_coupon: 是否使用用户可用优惠券，默认 False。
    """
    phone_tail = phone_tail.strip()[-4:]
    sku_rows = items if items else await cart_store.get(phone_tail)
    if not sku_rows:
        return {"success": False, "message": "购物车是空的，请先加入商品再下单"}

    detail, amount = [], 0.0
    for row in sku_rows:
        p = await product_store.find_by_sku(row["sku"])
        if not p:
            return {"success": False, "message": f"货号 {row['sku']} 的商品不存在，请移除后重试"}
        qty = max(1, int(row.get("qty", 1)))
        if p.get("stock", 0) < qty:
            return {"success": False, "message": f"「{p['name']}」库存不足"}
        subtotal = round(p["price"] * qty, 2)
        amount += subtotal
        detail.append({"name": p["name"], "qty": qty, "price": p["price"]})
    amount = round(amount, 2)

    coupon = None
    # 查找该用户的首张可用优惠券（标题匹配 "满X减Y"）
    orders = await order_store.find_by_phone_tail(phone_tail)
    user = await user_store.find_by_id(orders[0]["user_id"]) if orders else None
    if use_coupon and user:
        for c in user.get("coupons", []):
            parsed = _parse_coupon(c.get("title", ""))
            if parsed and amount >= parsed["threshold"]:
                amount = round(amount - parsed["discount"], 2)
                coupon = {"title": c["title"], "discount": parsed["discount"]}
                break

    user_id = user["user_id"] if user else ("U" + phone_tail)
    order = await order_store.create(
        user_id=user_id,
        phone_tail=phone_tail,
        items=detail,
        amount=amount,
        address=address,
    )
    # 下单成功后清空该用户购物车，避免残留商品导致下次结算金额翻倍
    await cart_store.clear(phone_tail)
    return {
        "success": True,
        "order_id": order["order_id"],
        "amount": order["amount"],
        "status": order["status"],
        "coupon": coupon,
        "message": (
            f"订单 {order['order_id']} 已生成，应付 {order['amount']} 元，"
            "确认后我可以帮您完成支付"
            + (f"（已使用优惠券：{coupon['title']}）" if coupon else "")
        ),
    }


def _parse_coupon(title: str) -> dict | None:
    """解析 "满X减Y" 形式的优惠券标题。"""
    import re

    m = re.search(r"满\s*(\d+)\s*减\s*(\d+)", title or "")
    if not m:
        return None
    return {"threshold": float(m.group(1)), "discount": float(m.group(2))}


async def pay_order(order_id: str, method: str = "模拟支付") -> dict:
    """完成待付款订单的支付（写操作），将订单状态从待付款更新为待发货。

    Args:
        order_id: 订单号，例如 "SO20260810001"。
        method: 支付方式，默认模拟支付。
    """
    order = await order_store.find_by_id(order_id)
    if not order:
        return {"found": False, "message": f"未查询到订单 {order_id.strip().upper()}"}
    if order.get("status") != "待付款":
        return {
            "success": False,
            "message": f"订单当前状态为「{order.get('status')}」，无需或无法支付",
        }
    await order_store.update(order_id, status="待发货")
    return {
        "success": True,
        "order_id": order["order_id"],
        "amount": order.get("amount"),
        "pay_method": method,
        "message": (
            f"订单 {order['order_id']} 已支付成功（{method}），"
            f"支付 {order.get('amount')} 元，商家将尽快为您发货"
        ),
    }
