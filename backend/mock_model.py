# -*- coding: utf-8 -*-
"""离线规则模型：无 API Key 时驱动完整 ReAct + 工具调用链路的演示模型。

严格遵循 AgentScope 2.0 ``ChatModelBase`` 契约：只产出 ``is_last=False`` 的
增量块（由框架的 ``__call__`` 包装器自动累积并补发 ``is_last=True``），
实现确定性的"意图识别 -> 工具调用 -> 结果复述"流程，用于：

- 无网络 / 无 API Key 环境下端到端体验系统（SSE 流式、工具调用、多会话）
- 自动化测试（不依赖外部服务）
- 多轮对话追问：订单号 / 手机号 / 货号等实体跨轮继承，
  短句与裸数字回答映射到上一轮挂起的意图

接入真实模型后本文件不会被使用。
"""
import asyncio
import json
import re
from typing import Any, AsyncGenerator

from agentscope.message import Msg
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock
from agentscope._utils._common import _generate_id

_ORDER_RE = re.compile(r"SO\d{8,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_SKU_RE = re.compile(r"P\d{3,4}", re.IGNORECASE)

_BARE_PHONE_RE = re.compile(r"^\s*(\d{4})\s*$")
_ACK_WORDS = ("好的", "好", "嗯", "嗯嗯", "确认", "是的", "对", "行", "可以",
              "就这样", "没问题", "ok", "OK")

_THINK = "思考过程："


def _tool_result_after(msgs: list[Msg]) -> list[tuple[str, dict]]:
    """扫描最后一条用户消息之后的所有工具结果，返回 (工具名, 解析后的 dict)。"""
    idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].role == "user":
            idx = i
            break
    tail = msgs[idx + 1 :] if idx >= 0 else msgs
    results: list[tuple[str, dict]] = []
    for m in tail:
        for block in m.get_content_blocks("tool_result"):
            output = block.output
            if isinstance(output, list):
                output = "".join(getattr(x, "text", "") for x in output)
            try:
                parsed = json.loads(output or "{}")
            except (ValueError, TypeError):
                parsed = {"_raw": output}
            results.append((block.name, parsed))
    return results


def _extract(text: str) -> dict[str, str]:
    """从用户文本中提取订单号 / 手机号后四位 / 退款原因。"""
    order_match = _ORDER_RE.search(text)
    order_id = order_match.group(0).upper() if order_match else ""
    masked = _ORDER_RE.sub("", text)
    tail_match = _PHONE_RE.search(masked)
    phone_tail = tail_match.group(1) if tail_match else ""
    reason = "用户申请退款"
    for kw, r in (
        ("破损", "商品破损/质量问题"),
        ("损坏", "商品损坏/质量问题"),
        ("坏了", "商品损坏"),
        ("质量", "质量问题"),
        ("拍错", "拍错了"),
        ("买错", "买错了"),
        ("下错", "下错单了"),
        ("不想要", "不想要了"),
        ("不喜欢", "不合适/不喜欢"),
        ("七天", "七天无理由退货"),
    ):
        if kw in text:
            reason = r
            break
    sku_match = _SKU_RE.search(text)
    sku = sku_match.group(0).upper() if sku_match else ""
    return {"order_id": order_id, "phone_tail": phone_tail, "reason": reason, "sku": sku}


def _history_entities(messages: list[Msg]) -> dict[str, str]:
    """从更早的对话上下文中回收最近提到的订单号 / 手机号后四位 / 货号（指代消解）。

    扫描最后一条用户消息之前的消息文本、工具入参与工具结果，
    让"帮我退了""那物流呢"这类指代上文的短句能继承实体，实现多轮对话。
    """
    boundary = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            boundary = i
            break
    parts: list[str] = []
    for m in messages[:boundary]:
        parts.append(m.get_text_content() or "")
        for block in m.get_content_blocks("tool_call"):
            parts.append(str(getattr(block, "input", "") or ""))
        for block in m.get_content_blocks("tool_result"):
            output = getattr(block, "output", "")
            if isinstance(output, list):
                output = " ".join(getattr(x, "text", "") for x in output)
            parts.append(str(output))
    blob = "\n".join(parts)

    orders = _ORDER_RE.findall(blob)
    skus = _SKU_RE.findall(blob)
    # 剔除订单号 / 货号 / 金额小数，避免干扰手机号匹配
    masked = _ORDER_RE.sub(" ", blob)
    masked = _SKU_RE.sub(" ", masked)
    masked = re.sub(r"\d+\.\d+", " ", masked)
    phones = [
        p for p in _PHONE_RE.findall(masked)
        if not re.fullmatch(r"(19|20)\d{2}", p)   # 排除时间戳里的年份
    ]
    return {
        "order_id": orders[-1].upper() if orders else "",
        "phone_tail": phones[-1] if phones else "",
        "sku": skus[-1].upper() if skus else "",
    }


def _pending_intent(messages: list[Msg]) -> str:
    """从上一条客服回复里识别"挂起的问题"，供短句 / 裸数字回答继承意图。"""
    boundary = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            boundary = i
            break
    for m in reversed(messages[:boundary]):
        if m.role != "assistant":
            continue
        text = m.get_text_content() or ""
        if not text:
            continue
        if "确认退款" in text or ("退款" in text and "确认" in text):
            return "refund"
        if "下单" in text:
            return "checkout"
        if "购物车" in text:
            return "cart"
        if "手机号" in text or "订单号" in text or "定位订单" in text:
            return "order"
        if "物流" in text or "快递" in text:
            return "logistics"
        return ""
    return ""


def _resolve_followup(intent: str, user_text: str, messages: list[Msg]) -> str:
    """多轮对话的意图修补：裸数字回答 / 简短确认映射到上一轮挂起的意图。"""
    text = (user_text or "").strip()
    if _BARE_PHONE_RE.fullmatch(text):
        pending = _pending_intent(messages)
        return pending or intent
    if intent in ("fallback", "greeting"):
        ack = text.strip("。！!？?~～ ")
        if ack in _ACK_WORDS and _pending_intent(messages) == "refund":
            return "refund"
    return intent


def _intent(text: str) -> str:
    """按优先级识别用户意图。"""
    if any(k in text for k in ("转人工", "人工服务", "人工客服", "真人", "活人", "人工处理")):
        # 排除"客服工作时间/客服电话"等问询类
        if re.search(r"(时间|几点|电话|客服小时)", text):
            return "faq"
        return "human"
    if any(k in text for k in ("投诉", "差评", "举报", "反馈问题", "我要投诉")):
        if "投诉" in text:
            return "complaint"
        return "suggestion"
    if any(k in text for k in ("退款", "退货", "退钱", "退掉", "退了吧", "退了", "退吧", "帮我退",
                           "能退", "可退", "不要了", "想退", "申请退")):
        if any(k in text for k in ("能退", "可以退", "行不行", "可退", "什么条件", "规则", "支持退")):
            return "refund_inquiry"
        return "refund"
    if any(k in text for k in ("取消订单", "帮我取消", "取消这个订单", "取消这单", "取消吧", "取消掉")):
        return "cancel"
    if any(k in text for k in ("物流", "快递", "到哪", "送到哪", "签收", "发货了", "发出了", "多久能到", "什么时候到", "派送")):
        return "logistics"
    if any(k in text for k in ("支付", "付款", "付钱", "完成支付")):
        return "pay"
    if any(k in text for k in ("下单", "结算", "提交订单", "确认订单", "去结算", "立即购买")):
        return "checkout"
    if any(k in text for k in ("购物车", "加购", "加入购物车", "加进购物车", "车里有")):
        return "cart"
    if any(k in text for k in ("推荐", "想买", "要买", "买个", "买点", "买什么", "有什么推荐", "商品目录", "看看有什么", "有哪些商品", "介绍一下", "挑一个", "帮选")):
        return "shop"
    if "订单" in text or _ORDER_RE.search(text) or (
        _PHONE_RE.search(text) and "订单" in text
    ):
        return "order"
    if any(k in text for k in ("会员", "积分", "优惠券", "升级", "权益", "等级")):
        return "profile"
    if any(
        k in text
        for k in ("运费", "包邮", "发货时间", "什么时候发", "发票", "改地址", "地址修改",
                  "客服时间", "几点上班", "冷链", "快递费", "邮费")
    ):
        return "faq"
    if any(k in text for k in ("你好", "您好", "在吗", "hi", "hello", "嗨", "早上好", "谢谢")):
        return "greeting"
    return "fallback"


class OfflineChatModel(ChatModelBase):
    """基于规则的离线演示模型。"""

    def __init__(self) -> None:
        super().__init__(
            credential=None,  # type: ignore[arg-type]
            model="offline-mock",
            parameters=None,  # type: ignore[arg-type]
            stream=True,
        )
        self.formatter = OpenAIChatFormatter()

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: Any | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[ChatResponse, None]:
        del model_name, tools, tool_choice, kwargs
        return self._respond(messages)

    # ------------------------------------------------------------------
    # 决策与回复
    # ------------------------------------------------------------------
    async def _respond(
        self,
        messages: list[Msg],
    ) -> AsyncGenerator[ChatResponse, None]:
        resp_id = _generate_id()
        text_id = resp_id + "_t"
        think_id = resp_id + "_th"
        call_id = "call_" + _generate_id()

        user_text = ""
        for m in reversed(messages):
            if m.role == "user":
                user_text = m.get_text_content() or ""
                break

        results = _tool_result_after(messages)
        intent = _intent(user_text)
        ex = _extract(user_text)
        intent = _resolve_followup(intent, user_text, messages)

        # 多轮对话：当前消息缺实体时，继承上下文里最近提到的订单号/手机号/货号
        current_order = ex.get("order_id")
        history_ex = _history_entities(messages)
        for key in ("order_id", "phone_tail", "sku"):
            if not ex.get(key) and history_ex.get(key):
                ex[key] = history_ex[key]
        # 特例：追问物流而订单号是继承来的、且手机位可用时，
        # 优先走"手机号定位在途订单"的语义路径，避免误选历史列表里的尾单。
        if intent == "logistics" and not current_order and ex.get("phone_tail"):
            ex["order_id"] = ""

        action = self._decide(intent, ex, results, user_text)

        # 思考块
        think = "识别用户意图并确定下一步操作…"
        if action.get("type") == "tool":
            think = (
                f"用户意图：{action.get('intent_note')}\n"
                f"准备调用工具：{action.get('name')}，参数："
                f"{json.dumps(action.get('args', {}), ensure_ascii=False)}"
            )
        yield ChatResponse(
            id=resp_id,
            content=[ThinkingBlock(thinking=think, id=think_id)],
            is_last=False,
        )
        await asyncio.sleep(0.02)

        if action.get("type") == "tool":
            args_json = json.dumps(action.get("args", {}), ensure_ascii=False)
            yield ChatResponse(
                id=resp_id,
                content=[
                    ToolCallBlock(
                        id=call_id,
                        name=action["name"],
                        input=args_json,
                    )
                ],
                is_last=False,
            )
            return

        # 纯文本回复，流式吐出（只出增量块，由框架累积并补发 is_last=True 完整块）
        text = action.get("text", "")
        for i in range(0, len(text), 7):
            yield ChatResponse(
                id=resp_id,
                content=[TextBlock(text=text[i : i + 7], id=text_id)],
                is_last=False,
            )
            await asyncio.sleep(0.012)

    def _decide(
        self,
        intent: str,
        ex: dict[str, str],
        results: list[tuple[str, dict]],
        user_text: str,
    ) -> dict[str, Any]:
        """根据意图与已产生的工具结果，决定本轮是调用工具还是直接回复。"""
        latest = {name: data for name, data in results}

        # ---- 写操作已完成的收尾 ----
        for done in ("apply_refund", "cancel_order", "create_ticket", "transfer_to_human", "add_to_cart", "update_cart_item", "place_order", "pay_order"):
            if done in latest:
                return {"type": "answer", "text": self._compose_done(done, latest[done])}

        # ---- 退款链：先查资格，再决定是否申请 ----
        if intent in ("refund", "refund_inquiry") and latest.get("check_refund_policy"):
            policy = latest["check_refund_policy"]
            if policy.get("refundable"):
                if intent == "refund":
                    return {
                        "type": "tool",
                        "name": "apply_refund",
                        "args": {
                            "order_id": ex.get("order_id") or policy.get("order_id", ""),
                            "reason": ex.get("reason"),
                        },
                        "intent_note": "确认可退款，用户已明确要求退款，提交申请",
                    }
                return {
                    "type": "answer",
                    "text": self._compose_refund_policy(policy),
                }
            return {
                "type": "answer",
                "text": f"很抱歉，这笔订单目前无法退款：{policy.get('reason', '状态不支持')}。"
                "如果涉及商品质量问题，我可以帮您创建工单由专员跟进哦。",
            }

        # ---- 物流链：先列订单定位，再查物流 ----
        if intent == "logistics" and latest.get("track_logistics"):
            return {"type": "answer", "text": self._compose_logistics(latest["track_logistics"])}
        if intent == "logistics" and latest.get("list_recent_orders"):
            orders = latest["list_recent_orders"].get("orders", [])
            for o in orders:
                if o.get("status") in ("已发货", "派送中"):
                    return {
                        "type": "tool",
                        "name": "track_logistics",
                        "args": {"order_id": o["order_id"]},
                        "intent_note": "根据手机号定位到在途订单，查询物流轨迹",
                    }
            return {"type": "answer", "text": self._compose_orders(latest["list_recent_orders"])}

        # ---- 其余工具结果：直接复述 ----
        if latest.get("query_order"):
            return {"type": "answer", "text": self._compose_order(latest["query_order"])}
        if latest.get("track_logistics"):
            return {"type": "answer", "text": self._compose_logistics(latest["track_logistics"])}
        if latest.get("list_recent_orders"):
            return {"type": "answer", "text": self._compose_orders(latest["list_recent_orders"])}
        if latest.get("query_user_profile"):
            return {"type": "answer", "text": self._compose_profile(latest["query_user_profile"])}
        if latest.get("search_faq"):
            return {"type": "answer", "text": self._compose_faq(latest["search_faq"])}

        # ---- 首轮决策：按意图调用工具或直接回复 ----
        order_id, phone, sku = ex.get("order_id"), ex.get("phone_tail"), ex.get("sku")

        if intent == "human":
            return {
                "type": "tool",
                "name": "transfer_to_human",
                "args": {"reason": f"用户要求转人工：{user_text}"},
                "intent_note": "用户要求转人工",
            }
        if intent in ("complaint", "suggestion"):
            return {"type": "tool", "name": "create_ticket",
                    "args": {"title": self._ticket_title(user_text), "detail": user_text,
                             "category": "complaint" if intent == "complaint" else "suggestion"},
                    "intent_note": "创建投诉/建议工单"}

        if intent in ("cancel",) and order_id:
            return {"type": "tool", "name": "cancel_order",
                    "args": {"order_id": order_id, "reason": "用户主动取消"},
                    "intent_note": "取消未发货订单"}

        if intent == "refund":
            if order_id:
                return {"type": "tool", "name": "check_refund_policy",
                        "args": {"order_id": order_id}, "intent_note": "先核查退款资格"}
            if phone:
                return {"type": "tool", "name": "list_recent_orders",
                        "args": {"phone_tail": phone}, "intent_note": "用手机号定位订单后办理退款"}
            return {"type": "answer", "text": self._ask_order()}

        if intent == "refund_inquiry":
            # 纯政策问询：优先检索知识库的口径
            if not order_id:
                return {"type": "tool", "name": "search_faq",
                        "args": {"question": user_text}, "intent_note": "检索退款/退货政策"}
            return {"type": "tool", "name": "check_refund_policy",
                    "args": {"order_id": order_id}, "intent_note": "核查这笔订单的退款资格"}

        if intent == "logistics":
            if order_id:
                return {"type": "tool", "name": "track_logistics",
                        "args": {"order_id": order_id}, "intent_note": "查询物流轨迹"}
            if phone:
                return {"type": "tool", "name": "list_recent_orders",
                        "args": {"phone_tail": phone}, "intent_note": "用手机号定位在途订单"}
            return {"type": "answer", "text": self._ask_order()}

        if intent == "order":
            if order_id:
                return {"type": "tool", "name": "query_order",
                        "args": {"order_id": order_id}, "intent_note": "查询订单详情"}
            if phone:
                return {"type": "tool", "name": "list_recent_orders",
                        "args": {"phone_tail": phone}, "intent_note": "查询用户最近订单"}
            return {"type": "answer", "text": self._ask_order()}

        # ---- 购物：浏览/加购/下单/支付 ----
        if intent == "shop":
            if latest.get("list_products"):
                return {"type": "answer", "text": self._compose_products(latest["list_products"], price_limit=self._price_limit(user_text))}
            if latest.get("query_product"):
                return {"type": "answer", "text": self._compose_product(latest["query_product"])}
            if sku:
                return {"type": "tool", "name": "query_product",
                        "args": {"sku": sku}, "intent_note": "查询商品详情"}
            return {"type": "tool", "name": "list_products",
                    "args": {"keyword": self._keyword(user_text)}, "intent_note": "浏览/搜索商品"}

        if intent == "cart":
            if latest.get("view_cart"):
                return {"type": "answer", "text": self._compose_cart(latest["view_cart"])}
            if sku and phone:
                return {"type": "tool", "name": "add_to_cart",
                        "args": {"phone_tail": phone, "sku": sku, "qty": 1},
                        "intent_note": "把指定商品加入购物车"}
            if phone:
                return {"type": "tool", "name": "view_cart",
                        "args": {"phone_tail": phone}, "intent_note": "查看购物车"}
            return {"type": "answer", "text": "好的，要看购物车需要先确认您的身份，请问收货手机号后四位是多少呢？"}

        if intent == "checkout":
            if latest.get("place_order"):
                return {"type": "answer", "text": self._compose_done("place_order", latest["place_order"])}
            if latest.get("view_cart"):
                cart = latest["view_cart"]
                if cart.get("empty"):
                    return {"type": "answer", "text": cart.get("message", "购物车是空的哦")}
                return {"type": "tool", "name": "place_order",
                        "args": {"phone_tail": phone,
                                 "address": self._address(user_text),
                                 "items": None, "use_coupon": False},
                        "intent_note": "按购物车内容下单"}
            if phone:
                return {"type": "tool", "name": "view_cart",
                        "args": {"phone_tail": phone}, "intent_note": "先核对购物车再下单"}
            return {"type": "answer", "text": "好的，下单前请提供收货手机号后四位，我好核对您的购物车与地址哦。"}

        if intent == "pay":
            if latest.get("pay_order"):
                return {"type": "answer", "text": self._compose_done("pay_order", latest["pay_order"])}
            if order_id:
                return {"type": "tool", "name": "pay_order",
                        "args": {"order_id": order_id}, "intent_note": "完成待付款订单支付"}
            return {"type": "answer", "text": "好的，请告诉我您的订单号（SO 开头），我来帮您完成支付。"}

        if intent == "profile":
            if phone:
                return {"type": "tool", "name": "query_user_profile",
                        "args": {"phone_tail": phone}, "intent_note": "查询会员档案"}
            return {"type": "tool", "name": "search_faq",
                    "args": {"question": user_text}, "intent_note": "检索会员政策"}

        if intent == "faq":
            return {"type": "tool", "name": "search_faq",
                    "args": {"question": user_text}, "intent_note": "检索知识库"}

        if intent == "greeting":
            return {"type": "answer", "text": self._greeting()}

        # fallback
        return {"type": "answer", "text": self._fallback()}

    def _keyword(self, text: str) -> str:
        for token in ("耳机", "保温杯", "餐具", "连衣裙", "键盘", "洗面奶", "四件套", "充电宝", "杯子", "降噪"):
            if token in text:
                return token
        return ""

    def _address(self, text: str) -> str:
        m = re.search(r"([\u4e00-\u9fa5\d\w]{2,30}(?:路|街|道|区|号|园|镇))", text)
        return m.group(1) if m else "浙江省杭州市西湖区文三路 199 号"

    def _ticket_title(self, text: str) -> str:
        text = text.strip().replace("\n", " ")
        return text[:20] if text else "用户提交"

    # ---- 回复组成 ----
    def _price_limit(self, text: str) -> float | None:
        """从"300以内/300元以下/预算300/低于300"中解析价格上限（多条件选品）。"""
        text = text or ""
        m = re.search(
            r"(?:低于|不超过|最多|以内|以下|预算|控制|below|under|max|less than)\s*[￥¥€]?\s*(\d{2,6})",
            text,
        )
        if not m:
            m = re.search(r"[￥¥]\s*(\d{2,6})\s*元?\s*(?:以|之|及)?(?:内|下)", text)
        if not m:
            m = re.search(r"(\d{2,6})\s*元?\s*(?:以|之|及)?(?:内|下|左右)", text)
        if not m:
            return None
        return float(m.group(1))

    def _fmt_price(self, price: float | None) -> str:
        return f"￥{price:g}" if price is not None else "不限"

    def _compose_products(self, data: dict, price_limit: float | None = None) -> str:
        if not data.get("found"):
            return data.get("message", "没有找到相关商品哦")
        all_products = list(data.get("products", []))
        products = all_products
        if price_limit is not None:
            products = [p for p in all_products if (p.get("price") or 0) <= price_limit]
        if price_limit is not None and not products:
            if all_products:
                nearest = min(all_products, key=lambda p: p.get("price", 0))
                return (
                    f"抱歉，{self._fmt_price(price_limit)} 内暂时没有符合条件的 ～\n"
                    f"目前在售中最接近的是 **{nearest.get('name')}**【{nearest.get('sku')}】"
                    f"￥{nearest.get('price', 0):.2f}。\n"
                    "需要我帮您放宽预算，或看看其他品类吗？"
                )
            return f"抱歉，{self._fmt_price(price_limit)} 内暂时没有符合条件的 ～ 需要我换个品类帮您看看吗？"
        lines = []
        for i, pr in enumerate(products, 1):
            lines.append(
                f"{i}. **{pr['name']}**【{pr['sku']}】￥{pr['price']:.2f}"
                f"（库存 {pr.get('stock','-')}）{pr.get('spec','') or ''}"
            )
        note = ""
        if price_limit is not None:
            note = f"（已按 **{self._fmt_price(price_limit)}以内** 过滤 ✓）\n"
        return (
            f"为您找到 {len(products)} 件商品 👇\n"
            + note
            + "\n".join(lines)
            + "\n\n想了解哪件？告诉我货号（如 P1001），或直接说“把P1001加进购物车（附收货手机号后四位）”就行～"
        )


    def _compose_product(self, data: dict) -> str:
        if not data.get("found"):
            return data.get("message", "没找到该商品哦，您核对下货号？")
        pr = data["product"]
        return (
            f"📦 **{pr['name']}**\n"
            f"· 货号：{pr.get('sku')}\n"
            f"· 价格：￥{pr['price']:.2f}\n"
            f"· 库存：{pr.get('stock','-')} 件\n"
            f"· 规格：{pr.get('spec','—') or '—'}\n"
            f"· 介绍：{pr.get('desc','')}\n\n"
            "需要加入购物车吗？告诉我收货手机号后四位即可下单。"
        )

    def _compose_cart(self, data: dict) -> str:
        if data.get("empty"):
            return data.get("message", "购物车还是空的呢，需要我先帮您挑几件商品吗？")
        lines = []
        for it in data.get("items", []):
            lines.append(f"· {it['name']} x{it['qty']}  ￥{it['subtotal']:.2f}")
        return (
            "🛒 您的购物车\n"
            + "\n".join(lines)
            + f"\n\n合计 **￥{data['total']:.2f}**\n"
            "确认下单请回复“下单”并附收货地址，我来帮您生成订单；也可以先这样选购～"
        )

    def _compose_order(self, data: dict) -> str:
        if not data.get("found"):
            return f"{data.get('message', '未查询到该订单')} 🙏 您可以核对订单号后再试，或告诉我收货手机号后四位，我帮您查。"
        o = data["order"]
        items = "、".join(f"{it['name']}×{it['qty']}" for it in o["items"])
        return (
            f"为您查到订单 {o['order_id']} 啦 📄\n"
            f"· 状态：**{o['status']}**\n"
            f"· 商品：{items}\n"
            f"· 实付：￥{o['amount']:.2f}\n"
            f"· 下单时间：{o['created_at']}\n"
            f"· 收货地址：{o['address']}\n"
            f"需要我接着帮您查物流，或办理退款/其他售后吗？"
        )

    def _compose_orders(self, data: dict) -> str:
        if not data.get("found"):
            return data.get("message", "未找到订单")
        orders = data["orders"]
        lines = []
        for i, o in enumerate(orders, 1):
            items = "、".join(f"{it['name']}" for it in o["items"])
            lines.append(
                f"{i}. {o['order_id']}｜{o['status']}｜￥{o['amount']:.2f}｜{items}（{o['created_at'][5:]} 下单）"
            )
        return (
            f"为您找到最近 {len(orders)} 笔订单 👇\n"
            + "\n".join(lines)
            + "\n\n告诉我订单号（如 SO…）就可以继续查物流 / 退款 / 取消等操作哦 😊"
        )

    def _compose_logistics(self, data: dict) -> str:
        if not data.get("found"):
            return data.get("message", "未查询到该订单")
        if not data.get("has_logistics"):
            return f"这笔订单目前状态是「{data.get('order_status','')}」，还没有物流信息哦 🔍 商家发货后第一时间告诉我，我再帮您盯。"
        latest = data.get("latest") or {}
        text = (
            f"帮您查到物流啦 🚚\n"
            f"承运：{data.get('company')}｜运单：{data.get('tracking_no')}\n"
            f"最新动态：{latest.get('desc','')}（{latest.get('time','')}）\n"
        )
        if data.get("timeline"):
            text += "最近节点：\n" + "\n".join(
                f"· {t.get('time','')} {t.get('desc','')}" for t in reversed(data["timeline"])
            )
        return text + "\n还有其他需要吗？"

    def _compose_refund_policy(self, data: dict) -> str:
        return (
            f"这笔订单可以退款/退货哦 ✅（实付 ￥{data.get('amount', 0):.2f}）\n"
            f"适用规则：{data.get('rule','')}\n"
            f"如果您现在就要申请，直接回复「确认退款」即可，我马上帮您提交。"
        )

    def _compose_done(self, tool: str, data: dict) -> str:
        msg = data.get("message", "")
        if tool == "apply_refund" and data.get("success"):
            return f"{msg}\n\n已提交的申请单号是 **{data.get('ticket_id','')}**，可随时问我处理进度 😊"
        return msg + ("\n还有其他问题需要帮忙吗？" if not data.get("success", False) else "")

    def _compose_profile(self, data: dict) -> str:
        if not data.get("found"):
            return data.get("message", "未找到用户档案")
        coupons = data.get("coupons") or []
        text = (
            f"查到了~ {data.get('nickname')}，您当前是 **{data.get('member_level')}**，"
            f"账户积分 **{data.get('points')}** 分（100 积分可抵 1 元）。\n"
        )
        if coupons:
            text += "可用券：\n" + "\n".join(f"· {c.get('title')}（{c.get('expire_at','')} 到期）" for c in coupons)
        else:
            text += "暂无可用优惠券，签到可得积分哦 🎁"
        return text

    def _compose_faq(self, data: dict) -> str:
        if not data.get("found"):
            return data.get("message", "知识库暂未收录这个问题，我建议您直接转人工或创建工单，专人为您解答 🙏")
        results = data["results"]
        text = f"关于「{results[0].get('title','')}」📚\n{results[0].get('content','')}"
        if len(results) > 1:
            text += f"\n\n👀 另有一条相关说明「{results[1].get('title','')}」，需要的话我再展开。"
        return text

    def _ask_order(self) -> str:
        return "好的，请问能提供**订单号**（SO 开头的号码）吗？或者给我**收货手机号后四位**，我帮您定位订单 😊"

    def _greeting(self) -> str:
        return "您好，我是「云雀商城」的智能客服小云 ☁️ 想选购商品、查订单物流、办理退款退货、咨询运费发票会员等，都可以找我哦。要看看我们有哪些好物吗？"

    def _fallback(self) -> str:
        return (
            "这个问题我还不太确定怎么帮您 🤔\n"
            "我目前可以帮您：\n"
            "· 选购商品 / 加购物车 / 下单支付\n"
            "· 查订单 / 物流（提供订单号或手机号后四位）\n"
            "· 申请退款退货 / 取消订单\n"
            "· 解答运费、发货、发票、会员积分等政策\n"
            "· 创建投诉工单 / 转人工客服\n\n"
            "直接告诉我想做什么就好～"
        )