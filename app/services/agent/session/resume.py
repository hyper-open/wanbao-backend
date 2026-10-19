"""简历优化的会话 handler：产出阶段额外导出 docx。

分析与追问决策直接复用 `DefaultHandler`（提示词由 `Tool.analysis_prompt` /
`Tool.prompt_template` 提供，见 seed）；只有产出阶段不同 —— 需要把优化后的文本
落成 docx 并回写 S3，这里直接复用现有 `docx_resume` 的导出实现（含 AIGC 标识），
避免重复代码。
"""
from __future__ import annotations

from typing import Optional

from app.services.agent.session.base import DefaultHandler, SessionCtx


class ResumeHandler(DefaultHandler):
    def produce(self, ctx: SessionCtx) -> tuple[str, Optional[str], str, float]:
        text, ref, model_used, cost = super().produce(ctx)
        if not text or not ctx.invocation_id:
            return text, ref, model_used, cost
        # 复用 docx_resume 的导出节点（同一份 AIGC 隐式/显式标识逻辑）
        from app.services.agent.graphs.docx_resume import _make_docx

        out = _make_docx({"result": text, "inv_id": ctx.invocation_id})
        return text, out.get("result_ref"), model_used, cost
