# -*- coding: utf-8 -*-
"""服务降级包装器：主模型异常时自动降级到备用模型，保证业务可用性。"""
import asyncio
import inspect
import logging
from typing import Any, AsyncGenerator

from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse

logger = logging.getLogger(__name__)


async def _aclose(gen) -> None:
    """安静地关闭一个异步生成器，忽略"已在关闭/已取消"等可容忍错误。"""
    if gen is None:
        return
    try:
        await gen.aclose()
    except (RuntimeError, asyncio.CancelledError, GeneratorExit):
        pass


def _has_answer_content(chunk) -> bool:
    """chunk 里是否含可被上层消费的实质内容（回复文本或工具调用）。

    用来识别"只吐了 thinking、却始终没有答案"的死等场景：累积器在超时/取消时
    会把已读的 thinking 块以 is_last=True 补发出来，此时既没有 TextBlock 也没有
    ToolCallBlock，则应判定为无果、降级到 fallback。
    """
    for block in (getattr(chunk, "content", None) or []):
        if isinstance(block, (TextBlock, ToolCallBlock)):
            return True
    return False


class DegradedChatModel(ChatModelBase):
    """服务降级包装模型。

    包装一个主模型和一个降级模型。
    当主模型的 __call__ 协程抛出异常时，自动降级到备用模型。
    对上层（Agent / ChatStreamer）完全透明。
    """

    def __init__(
        self,
        primary: ChatModelBase,
        fallback: ChatModelBase,
        fallback_on_timeout: bool = True,
        fallback_on_api_error: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        # ChatModelBase.__init__ 需要 credential/model/parameters，
        # DegradedChatModel 是包装器，直接从 primary 复制属性。
        super().__init__(
            credential=getattr(primary, "credential", None),
            model=getattr(primary, "model", "degraded-wrapper"),
            parameters=getattr(primary, "parameters", None),
            stream=getattr(primary, "stream", True),
            max_retries=0,  # DegradedChatModel 不重试，自己处理降级
            retry_delay=1.0,
            context_size=getattr(primary, "context_size", 32768),
        )
        self.primary = primary
        self.fallback = fallback
        self.fallback_on_timeout = fallback_on_timeout
        self.fallback_on_api_error = fallback_on_api_error
        self.timeout_seconds = timeout_seconds
        self._degraded_count = 0
        self._total_count = 0

    def __getattr__(self, name: str):
        """将未定义的属性代理到主模型（如 formatter）。"""
        return getattr(self.primary, name)

    @property
    def degraded_rate(self) -> float:
        if self._total_count == 0:
            return 0.0
        return self._degraded_count / self._total_count

    async def __call__(
        self,
        messages: list[Msg] | None = None,
        tools: list[dict] | None = None,
        tool_choice=None,
        **kwargs: Any,
    ):
        """
        协程入口：先尝试主模型，失败或超时降级到备用模型。

        关键点：流式模型返回 AsyncGenerator，真正的 token 消费发生在调用方
        边 ``async for`` 边等待网络响应那一层。因此超时*必须*包住整个流式
        消费，而不能只包住"拿到生成器"这第一步——否则模型只吐 thinking 却
        永远不产出终响应时（例如慢的推理模型），30s 降级永远不会触发，表现
        为"思考中…"一直转、却始终没有答案。
        返回值：ChatResponse 或 AsyncGenerator[ChatResponse, None]
        """
        self._total_count += 1

        # -- 1) 建立主模型流。这一步出错（网络/参数）→ 直接降级 --
        try:
            if self.timeout_seconds > 0:
                primary = await asyncio.wait_for(
                    self.primary(
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        **kwargs,
                    ),
                    timeout=self.timeout_seconds,
                )
            else:
                primary = await self.primary(
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    **kwargs,
                )
        except asyncio.TimeoutError as exc:
            if not self.fallback_on_timeout:
                raise
            logger.warning(
                "[Degradation] 主模型建立超时 (%.1fs)，降级 | error=%s",
                self.timeout_seconds, exc,
            )
            return self._fallback_stream(messages, tools, tool_choice, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if not self.fallback_on_api_error:
                raise
            logger.warning(
                "[Degradation] 主模型异常，降级 | error=%s", exc,
            )
            return self._fallback_stream(messages, tools, tool_choice, **kwargs)

        # -- 2) 非流式：直接透传 --
        if not inspect.isasyncgen(primary):
            return primary

        # -- 3) 流式：整体超时包住消费；超时或全程无回答内容 → 降级 --
        async def _consume() -> AsyncGenerator[ChatResponse, None]:
            try:
                saw_answer = False
                if self.timeout_seconds > 0:
                    async with asyncio.timeout(self.timeout_seconds):
                        async for chunk in primary:
                            if _has_answer_content(chunk):
                                saw_answer = True
                            yield chunk
                else:
                    async for chunk in primary:
                        if _has_answer_content(chunk):
                            saw_answer = True
                        yield chunk
            except TimeoutError as exc:
                await _aclose(primary)
                if not self.fallback_on_timeout:
                    raise
                logger.warning(
                    "[Degradation] 主模型流式超时(%.1fs)未产出终响应，降级",
                    self.timeout_seconds,
                )
                async for chunk in self._fallback_stream(
                    messages, tools, tool_choice, **kwargs
                ):
                    yield chunk
                return
            except Exception as exc:  # noqa: BLE001
                await _aclose(primary)
                if not self.fallback_on_api_error:
                    raise
                logger.warning(
                    "[Degradation] 主模型流式中断，降级 | error=%s", exc,
                )
                async for chunk in self._fallback_stream(
                    messages, tools, tool_choice, **kwargs
                ):
                    yield chunk
                return

            # 流正常结束：若全程只有 thinking、没有回复文本/工具调用，
            # 说明被提前终止或模型只思考不回复 → 降级兜底，避免"思考中…却无答案"。
            if not saw_answer:
                logger.warning(
                    "[Degradation] 主模型流正常结束但无任何回答/工具内容，降级"
                )
                async for chunk in self._fallback_stream(
                    messages, tools, tool_choice, **kwargs
                ):
                    yield chunk

        return _consume()

    def _fallback_stream(self, messages, tools, tool_choice, **kwargs: Any):
        """构造降级流的异步生成器（fallback 无论流式/非流式均可被 ``async for``）。"""
        self._degraded_count += 1
        return self._iter_fallback(messages, tools, tool_choice, **kwargs)

    async def _iter_fallback(self, messages, tools, tool_choice, **kwargs: Any):
        res = await self.fallback(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )
        if inspect.isasyncgen(res):
            async for chunk in res:
                yield chunk
        else:
            yield res

    def __repr__(self) -> str:
        return (
            f"DegradedChatModel(primary={self.primary!r}, "
            f"fallback={self.fallback!r}, "
            f"degraded={self._degraded_count}/{self._total_count})"
        )
