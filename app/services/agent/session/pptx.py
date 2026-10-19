"""PPT 视觉重排的会话 handler：产出阶段复用 pptx_resume 的 load→optimize→rebuild。

与 ResumeHandler 的区别：PPT 的产出是「基于原稿重排导出新 pptx」，
需要读原附件 → LLM 规划每页标题/要点/版式 → python-pptx 重建。
这里直接驱动 pptx_resume 的三个节点函数，避免重复实现；
用户在多轮里确认的信息通过 `input`（优化方向）传给 optimize 节点。
"""
from __future__ import annotations

from typing import Optional

from app.services.agent.session.base import DefaultHandler, SessionCtx, render_facts


class PptxHandler(DefaultHandler):
    def produce(self, ctx: SessionCtx) -> tuple[str, Optional[str], str, float]:
        from app.services.agent.graphs.pptx_resume import _load, _optimize, _rebuild

        direction_parts = [ctx.input_text or ""]
        if ctx.facts:
            direction_parts.append("【用户补充确认的信息】\n" + render_facts(ctx.facts))
        before = ctx.gw.total_cost
        state = {
            "db": ctx.db,
            "inv_id": ctx.invocation_id,
            "input": "\n\n".join(p for p in direction_parts if p),
            "gateway": ctx.gw,
        }
        state = _load(state)
        state = _optimize(state)
        state = _rebuild(state)
        return (
            state.get("result") or "",
            state.get("result_ref"),
            state.get("model_used") or "",
            float(ctx.gw.total_cost - before),
        )
