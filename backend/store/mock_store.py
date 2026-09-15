# -*- coding: utf-8 -*-
"""JSON 文件数据仓库。

以"读-改-写 + 文件锁"的方式模拟电商后台（订单 / 用户 / FAQ / 工单）。
接口设计与真实订单中台保持一致，接入真实系统时仅替换本模块实现即可。
"""
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_TZ_CN = timezone(timedelta(hours=8))

# 文件级互斥锁（进程内）
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(name: str) -> asyncio.Lock:
    if name not in _locks:
        _locks[name] = asyncio.Lock()
    return _locks[name]


def now_cn() -> str:
    """当前北京时间，精确到分钟。"""
    return datetime.now(_TZ_CN).strftime("%Y-%m-%d %H:%M")


async def _read_json(name: str) -> Any:
    path = _DATA_DIR / name
    text = await asyncio.to_thread(path.read_text, encoding="utf-8-sig")
    return json.loads(text)


async def _write_json(name: str, data: Any) -> None:
    path = _DATA_DIR / name
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    await asyncio.to_thread(path.write_text, payload, "utf-8")


class OrderStore:
    """订单数据访问。"""

    def __init__(self, filename: str = "orders.json") -> None:
        self._filename = filename

    async def all(self) -> list[dict]:
        return await _read_json(self._filename)

    async def find_by_id(self, order_id: str) -> dict | None:
        order_id = order_id.strip().upper()
        for order in await self.all():
            if str(order.get("order_id", "")).upper() == order_id:
                return order
        return None

    async def find_by_user(self, user_id: str) -> list[dict]:
        return [
            order
            for order in await self.all()
            if order.get("user_id") == user_id
        ]

    async def find_by_phone_tail(self, phone_tail: str) -> list[dict]:
        phone_tail = phone_tail.strip()[-4:]
        return [
            order
            for order in await self.all()
            if str(order.get("phone_tail", "")) == phone_tail
        ]

    async def create(
        self,
        *,
        user_id: str,
        phone_tail: str,
        items: list[dict],
        amount: float,
        address: str,
    ) -> dict:
        """创建一笔新订单（默认待付款，无物流）。返回订单字典。"""
        async with _lock_for(self._filename):
            orders = await _read_json(self._filename)
            nums = []
            for o in orders:
                m = re.search(r"(\d+)\s*$", str(o.get("order_id", "")))
                if m:
                    nums.append(int(m.group(1)))
            seq = 1 + max(nums, default=0)
            order = {
                "order_id": "SO" + datetime.now(_TZ_CN).strftime("%Y%m%d") + f"{seq:03d}",
                "user_id": user_id,
                "phone_tail": phone_tail,
                "items": items,
                "amount": round(amount, 2),
                "status": "待付款",
                "created_at": now_cn(),
                "address": address,
                "logistics": None,
            }
            orders.append(order)
            await _write_json(self._filename, orders)
        return order

    async def update(self, order_id: str, **fields: Any) -> dict | None:
        async with _lock_for(self._filename):
            orders = await _read_json(self._filename)
            for order in orders:
                if str(order.get("order_id", "")).upper() == order_id.upper():
                    order.update(fields)
                    await _write_json(self._filename, orders)
                    return order
        return None



class ProductStore:
    """商品目录访问（products.json）。"""

    def __init__(self, filename: str = "products.json") -> None:
        self._filename = filename

    async def all(self) -> list[dict]:
        return await _read_json(self._filename)

    async def find_by_sku(self, sku: str) -> dict | None:
        sku = sku.strip().upper()
        for p in await self.all():
            if str(p.get("sku", "")).upper() == sku:
                return p
        return None

    async def search(self, keyword: str = "", category: str = "") -> list[dict]:
        keyword = (keyword or "").strip().lower()
        category = (category or "").strip()
        hits = []
        for p in await self.all():
            if category and p.get("category", "") != category:
                continue
            if keyword:
                hay = (p.get("name", "") + " " + p.get("desc", "") + " " + p.get("category", "")).lower()
                if keyword not in hay:
                    continue
            hits.append(p)
        return hits

    async def create(self, name: str, price: float, category: str = "", stock: int = 0, spec: str = "", desc: str = "") -> dict:
        p = {
            "sku": "P" + uuid.uuid4().hex[:4].upper(),
            "name": name,
            "category": category,
            "price": round(float(price), 2),
            "stock": int(stock),
            "spec": spec or "",
            "desc": desc or "",
        }
        async with _lock_for(self._filename):
            items = await _read_json(self._filename)
            items.append(p)
            await _write_json(self._filename, items)
        return p

    async def update(self, sku: str, **fields) -> dict | None:
        sku = sku.strip().upper()
        async with _lock_for(self._filename):
            items = await _read_json(self._filename)
            for p in items:
                if str(p.get("sku", "")).upper() == sku:
                    p.update(fields)
                    await _write_json(self._filename, items)
                    return p
        return None

    async def delete(self, sku: str) -> bool:
        sku = sku.strip().upper()
        async with _lock_for(self._filename):
            items = await _read_json(self._filename)
            kept = [p for p in items if str(p.get("sku", "")).upper() != sku]
            if len(kept) == len(items):
                return False
            await _write_json(self._filename, kept)
        return True


class CartStore:
    """购物车访问（cart.json，按用户 phone_tail 维度的购物车中商品与数量）。"""

    def __init__(self, filename: str = "cart.json") -> None:
        self._filename = filename

    async def _data(self) -> dict:
        text = await asyncio.to_thread((_DATA_DIR / self._filename).read_text, encoding="utf-8-sig")
        return json.loads(text) or {}

    async def get(self, phone_tail: str) -> list[dict]:
        phone_tail = phone_tail.strip()[-4:]
        return (await self._data()).get(phone_tail, [])

    async def add(self, phone_tail: str, sku: str, qty: int = 1) -> list[dict]:
        phone_tail = phone_tail.strip()[-4:]
        sku = sku.strip().upper()
        qty = max(1, int(qty))
        async with _lock_for(self._filename):
            data = await self._data()
            rows = data.setdefault(phone_tail, [])
            found = None
            for row in rows:
                if str(row.get("sku", "")).upper() == sku:
                    found = row
                    break
            if found:
                found["qty"] += qty
            else:
                rows.append({"sku": sku, "qty": qty})
            await _write_json(self._filename, data)
        return rows

    async def update(self, phone_tail: str, sku: str, qty: int) -> list[dict]:
        phone_tail = phone_tail.strip()[-4:]
        sku = sku.strip().upper()
        qty = int(qty)
        async with _lock_for(self._filename):
            data = await self._data()
            rows = data.setdefault(phone_tail, [])
            if qty <= 0:
                rows = [row for row in rows if str(row.get("sku", "")).upper() != sku]
            else:
                for row in rows:
                    if str(row.get("sku", "")).upper() == sku:
                        row["qty"] = qty
            data[phone_tail] = rows
            await _write_json(self._filename, data)
        return rows

    async def remove(self, phone_tail: str, sku: str) -> list[dict]:
        return await self.update(phone_tail, sku, 0)

    async def clear(self, phone_tail: str) -> None:
        phone_tail = phone_tail.strip()[-4:]
        async with _lock_for(self._filename):
            data = await self._data()
            data.pop(phone_tail, None)
            await _write_json(self._filename, data)

class UserStore:
    """用户数据访问。"""

    def __init__(self, filename: str = "users.json") -> None:
        self._filename = filename

    async def all(self) -> list[dict]:
        return await _read_json(self._filename)

    async def find_by_id(self, user_id: str) -> dict | None:
        for user in await self.all():
            if user.get("user_id") == user_id:
                return user
        return None


class FaqStore:
    """FAQ 知识库访问。"""

    def __init__(self, filename: str = "faq.json") -> None:
        self._filename = filename

    async def all(self) -> list[dict]:
        return await _read_json(self._filename)

    async def find_by_id(self, faq_id: str) -> dict | None:
        faq_id = faq_id.strip()
        for entry in await self.all():
            if entry.get("id") == faq_id:
                return entry
        return None

    async def create(self, title: str, keywords: list[str], content: str) -> dict:
        entry = {
            "id": "faq_" + uuid.uuid4().hex[:8],
            "title": title,
            "keywords": keywords or [],
            "content": content,
        }
        async with _lock_for(self._filename):
            entries = await _read_json(self._filename)
            entries.append(entry)
            await _write_json(self._filename, entries)
        return entry

    async def update(self, faq_id: str, **fields) -> dict | None:
        async with _lock_for(self._filename):
            entries = await _read_json(self._filename)
            for entry in entries:
                if entry.get("id") == faq_id:
                    entry.update(fields)
                    await _write_json(self._filename, entries)
                    return entry
        return None

    async def delete(self, faq_id: str) -> bool:
        async with _lock_for(self._filename):
            entries = await _read_json(self._filename)
            kept = [e for e in entries if e.get("id") != faq_id]
            if len(kept) == len(entries):
                return False
            await _write_json(self._filename, kept)
        return True


class TicketStore:
    """售后 / 投诉工单存储。"""

    def __init__(self, filename: str = "tickets.json") -> None:
        self._filename = filename

    async def all(self) -> list[dict]:
        return await _read_json(self._filename)

    async def create(
        self,
        title: str,
        detail: str,
        *,
        category: str,
        contact: str = "",
        order_id: str = "",
        priority: str = "normal",
    ) -> dict:
        ticket = {
            "ticket_id": "TK" + uuid.uuid4().hex[:10].upper(),
            "title": title,
            "detail": detail,
            "category": category,
            "contact": contact,
            "order_id": order_id,
            "priority": priority,
            "status": "待处理",
            "created_at": now_cn(),
        }
        async with _lock_for(self._filename):
            tickets = await _read_json(self._filename)
            tickets.append(ticket)
            await _write_json(self._filename, tickets)
        return ticket

    async def find_by_id(self, ticket_id: str) -> dict | None:
        ticket_id = ticket_id.strip().upper()
        for t in await self.all():
            if str(t.get("ticket_id", "")).upper() == ticket_id:
                return t
        return None

    async def update(self, ticket_id: str, **fields) -> dict | None:
        async with _lock_for(self._filename):
            tickets = await _read_json(self._filename)
            for t in tickets:
                if str(t.get("ticket_id", "")).upper() == ticket_id.strip().upper():
                    t.update(fields)
                    await _write_json(self._filename, tickets)
                    return t
        return None


order_store = OrderStore()
user_store = UserStore()
faq_store = FaqStore()
ticket_store = TicketStore()
product_store = ProductStore()
cart_store = CartStore()
