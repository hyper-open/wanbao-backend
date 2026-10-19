"""交互式会话图（通用）：analyze → decide → ask(interrupt) → merge → … → ask_confirm(interrupt) → produce。

```
analyze ─► decide ─(有问题)─► ask(interrupt) ─► merge ─► decide
              └─(够了/跳过)─► prepare_confirm ─► ask_confirm(interrupt) ─► produce ─► END
```

**两条硬约束**（LangGraph interrupt 语义决定的）：
1. `ask` / `ask_confirm` 节点**只调用 `interrupt()`**，不做任何副作用 ——
   被中断的节点在恢复时会**从头重跑**，若有副作用就会重复执行（重复调 LLM / 重复写库）。
   读操作（查报价/余额）放在 `prepare_confirm` 这种独立节点里。
2. **state 必须是可序列化的纯数据**（图带 checkpointer，state 会落库），
   禁止放 SQLAlchemy Session / ORM 对象；每个节点自行 `SessionLocal()` 并关闭。
"""
from __future__ import annotations

from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from app.core.db import SessionLocal
from app.core.llm.checkpoint import MySQLCheckpointSaver
from app.core.llm.gateway import LLMGateway
from app.models import Tool
from app.services.agent.session.base import SessionCtx, session_defaults
from app.services.agent.session.registry import get_handler


class SessionState(TypedDict, total=False):
    # —— 仅可序列化纯数据 ——
    tool_id: str
    channel: str
    user_id: int
    input_text: str
    attachment_text: str
    attachment_images: list    # 图片附件 object_key 列表（多模态输入）
    analysis: dict
    facts: dict
    round: int
    max_rounds: int
    question: Optional[dict]
    done_asking: bool          # 用户跳过 / 轮次用尽
    skipped: bool
    price_quota: Optional[int]
    quota_remain: Optional[int]
    invocation_id: Optional[int]
    llm_cost: float
    prompt_tokens: int          # 累计输入 token（观测/上下文护栏）
    completion_tokens: int      # 累计输出 token
    result: str
    result_ref: Optional[str]
    model_used: str
    diff: Optional[dict]        # 优化前/后对比（可配置关闭）
    _answer: Optional[dict]


def _mk_ctx(db, state: SessionState) -> SessionCtx:
    """每个节点各自建 db/gateway（不能跨节点持有会话）。"""
    tool = db.get(Tool, state.get("tool_id"))
    d = session_defaults(tool)
    # 注意：max_rounds 可能是 0（轻量工具跳过追问），不能用 `or` 兜底（0 是 falsy）
    mr = state.get("max_rounds")
    ctx = SessionCtx(
        db=db,
        tool=tool,
        gw=LLMGateway(db),
        input_text=state.get("input_text") or "",
        attachment_text=state.get("attachment_text") or "",
        attachment_images=state.get("attachment_images") or [],
        analysis=state.get("analysis") or {},
        facts=state.get("facts") or {},
        round_no=int(state.get("round") or 0),
        max_rounds=int(mr) if mr is not None else int(d["max_rounds"]),
        allowed_tools=d["tool_calls"],
        max_tool_rounds=d["max_tool_rounds"],
        invocation_id=state.get("invocation_id"),
    )
    return ctx


# ---------------- 节点 ----------------
def _usage_delta(state: SessionState, gw: LLMGateway, p0: int, c0: int) -> dict:
    """把本次节点内消耗的 token 累加进 state（网关实例累计，跨 handler 通用）。"""
    return {
        "prompt_tokens": int(state.get("prompt_tokens") or 0) + (gw.total_prompt_tokens - p0),
        "completion_tokens": int(state.get("completion_tokens") or 0) + (gw.total_completion_tokens - c0),
    }


def _analyze(state: SessionState) -> dict:
    db = SessionLocal()
    try:
        ctx = _mk_ctx(db, state)
        d = session_defaults(ctx.tool)
        # 轻量工具（analyze=False）：跳过分析，不花 LLM 成本；max_rounds=0 会让
        # _decide 直接判定「无需追问」，一路走到 prepare_confirm。
        if not d["analyze"]:
            return {
                "analysis": {"summary": "", "missing": [], "suspicious": []},
                "facts": state.get("facts") or {},
                "round": 0,
                "max_rounds": 0,
                "llm_cost": float(state.get("llm_cost") or 0),
            }
        p0, c0 = ctx.gw.total_prompt_tokens, ctx.gw.total_completion_tokens
        analysis, cost = get_handler(ctx.tool).analyze(ctx)
        return {
            "analysis": analysis,
            "facts": state.get("facts") or {},
            "round": int(state.get("round") or 0),
            "max_rounds": int(state.get("max_rounds") or d["max_rounds"]),
            "llm_cost": float(state.get("llm_cost") or 0) + cost,
            **_usage_delta(state, ctx.gw, p0, c0),
        }
    finally:
        db.close()


def _decide(state: SessionState) -> dict:
    cost_prev = float(state.get("llm_cost") or 0)
    if state.get("done_asking"):
        return {"question": None}
    db = SessionLocal()
    try:
        ctx = _mk_ctx(db, state)
        if ctx.round_no >= ctx.max_rounds:
            return {"question": None, "done_asking": True}
        p0, c0 = ctx.gw.total_prompt_tokens, ctx.gw.total_completion_tokens
        q, cost = get_handler(ctx.tool).decide(ctx)
        return {"question": q, "llm_cost": cost_prev + cost, **_usage_delta(state, ctx.gw, p0, c0)}
    finally:
        db.close()


def _route_after_decide(state: SessionState) -> str:
    return "ask" if state.get("question") else "prepare_confirm"


def _ask(state: SessionState) -> dict:
    """⚠️ 只 interrupt，无副作用（恢复时会重跑本节点）。"""
    v = interrupt({"type": "question", "question": state.get("question") or {}})
    return {"_answer": v if isinstance(v, dict) else {"answer": str(v or "")}}


def _merge(state: SessionState) -> dict:
    ans = state.get("_answer") or {}
    if ans.get("skip"):
        return {"skipped": True, "done_asking": True, "question": None, "_answer": None}
    q = state.get("question") or {}
    text = str(ans.get("answer") or "").strip()
    facts = dict(state.get("facts") or {})
    if text:
        facts[str(q.get("field") or "general")] = text
    return {
        "facts": facts,
        "round": int(state.get("round") or 0) + 1,
        "question": None,
        "_answer": None,
    }


def _prepare_confirm(state: SessionState) -> dict:
    """确认前的只读准备：算报价与余额（放在 interrupt 之前的独立节点）。"""
    from app.services.billing import effective_price_quota, get_account, quota_remain

    db = SessionLocal()
    try:
        tool = db.get(Tool, state.get("tool_id"))
        price = effective_price_quota(tool, state.get("channel") or "wechat")
        acct = get_account(db, state.get("user_id"))
        return {"price_quota": int(price), "quota_remain": int(quota_remain(db, state.get("user_id")))}
    finally:
        db.close()


def _ask_confirm(state: SessionState) -> dict:
    """⚠️ 只 interrupt，无副作用；恢复值由 API 在扣次后传入 invocation_id。"""
    v = interrupt(
        {
            "type": "confirm_charge",
            "price_quota": state.get("price_quota"),
            "quota_remain": state.get("quota_remain"),
        }
    )
    return {"invocation_id": (v or {}).get("invocation_id")}


def _produce(state: SessionState) -> dict:
    db = SessionLocal()
    try:
        from app.services.content_security import check_output

        ctx = _mk_ctx(db, state)
        handler = get_handler(ctx.tool)
        p0, c0 = ctx.gw.total_prompt_tokens, ctx.gw.total_completion_tokens
        result, ref, model_used, cost = handler.produce(ctx)
        # 输出层内容安全（fail-closed：命中抛异常 → runner 置 failed 并退次）
        check_output(result, db=db, user_id=state.get("user_id"))
        out = {
            "result": result,
            "result_ref": ref,
            "model_used": model_used,
            "llm_cost": float(state.get("llm_cost") or 0) + cost,
        }
        # 产出后生成「优化前后对比」（可配置关闭；失败不影响产出）
        if session_defaults(ctx.tool).get("diff"):
            fn = getattr(handler, "diff", None)
            if callable(fn):
                try:
                    diff, dcost = fn(ctx, result)
                except Exception:  # noqa: BLE001  对比生成失败不阻塞产出
                    diff, dcost = None, 0.0
                out["llm_cost"] = float(out["llm_cost"]) + float(dcost or 0)
                if diff:
                    out["diff"] = diff
        out.update(_usage_delta(state, ctx.gw, p0, c0))
        return out
    finally:
        db.close()


def build_graph():
    """构建交互图（每次运行重建，状态由 MySQL checkpointer 持久化）。"""
    g = StateGraph(SessionState)
    g.add_node("analyze", _analyze)
    g.add_node("decide", _decide)
    g.add_node("ask", _ask)
    g.add_node("merge", _merge)
    g.add_node("prepare_confirm", _prepare_confirm)
    g.add_node("ask_confirm", _ask_confirm)
    g.add_node("produce", _produce)

    g.set_entry_point("analyze")
    g.add_edge("analyze", "decide")
    g.add_conditional_edges(
        "decide", _route_after_decide, {"ask": "ask", "prepare_confirm": "prepare_confirm"}
    )
    g.add_edge("ask", "merge")
    g.add_edge("merge", "decide")
    g.add_edge("prepare_confirm", "ask_confirm")
    g.add_edge("ask_confirm", "produce")
    g.add_edge("produce", END)
    return g.compile(checkpointer=MySQLCheckpointSaver())
