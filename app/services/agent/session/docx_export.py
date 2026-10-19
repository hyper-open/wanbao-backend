"""通用 Word 导出会话 handler：产出阶段把文本落成 docx。

不按 tool_id 注册：`get_handler` 在 `tool.executor == "docx_export"` 时自动启用。
后台把任意纯文本工具的执行器设为 `docx_export`（Word 文档导出（通用）），
即可获得 .docx 下载能力，无需写代码。导出实现复用 `graphs/docx_export`（含 AIGC 标识）。
"""
from __future__ import annotations

from typing import Optional

from app.services.agent.session.base import DefaultHandler, SessionCtx


class DocxExportHandler(DefaultHandler):
    def produce(self, ctx: SessionCtx) -> tuple[str, Optional[str], str, float]:
        text, ref, model_used, cost = super().produce(ctx)
        if not text or not ctx.invocation_id:
            return text, ref, model_used, cost
        from app.services.agent.graphs.docx_export import _make_docx

        out = _make_docx({"result": text, "inv_id": ctx.invocation_id})
        return text, out.get("result_ref"), model_used, cost
