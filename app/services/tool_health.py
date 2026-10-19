"""工具描述体检：为「意图识别准确性」提供自动化校验流程。

三类检查（前两类纯计算秒级完成，第三类可选 LLM 抽查）：
1. 配置完整性：描述缺失/过短、无搜索标签——描述是意图识别的原料，缺了就退化成猜
2. 描述混淆对：两工具向量化文本余弦过高 → 分类器在这对工具间容易选错
3. 召回自检：以工具名 / 首个标签为模拟查询，验证该工具能进 TOP_K 候选
   （批量一次 embedding 请求算完所有模拟查询，不逐条调服务）

用法：管理后台「工具管理 → 工具体检」按钮，或 GET /api/admin/tools/health?llm=1
"""
from __future__ import annotations

from itertools import combinations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Tool
from app.services.agent.recall import TOP_K
from app.services.embedding import cosine, get_tool_vectors, openai_embed, tool_content

DESC_MIN_LEN = 8        # 描述至少 8 个字
CONFUSION_TH = 0.90     # 描述余弦超过此值视为混淆对
LLM_PAIR_LIMIT = 5      # LLM 抽查最多检查的混淆对数


def _embedding_enabled() -> bool:
    from app.services.embedding import cfg

    return cfg()["provider"] == "openai"


def report(db: Session, llm_check: bool = False) -> dict:
    tools = db.query(Tool).filter(Tool.enabled == True).all()  # noqa: E712
    out = {
        "total": len(tools),
        "config_issues": [],       # [{tool_id, name, issue}]
        "confusion_pairs": [],     # [{a, a_name, b, b_name, score}]
        "recall_misses": [],       # [{tool_id, name, query, reason}]
        "llm_checks": [],          # [{query, expect, got, ok}]
        "llm_skipped": not llm_check,
    }

    # ---- 1. 配置完整性 ----
    for t in tools:
        desc = (t.description or "").strip()
        if not desc:
            out["config_issues"].append({"tool_id": t.id, "name": t.name, "issue": "描述缺失"})
        elif len(desc) < DESC_MIN_LEN:
            out["config_issues"].append({"tool_id": t.id, "name": t.name, "issue": f"描述过短({len(desc)}字)"})
        if not (t.tags or []):
            out["config_issues"].append({"tool_id": t.id, "name": t.name, "issue": "无搜索标签（召回强特征缺失）"})

    # ---- 2. 描述混淆对（两两余弦，向量化文本来自缓存表）----
    tvecs = get_tool_vectors(db, tools)  # 缓存命中则零 embedding 调用
    confusion: list[tuple] = []
    if tvecs is not None:
        for a, b in combinations(tools, 2):
            va, vb = tvecs.get(a.id), tvecs.get(b.id)
            if va and vb:
                s = cosine(va, vb)
                if s >= CONFUSION_TH:
                    confusion.append((s, a, b))
        confusion.sort(key=lambda x: -x[0])
        out["confusion_pairs"] = [
            {"score": round(s, 4), "a": a.id, "a_name": a.name, "b": b.id, "b_name": b.name}
            for s, a, b in confusion
        ]

    # ---- 3. 召回自检（批量一次 embedding 请求）----
    queries: list[tuple[Tool, str, str]] = []  # (工具, 模拟查询, 查询类型)
    for t in tools:
        queries.append((t, t.name or t.id, "工具名"))
        if t.tags:
            queries.append((t, t.tags[0], "首标签"))
    if queries and _embedding_enabled():
        # 与生产一致：用混合召回（语义 ∪ 关键词）做自检，而非纯语义
        qvecs = openai_embed([q for _, q, _ in queries])
        if qvecs and len(qvecs) == len(queries):
            for (t, q, qtype), qv in zip(queries, qvecs):
                scored = sorted(
                    ((cosine(tvecs.get(x.id) or [], qv), x) for x in tools if tvecs.get(x.id)),
                    key=lambda x: -x[0],
                )
                top_sem = [x.id for _, x in scored[:TOP_K]]
                # 关键词通道兜底：名称/标签 bigram 强命中的工具也视为可达
                from app.services.agent.recall import _bigrams, _name_terms

                hits_b = _bigrams(q)
                kw_hit = any(_name_terms(x) & hits_b for x in tools)
                top = top_sem + ([x.id for x in tools if _name_terms(x) & hits_b] if not kw_hit else [])
                if t.id not in top and not any(_name_terms(x) & hits_b and x.id == t.id for x in tools):
                    out["recall_misses"].append(
                        {"tool_id": t.id, "name": t.name, "query": q, "query_type": qtype, "top": top[:TOP_K]}
                    )
    else:
        out["recall_misses_skipped"] = True

    # ---- 4. 可选：LLM 抽查混淆对（分类器能否区分）----
    if llm_check and confusion:
        from app.services.agent.router import recognize

        for s, a, b in confusion[:LLM_PAIR_LIMIT]:
            for tool, other in ((a, b), (b, a)):
                q = _query_from(tool)
                r = recognize(db, q, prior_tool=None)
                got = "menu" if r.get("status") == "menu" else r.get("tool_id")
                out["llm_checks"].append(
                    {"query": q, "expect": tool.id, "other": other.id, "got": got, "ok": got == tool.id}
                )
    return out


def _query_from(tool: Tool) -> str:
    """从工具自身生成一句自然的用户式查询（用于 LLM 抽查）。"""
    return f"我想{tool.description or tool.name}"
