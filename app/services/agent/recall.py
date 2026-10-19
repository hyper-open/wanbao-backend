"""候选工具召回（Router 第一级：把全量工具收缩到 top-K，防上下文爆炸）。

- mock 排序（默认）：工具名/类目 bigram 命中数 ×2 + 描述 bigram 重叠，零依赖可离线。
- 真实 embedding：`EMBEDDING_PROVIDER=openai`（配 EMBEDDING_* 走 OpenAI 兼容 /embeddings）
  按余弦相似度 top-K；向量能力统一由 services/embedding.py 提供（§7.3 槽位）。
"""
from sqlalchemy.orm import Session  # noqa: F401  保持签名一致

from app.core.config import settings
from app.models import Tool
from app.services.embedding import cosine

TOP_K = 8  # 工具过百后放宽候选数：宁可多给分类器几个候选，也别漏掉正确工具
GLOBAL_RECALL_PER_MIN = 600  # 召回全局限流：所有用户的向量查询共享配额（保护本地 bge-m3）


def _recall_allow() -> bool:
    """召回全局限流（固定键 Redis 计数）；失败放行不阻塞主链路。"""
    try:
        from app.services.ratelimit import allow

        return allow("global:recall_query", limit=GLOBAL_RECALL_PER_MIN, category="global_recall")
    except Exception:  # noqa: BLE001
        return True


def _bigrams(s: str) -> set:
    import re

    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i: i + 2] for i in range(len(s) - 1)}


def _name_terms(tool: Tool) -> set:
    """强特征：名称+类目+搜索标签的 bigram（短且特异，命中即可信）。"""
    tags = " ".join(tool.tags or []) if tool.tags else ""
    return _bigrams(f"{tool.name or ''}{tool.category or ''}{tags}")


def _mock_rank(tools: list[Tool], text: str) -> list[tuple[Tool, float]]:
    hits_b = _bigrams(text)
    out = []
    for t in tools:
        strong = len(_name_terms(t) & hits_b)
        soft = len(_bigrams(f"{t.name or ''}{t.description or ''}") & hits_b)
        score = strong * 2.0 + soft * 0.3
        out.append((t, score))
    out.sort(key=lambda x: -x[1])
    return out


def _embedding_rank(db, tools: list[Tool], text: str) -> list[tuple[Tool, float]] | None:
    from app.services.embedding import cfg

    if cfg()["provider"] != "openai":
        return None  # recall 语义排序仅 openai 模式；local 哈希向量不优于 bigram，保持关键词
    if not _recall_allow():
        return None  # 全局限流：向量查询超全站配额 → 回落 bigram（保护本地 embedding 服务）
    from app.services.embedding import get_tool_vectors, openai_embed

    # 工具向量走缓存表（缺失/变更才补算），查询文本每次只算 1 条
    tvecs = get_tool_vectors(db, tools)
    if tvecs is None:
        return None
    qv = openai_embed([text])
    if not qv:
        from app.services.ratelimit import _alert
        _alert("embed_fallback", "语义召回失败，已回落关键词匹配（检查 bge-m3 服务）")
        return None
    q = qv[0]
    scored = [(t, cosine(tvecs.get(t.id), q)) for t in tools if tvecs.get(t.id)]
    scored.sort(key=lambda x: -x[1])
    return scored


def rank_tools(db, tools: list[Tool], text: str) -> list[tuple[Tool, float]]:
    """混合召回：语义 top-K ∪ 关键词 top-K（去重，语义优先）。

    单靠语义有一个已知盲区：用户把「待处理材料」直接内联在需求里时
    （如「把这句话翻译成英文：今晚的月色真美」），材料内容会稀释意图相似度，
    导致正确工具跌出 top-K。关键词通道（工具名/标签 bigram）恰好补上这个盲区。
    """
    if not tools:
        return []
    if not (text or "").strip():
        return [(t, 0.0) for t in tools]
    sem = _embedding_rank(db, tools, text)
    if sem is None:
        return _mock_rank(tools, text)
    mock = _mock_rank(tools, text)
    seen: set = set()
    out: list[tuple[Tool, float]] = []
    for t, s in sem[:TOP_K] + mock[:TOP_K]:
        if t.id not in seen:
            seen.add(t.id)
            out.append((t, s))
    return out[: TOP_K + 4]


def recall_topk(db, tools: list[Tool], text: str, top_k: int = TOP_K) -> list[Tool]:
    """兼容入口：返回混合召回的工具列表（语义 ∪ 关键词，已去重）。"""
    return [t for t, _ in rank_tools(db, tools, text)]
