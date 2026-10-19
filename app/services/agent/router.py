import json

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.llm.gateway import LLMGateway
from app.models import Tool
from app.services.agent.recall import recall_topk
from app.services.billing import effective_price_quota


def _channel_visible_tools(db: Session, account_id: str | None) -> list[Tool]:
    """意图识别候选池：启用 + 渠道可见（channel_ids 空=全渠道）。"""
    q = db.query(Tool).filter(Tool.enabled == True)  # noqa: E712
    if account_id:
        q = q.filter(
            or_(
                Tool.channel_ids.is_(None),
                func.json_length(Tool.channel_ids) == 0,
                func.json_contains(Tool.channel_ids, json.dumps(account_id)),
            )
        )
    return q.all()


def recognize(
    db: Session, text: str, channel: str = "wechat", attachment_hint: str = "", prior_tool=None,
    account_id: str | None = None,
) -> dict:
    """意图识别：召回 top-K 候选 -> classifier 选 1 -> 置信度三分支。

    attachment_hint：用户上传附件的文件名/内容摘要。用于「只传文件不说话」的场景，
    参与召回与分类，避免简历等附件类需求识别不出对应工具。

    prior_tool：本会话上一个任务用的工具（会话内延续先验）。输入像「继续上一任务
    的内容」时优先选它；且无论召回与否都保证它在候选里。

    status: confirm(≥0.85 直接确认) / clarify(0.6-0.85 追问) / menu(<0.6 菜单兜底)
    """
    tools = _channel_visible_tools(db, account_id)
    if not tools:
        return {"status": "menu", "menu": []}
    menu = [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "category": t.category or "other",
            "interactive": bool(t.interactive),  # 前端据此决定进会话页还是单轮执行
        }
        for t in tools
    ]

    hint = (attachment_hint or "").strip()[:800]
    # 召回：全量收缩到 top-K，工具再多也不撑爆上下文（附件内容一并参与召回）
    recall_text = (f"{text or ''} {hint}").strip()
    candidates = recall_topk(db, tools, recall_text)
    # 会话内延续先验：上一个工具无论召回与否都进入候选
    if prior_tool is not None:
        candidates = [prior_tool] + [t for t in candidates if t.id != prior_tool.id]
    gateway = LLMGateway(db)
    tool_list = [
        {
            "id": t.id,
            "name": t.name,
            "when": t.description,
            "needs_attachment": bool(t.needs_attachment),
            "price_quota": t.price_quota,
            "previous_in_session": bool(prior_tool is not None and t.id == prior_tool.id),
        }
        for t in candidates
    ]

    system = (
        "你是一个路由分类器。根据用户需求从候选工具中选择最合适的一个。"
        '只返回 JSON：{"tool_id": string, "confidence": float(0-1), "extracted": object, "needs_menu": bool}。'
        "confidence 反映把握：确定选对 0.85+；大概但不肯定 0.6-0.85；无法判断返回 needs_menu=true。"
        "若用户只上传了附件没写需求，请根据附件内容判断意图。"
        "重要：候选中标记 previous_in_session=true 的是本会话刚用过的工具；"
        "如果用户输入没有明确要求换工具、更像是上一个任务的延续（例如继续给出待处理的内容），"
        "应优先选择该工具并给出 0.9+ 的置信度。"
    )
    prior_note = ""
    if prior_tool is not None:
        prior_note = f"\n本会话上一个任务使用：{prior_tool.name}（id={prior_tool.id}）"
    if hint:
        user = (
            f"用户需求：{text or '（未填写，仅上传了附件）'}{prior_note}\n"
            f"用户上传的附件（节选）：\n{hint}\n"
            f"候选工具：{json.dumps(tool_list, ensure_ascii=False)}"
        )
    else:
        user = f"用户需求：{text}{prior_note}\n候选工具：{json.dumps(tool_list, ensure_ascii=False)}"

    resp = gateway_call(gateway, tool_list, system, user)
    try:
        data = json.loads(resp["content"])
    except Exception:  # noqa: BLE001  分类器输出异常 -> 菜单兜底
        return {"status": "menu", "menu": menu}

    cid = data.get("tool_id")
    try:
        conf = float(data.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    chosen = next((t for t in candidates if t.id == cid), None)

    # 确定性加成：用户输入中**完整出现**了候选工具名（如「翻译成英文」含「翻译」，
    # 但不含「字幕翻译/学术翻译」）→ 该工具就是用户字面所指，置信度拉到直接确认档。
    # 抑制近义工具（字幕翻译/学术翻译…）在同义查询上的 clarify 抖动。
    name_hits = [t for t in candidates if t.name and t.name in (text or "")]
    if len(name_hits) == 1:
        chosen = name_hits[0]
        conf = max(conf, 0.92) if (chosen and cid == chosen.id) else 0.92

    if data.get("needs_menu") or not chosen or conf < 0.6:
        return {"status": "menu", "menu": menu}

    base = {
        "status": "confirm",
        "tool_id": cid,
        "tool_name": chosen.name,
        "confidence": conf,
        "price_quota": effective_price_quota(chosen, channel),
        "needs_attachment": bool(chosen.needs_attachment),
        "interactive": bool(chosen.interactive),  # 交互式工具 -> 小程序进会话页
    }
    if conf < 0.85:
        base["status"] = "clarify"
        base["clarify"] = f"你想使用「{chosen.name}」吗？可补充更多细节后确认。"
    return base


def gateway_call(gateway: LLMGateway, tool_list: list[dict], system: str, user: str) -> dict:
    return gateway.call(
        "classifier",
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        json_mode=True,
        tools=tool_list,
    )
