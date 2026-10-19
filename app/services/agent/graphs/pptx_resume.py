"""PPT 视觉重排执行图：load(取原稿) -> optimize(LLM 优化+版式规划) -> rebuild(重排导出)。

复杂工具内部多步 LangGraph，执行层为 Workflow 而非子 agent；计费边界在 confirm 统一处理。

说明：
- 真实模式下 LLM 输出每页的标题/要点/版式(JSON)，rebuild 据此重排导出新 pptx。
- mock 模式下 LLM 不返回 JSON，rebuild 自动回退为「原稿重渲染」，保证整条异步链路跑通。
"""
import io
import json
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, END

from app.core.llm.gateway import LLMGateway
from app.models import Attachment, ToolInvocation
from app.services import attachment as att


class PptxState(TypedDict, total=False):
    db: object
    inv_id: int
    input: str
    slides_raw: list
    optimized: str
    result: str
    model_used: str
    result_ref: str
    gateway: object


def _extract_slides(prs) -> list:
    slides = []
    for slide in prs.slides:
        layout = slide.slide_layout.name if slide.slide_layout else "unknown"
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = (para.text or "").strip()
                    if t:
                        texts.append(t)
        if not texts:
            continue
        slides.append({"layout": layout, "title": texts[0], "bullets": texts[1:]})
    return slides


def _load(s: PptxState) -> PptxState:
    db = s.get("db")
    inv_id = s.get("inv_id")
    if not db or inv_id is None:
        raise RuntimeError("pptx loader needs db+inv_id")
    inv = db.get(ToolInvocation, inv_id)
    if not inv or not inv.attachment_ids:
        raise RuntimeError("pptx: no attachment")
    aid = inv.attachment_ids[0]
    a = db.get(Attachment, aid)
    if not a:
        raise RuntimeError("pptx: attachment missing")
    data = att.get_object_bytes(a.object_key)
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    s["slides_raw"] = _extract_slides(prs)
    if not s["slides_raw"]:
        raise RuntimeError("pptx: no text extracted")
    return s


def _optimize(s: PptxState) -> PptxState:
    gw: LLMGateway = s["gateway"]
    direction = (s.get("input") or "").strip()
    slides_text = "\n".join(
        f"[第{i+1}页 | {sl['layout']}]\n标题: {sl['title']}\n要点:\n"
        + "\n".join(f"  - {b}" for b in sl["bullets"])
        for i, sl in enumerate(s["slides_raw"])
    )
    system = (
        "你是资深演示设计师。请基于用户 PPT 原始内容做内容优化与视觉重排规划。\n"
        "输出 JSON 数组，每个元素：{\"layout\": \"title|section|content\", "
        "\"title\": \"...\", \"bullets\": [\"...\",\"...\"]}。\n"
        "要求：强化标题、精简要点、去除冗余、保持原意不虚构、为每页选择最合适的版式。"
        "仅输出 JSON 数组，不要额外解释。"
    )
    user = f"用户优化方向：{direction or '（无，按专业标准优化）'}\n\n原始内容：\n{slides_text}"
    resp = gw.call(
        "executor",
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        json_mode=True,
        max_tokens=6000,
    )
    s["optimized"] = resp["content"]
    s["model_used"] = resp["model_used"]
    return s


def _norm(x: dict) -> dict:
    if isinstance(x, str):
        return {"title": x, "bullets": []}
    title = x.get("title") or ""
    bullets = x.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [bullets]
    return {"title": title, "bullets": [str(b) for b in bullets]}


def _build_pptx(slides: list) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    # AIGC 隐式标识：文件元数据（§16.3-A，《AI 生成合成内容标识办法》）
    cp = prs.core_properties
    cp.author = "万宝AI助手 AI"
    cp.comments = "本文件由 AI（万宝AI助手小程序）生成，内容仅供参考"
    cp.category = "AI生成内容"
    blank = prs.slide_layouts[6]
    for sl in slides:
        slide = prs.slides.add_slide(blank)
        tb = slide.shapes.add_textbox(Inches(0.5), Inches(0.4), Inches(9), Inches(1.2))
        tf = tb.text_frame
        tf.text = sl.get("title") or ""
        for p in tf.paragraphs:
            p.font.size = Pt(32)
            p.font.bold = True
        bullets = sl.get("bullets") or []
        if bullets:
            cb = slide.shapes.add_textbox(Inches(0.7), Inches(1.8), Inches(9), Inches(5))
            ctf = cb.text_frame
            ctf.word_wrap = True
            first = True
            for b in bullets:
                p = ctf.paragraphs[0] if first else ctf.add_paragraph()
                first = False
                p.text = "• " + str(b)
                p.font.size = Pt(18)
    buf = io.BytesIO()
    # AIGC 显式标识：首页演讲者备注（§16.3-A）
    if prs.slides:
        prs.slides[0].notes_slide.notes_text_frame.text = (
            "本演示文稿由 AI（万宝AI助手小程序）生成，内容仅供参考。"
        )
    prs.save(buf)
    return buf.getvalue()


def _rebuild(s: PptxState) -> PptxState:
    slides = s.get("slides_raw") or []
    raw = s.get("optimized") or ""
    try:
        data = json.loads(raw)
        if isinstance(data, list) and data:
            slides = [_norm(x) for x in data]
    except Exception:  # noqa: BLE001  mock/解析失败时回退为原稿重渲染
        pass
    data_bytes = _build_pptx(slides)
    key = f"results/{s.get('inv_id')}.pptx"
    att.upload(key, data_bytes)
    # 存 object_key，下载 URL 由 API 层按需 presign（bucket 私有）
    s["result_ref"] = key
    s["result"] = f"已重排导出 {len(slides)} 张幻灯片"
    return s


def build(tool) -> "CompiledGraph":  # noqa: F821
    g = StateGraph(PptxState)
    g.add_node("load", _load)
    g.add_node("optimize", _optimize)
    g.add_node("rebuild", _rebuild)
    g.set_entry_point("load")
    g.add_edge("load", "optimize")
    g.add_edge("optimize", "rebuild")
    g.add_edge("rebuild", END)
    return g.compile()
