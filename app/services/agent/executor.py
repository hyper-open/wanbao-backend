from typing import TypedDict, Optional

from langgraph.graph import StateGraph, END
from sqlalchemy.orm import Session

from app.core.llm.gateway import LLMGateway
from app.models import Tool
from app.services.agent.graphs import EXECUTOR_REGISTRY
from app.services.content_security import check_output, ContentRejected

import uuid


class ExecState(TypedDict, total=False):
    input: str
    prompt: str
    image_keys: list          # 图片附件 object_key 列表（多模态输入）
    db: object
    result: str
    model_used: str
    result_ref: str
    gateway: object
    inv_id: int


def _build_simple_graph():
    def execute(state: ExecState) -> ExecState:
        from app.services.attachment import image_parts, with_images

        gw: LLMGateway = state["gateway"]
        db = state.get("db")
        img_parts, skipped = ([], []) if not state.get("image_keys") else image_parts(db, state["image_keys"])
        user_content = with_images(state["input"], img_parts, skipped)
        resp = gw.call(
            "executor",
            [
                {"role": "system", "content": state["prompt"]},
                {"role": "user", "content": user_content},
            ],
            max_tokens=4000,
        )
        state["result"] = resp["content"]
        state["model_used"] = resp["model_used"]
        return state

    g = StateGraph(ExecState)
    g.add_node("execute", execute)
    g.set_entry_point("execute")
    g.add_edge("execute", END)
    return g.compile()


def _build_graph(tool: Tool):
    if tool.executor and tool.executor in EXECUTOR_REGISTRY:
        return EXECUTOR_REGISTRY[tool.executor](tool)
    return _build_simple_graph()


def run_tool(
    db: Session,
    tool: Tool,
    input_text: str,
    attachment_texts: Optional[list] = None,
    inv_id: Optional[int] = None,
    user_id=None,
    image_keys: Optional[list] = None,
) -> tuple[str, str, Optional[str], float]:
    """执行工具，返回 (result_text, model_used, result_ref, cost_amount)。

    - 纯配置工具：单节点图。
    - 注册工具（如 docx_resume）：多步 LangGraph。
    附件文本已抽取，统一拼接到 input 中；计费边界由调用方负责。
    输出层内容安全在返回前统一筛查（命中抛 ContentRejected）。
    cost_amount 为本次执行全部 LLM 调用的估算成本（元，§16.4）。
    """
    combined = input_text or ""
    if attachment_texts:
        combined = combined + "\n\n【附件内容】\n" + "\n\n".join(attachment_texts)
    prompt = (tool.prompt_template or "{input}").replace("{input}", combined)

    gw = LLMGateway(db)
    graph = _build_graph(tool)
    out = graph.invoke(
        {
            "db": db,
            "input": combined,
            "prompt": prompt,
            "image_keys": image_keys or [],
            "result": "",
            "model_used": "",
            "result_ref": "",
            "gateway": gw,
            "inv_id": inv_id if inv_id is not None else uuid.uuid4().hex[:8],
        }
    )
    result = out.get("result", "")
    # 输出层内容安全筛查（fail-closed：命中抛 ContentRejected，由调用方退次/标记失败）
    check_output(result, db=db, user_id=user_id)
    return result, out.get("model_used", ""), out.get("result_ref"), round(gw.total_cost, 6)
