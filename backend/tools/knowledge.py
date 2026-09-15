# -*- coding: utf-8 -*-
"""FAQ 知识库检索工具（本地轻量实现：关键词 + 字符二元组相似度）。

不依赖外部向量库，开箱即用；生产环境可替换为 AgentScope RAG
（Qdrant / Milvus + Embedding 模型）而保持工具签名不变。
"""
from ..store.mock_store import faq_store


def _bigrams(text: str) -> set[str]:
    text = "".join(ch for ch in text.lower() if not ch.isspace())
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _score(query: str, entry: dict) -> float:
    """计算问题与 FAQ 条目的相关度得分。"""
    query_bg = _bigrams(query)
    if not query_bg:
        return 0.0

    # 关键词命中（权重高）
    keyword_hits = sum(1 for kw in entry.get("keywords", []) if kw in query)
    keyword_score = keyword_hits * 3.0

    # 标题 + 内容的二元组 jaccard
    doc_bg = _bigrams(entry.get("title", "") + " " + entry.get("content", ""))
    union = query_bg | doc_bg
    jaccard = len(query_bg & doc_bg) / len(union) if union else 0.0
    return keyword_score + jaccard * 4.0


async def search_faq(question: str) -> dict:
    """在商城知识库中检索常见问题的官方解答（运费、发货时效、退货规则、发票、会员积分、改地址等）。遇到政策性问题时优先使用本工具，禁止凭印象作答。

    Args:
        question: 用户的问题原文或关键描述。
    """
    entries = await faq_store.all()
    scored = sorted(
        ((entry, _score(question, entry)) for entry in entries),
        key=lambda item: item[1],
        reverse=True,
    )
    hits = [
        {
            "title": entry["title"],
            "content": entry["content"],
            "score": round(score, 4),
        }
        for entry, score in scored
        if score > 0.05
    ][:2]
    if not hits:
        return {
            "found": False,
            "message": "知识库中未检索到相关解答，请考虑转人工或创建工单",
        }
    return {"found": True, "results": hits}