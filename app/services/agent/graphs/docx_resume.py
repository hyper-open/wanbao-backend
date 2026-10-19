"""简历优化执行图：optimize(LLM) -> make_docx(导出新文档)。

演示「复杂工具内部多步 LangGraph」，执行层为 Workfow 而非子 agent。
计费边界在 confirm 接口统一处理，图内多步不影响。
"""
import io
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, END

from app.core.llm.gateway import LLMGateway


class ResumeState(TypedDict, total=False):
    input: str
    prompt: str
    result: str
    model_used: str
    result_ref: str
    gateway: object
    inv_id: int


def _optimize(s: ResumeState) -> ResumeState:
    gw: LLMGateway = s["gateway"]
    resp = gw.call(
        "executor",
        [
            {"role": "system", "content": s["prompt"]},
            {"role": "user", "content": s["input"]},
        ],
        max_tokens=4000,
    )
    s["result"] = resp["content"]
    s["model_used"] = resp["model_used"]
    return s


def _make_docx(s: ResumeState) -> ResumeState:
    """将优化后文本导出为 docx 并上传 S3，result_ref 指向下载地址。"""
    from docx import Document

    from app.services import attachment as att

    doc = Document()
    # AIGC 隐式标识：文件元数据（§16.3-A，《AI 生成合成内容标识办法》）
    cp = doc.core_properties
    cp.author = "万宝工具箱 AI"
    cp.comments = "本文件由 AI（万宝工具箱小程序）生成，内容仅供参考"
    cp.category = "AI生成内容"
    for line in (s.get("result") or "").splitlines():
        if line.strip():
            doc.add_paragraph(line)
    # AIGC 显式标识：文末说明行（§16.3-A）
    from docx.shared import Pt

    note = doc.add_paragraph()
    run = note.add_run("—— 本文件由 AI（万宝工具箱小程序）生成，内容仅供参考 ——")
    run.font.size = Pt(9)
    run.italic = True
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    key = f"results/{s.get('inv_id')}.docx"
    att.upload(key, buf.getvalue())
    # 存 object_key，下载 URL 由 API 层按需 presign（bucket 私有）
    s["result_ref"] = key
    return s


def build(tool) -> "CompiledGraph":  # noqa: F821
    g = StateGraph(ResumeState)
    g.add_node("optimize", _optimize)
    g.add_node("make_docx", _make_docx)
    g.set_entry_point("optimize")
    g.add_edge("optimize", "make_docx")
    g.add_edge("make_docx", END)
    return g.compile()
