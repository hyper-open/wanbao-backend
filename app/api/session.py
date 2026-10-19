"""交互式多轮会话接口（§6.5「工具图可含人工介入节点」）。

计费：**分析 / 追问 / 回答全程免费**；仅 `/confirm` 扣次一次，之后追问产物同属一次会话。
执行：全部异步（入队 → worker 跑/恢复图 → 前端轮询 `GET /session/{id}`）。

与既有 `/api/agent/confirm`（非交互式工具）**并存不互斥**，后者行为不变。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.llm.checkpoint import MySQLCheckpointSaver
from app.core.security import get_current_openid
from app.core.timeutil import local_day_start_utc, local_iso
from app.models import Attachment, SessionMessage, SessionTurn, Tool, ToolInvocation, ToolSession, User, utcnow
from app.services.agent.router import recognize
from app.services.agent.session import session_defaults
from app.services.appsettings import get_flag, get_num, STREAM_ENABLED
from app.services.auth import account_id_for
from app.services.billing import (
    consume,
    effective_price_quota,
    quota_remain,
    refund,
    user_id_by_openid,
    user_unit_price,
)
from app.services.content_security import ContentRejected, check_input
from app.services.ratelimit import allow
from app.worker.queue import enqueue

router = APIRouter()

# 未完成（未扣费）的活跃会话状态
ACTIVE_STATES = ("running", "awaiting_answer", "awaiting_confirm")
# 仍在进行中的子任务状态（占用会话，不能再追加新子任务）
TURN_ACTIVE = ("running", "awaiting_answer", "awaiting_confirm")


def _session_ttl_minutes() -> int:
    """会话有效期（后台可调 session_ttl_minutes，仅对新创建的会话/追问生效）。"""
    from app.services import appsettings

    return appsettings.get_num("session_ttl_minutes", settings.SESSION_TTL_MINUTES)



def _require_user(db: Session, openid: str) -> int:
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401, "未登录")
    u = db.get(User, user_id)
    if u and u.status == "ban":
        raise HTTPException(403, "账号已被封禁")
    return user_id


def _client_ip(request: Request) -> str:
    """Nginx 前置时取真实 IP（防批量注册账号刷分析额度）。"""
    if request is None:
        return "unknown"
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _guard_session_rate(openid: str, request: Request) -> None:
    """新建会话频率限制：按用户 + 按 IP（防批量账号刷分析额度）。

    阈值后台动态可调（app_setting：rl_session_user_10m / rl_session_ip_10m），
    未设置回落 config 默认值。
    """
    from app.services import appsettings

    if not allow(
        f"session:{openid}",
        limit=appsettings.get_num("rl_session_user_10m", settings.SESSION_CREATE_PER_10MIN),
        window_seconds=600, category="session_user",
    ):
        raise HTTPException(429, "操作过于频繁，请稍后再试")
    if not allow(
        f"session:ip:{_client_ip(request)}",
        limit=appsettings.get_num("rl_session_ip_10m", settings.SESSION_CREATE_IP_PER_10MIN),
        window_seconds=600, category="session_ip",
    ):
        raise HTTPException(429, "操作过于频繁，请稍后再试")


def _load(db: Session, sid: int, user_id: int) -> ToolSession:
    s = (
        db.query(ToolSession)
        .filter(ToolSession.id == sid, ToolSession.user_id == user_id)
        .first()
    )
    if not s:
        raise HTTPException(404, "会话不存在")
    return s


def _guard_expired(s: ToolSession) -> None:
    if s.expires_at and s.expires_at < utcnow():
        raise HTTPException(409, "会话已过期，请重新发起")


def _msg(db: Session, s: ToolSession, role: str, type_: str, content: str, payload=None, turn_seq=None) -> None:
    db.add(
        SessionMessage(
            session_id=s.id, turn_seq=turn_seq, role=role, type=type_, content=content, payload=payload
        )
    )


def _active_turn(db: Session, sid: int) -> SessionTurn | None:
    """会话当前进行中的子任务（最多一个）。"""
    return (
        db.query(SessionTurn)
        .filter(SessionTurn.session_id == sid, SessionTurn.status.in_(TURN_ACTIVE))
        .order_by(SessionTurn.seq.desc())
        .first()
    )


# ---------------- 建会话（免费：跑分析） ----------------
def _menu(db: Session) -> list[dict]:
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "category": t.category or "other",
            "interactive": bool(t.interactive),
        }
        for t in db.query(Tool).filter(Tool.enabled == True).all()  # noqa: E712
    ]


def _attachment_hint(db: Session, attachment_ids: list, user_id: int) -> str:
    """取附件文本摘要供识别使用；无附件/解析失败返回空串。"""
    if not attachment_ids:
        return ""
    from app.services.attachment import ensure_parsed

    parts = []
    for aid in attachment_ids[:3]:
        att = db.get(Attachment, aid)
        if not att or att.user_id != user_id:
            continue
        try:
            t = ensure_parsed(db, att) or ""
        except Exception:  # noqa: BLE001  解析失败不阻塞识别
            t = ""
        parts.append(f"{att.filename or '附件'}：{t[:600]}")
    return "\n".join(parts)[:800]


def _drop_turn_thread(sid: int, seq: int) -> None:
    try:
        MySQLCheckpointSaver().delete_thread(f"{sid}:{seq}")
    except Exception:  # noqa: BLE001  清理失败不影响取消结果
        pass


def _cancel_turn(db: Session, s: ToolSession, turn: SessionTurn, reason: str = "用户取消") -> None:
    """结束一个未扣费的子任务并清理其 checkpoint；已扣费（生成中）的不能取消。"""
    if turn.charged and turn.status == "running":
        raise HTTPException(409, "已扣费开始生成，无法取消")
    turn.status = "cancelled"
    turn.err = turn.err or reason
    turn.current_question = None
    turn.finished_at = utcnow()
    # 同步收尾该子任务的调用记录：退还已扣的（分析）次数
    inv = db.get(ToolInvocation, turn.invocation_id) if turn.invocation_id else None
    charged = 0
    if inv and inv.status in ("running", "pending"):
        charged = int(inv.quota_charged or 0)
        inv.status = "cancelled"
        inv.err = inv.err or reason
        inv.cost_amount = round(float(turn.llm_cost or 0), 6)
        inv.finished_at = utcnow()
        if charged > 0:
            inv.amount_charged = 0  # 已退次
    # 会话容器：active turn 被取消 → 回到可继续状态
    s.active_turn_id = None
    s.status = "awaiting_turn"
    s.current_question = None
    db.commit()
    if charged > 0:
        try:
            refund(db, inv.user_id, charged, inv.id, account_id=inv.channel_account_id)
        except Exception:  # noqa: BLE001  退次失败不掩盖取消结果
            db.rollback()
    _drop_turn_thread(turn.session_id, turn.seq)


def _cancel_session(db: Session, s: ToolSession, reason: str = "用户取消") -> None:
    """结束整个会话：取消所有未完成的子任务、清理全部 checkpoint。"""
    turns = (
        db.query(SessionTurn)
        .filter(SessionTurn.session_id == s.id)
        .order_by(SessionTurn.seq)
        .all()
    )
    for t in turns:
        if t.status in TURN_ACTIVE:
            _cancel_turn(db, s, t, reason)
        elif t.status in ("success", "failed", "expired"):
            _drop_turn_thread(s.id, t.seq)  # 终态子任务的线程不再需要
    s.status = "cancelled"
    s.err = s.err or reason
    s.current_question = None
    s.active_turn_id = None
    db.commit()


def _create_turn(
    db: Session, s: ToolSession, tool: Tool, text: str, attachment_ids: list, seq: int, user_id: int
) -> tuple[SessionTurn, ToolInvocation]:
    """建一个子任务 + 它的调用记录，并按该工具的 analyze_quota 扣分析费。

    每个子任务一行调用记录（含未确认的免费子任务）：便于成本核算，且「记录」页
    只需查 tool_invocation 一张表。确认生成时**更新**这条记录，不再新建。
    """
    d = session_defaults(tool)
    turn = SessionTurn(
        session_id=s.id,
        seq=seq,
        tool_id=tool.id,
        status="running",
        input_text=text,
        attachment_ids=attachment_ids,
        quota_price=int(effective_price_quota(tool, s.channel)),
        max_rounds=int(d["max_rounds"]),  # 轻量工具为 0（跳过追问）
        step_started_at=utcnow(),
        expires_at=utcnow() + timedelta(
            minutes=_session_ttl_minutes(),
        ),
    )
    db.add(turn)
    db.commit()
    db.refresh(turn)

    inv = ToolInvocation(
        user_id=user_id,
        tool_id=tool.id,
        tool_version=tool.version,
        channel_account_id=s.channel_account_id,
        input_text=text,
        attachment_ids=attachment_ids,
        intent_confidence=1.0,
        status="running",
        quota_charged=0,
        amount_charged=0,
        cost_amount=0,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    turn.invocation_id = inv.id
    db.commit()

    # 分析费：进入子任务即扣（分析/追问都算这一笔；失败/取消/过期会退还）
    analyze_price = int(tool.analyze_quota or 0)
    if analyze_price > 0:
        try:
            consume(db, user_id, analyze_price, inv.id, account_id=s.channel_account_id)
        except ValueError:
            # 余额不足：回滚刚建的空子任务，直接报错
            db.delete(inv)
            db.delete(turn)
            db.commit()
            raise HTTPException(402, "insufficient_quota")
        unit = float(user_unit_price(db, user_id))
        inv.quota_charged = analyze_price
        inv.unit_price_snapshot = round(unit, 4)
        inv.amount_charged = round(analyze_price * unit, 2)
        db.commit()

    # 会话容器：镜像「当前子任务」（既有读者依赖这些列）
    s.active_turn_id = turn.id
    s.tool_id = tool.id
    s.turn_count = max(int(s.turn_count or 0), seq)
    s.status = "running"
    s.step_started_at = utcnow()
    s.expires_at = turn.expires_at
    s.quota_price = turn.quota_price
    s.max_rounds = turn.max_rounds
    # 新子任务：重置上一子任务的状态，避免串味
    s.charged = False
    s.invocation_id = inv.id
    s.analysis = None
    s.facts = None
    s.current_question = None
    s.round_count = 0
    s.amount_charged = 0
    s.unit_price_snapshot = None
    db.commit()
    return turn, inv


def _pick_tool(
    db: Session, text: str, attachment_ids: list, user_id: int, channel: str, tid: str | None,
    last_tool=None, account_id: str | None = None,
):
    """选定工具：带 tool_id 直接用；否则走意图识别（带会话内上一工具先验）。

    - clarify 且选中的就是上一工具 → 自动沿用（用户在连续喂内容，不打断）
    - menu 兜底且有上一工具 → 回落沿用上一工具（会话内默认延续）
    返回 (tool, early)：early 非空表示应把该结构直接回给前端（menu / clarify）。
    """
    if tid:
        tool = db.query(Tool).filter(Tool.id == tid, Tool.enabled == True).first()  # noqa: E712
        if not tool:
            raise HTTPException(404, "工具不存在或已下架")
        return tool, None
    hint = _attachment_hint(db, attachment_ids, user_id)
    r = recognize(db, text, channel=channel, attachment_hint=hint, prior_tool=last_tool)
    st = r.get("status")
    if st == "menu":
        if last_tool is not None:
            return last_tool, None  # 会话内兜底：沿用上一个工具，保持任务延续
        return None, {"status": "menu", "menu": r.get("menu") or _menu(db)}
    chosen = db.query(Tool).filter(Tool.id == r.get("tool_id"), Tool.enabled == True).first()  # noqa: E712
    if not chosen:
        if last_tool is not None:
            return last_tool, None
        return None, {"status": "menu", "menu": _menu(db)}
    if st == "clarify":
        if last_tool is not None and chosen.id == last_tool.id:
            return chosen, None  # 分类器倾向的就是上一工具 → 直接延续，不再追问
        return chosen, {
            "status": "clarify",
            "tool_id": chosen.id,
            "tool_name": chosen.name,
            "clarify": r.get("clarify") or f"你想使用「{chosen.name}」吗？",
            "needs_attachment": bool(chosen.needs_attachment),
        }
    return chosen, None


def _stream_flag() -> bool:
    """流式输出开关（app_setting，后台即时切换；关闭=完全走旧轮询链路）。"""
    return get_flag(STREAM_ENABLED)


def _start_session(db: Session, user_id: int, tool: Tool, text: str, attachment_ids: list, channel: str, account_id: str | None = None) -> dict:
    """校验 + 防白嫖 + 建会话与首个子任务，入队；被 /session 与 /chat 共用。

    所有工具都进会话：多轮工具走「分析 + 追问」，简单工具（interactive=False）
    跳过分析，一次确认后直接产出（见 session_defaults）。
    """
    # 全站聚合限流 + 全局日成本闸（所有用户共享配额；动态阈值 rl_global_*，防整体被打爆）
    from app.services.ratelimit import allow as _gallow, global_daily_cost_get

    if not _gallow("global:session", limit=get_num("rl_global_session_per_min", 60), category="global_session"):
        raise HTTPException(429, "当前使用人数过多，请稍后再试")
    if global_daily_cost_get() >= get_num("rl_global_daily_cost", 10):
        raise HTTPException(429, "当前使用人数过多，请稍后再试")
    if tool.needs_attachment and not attachment_ids:
        raise HTTPException(400, "该工具需要上传附件")
    try:
        check_input(text or "", db=db, user_id=user_id)
    except ContentRejected:
        raise HTTPException(400, "内容不合规，已被拦截")

    # 防白嫖：会话开始已按工具「分析次数」扣费，这里再加一道自然日 LLM 成本上限，防止刷分析。
    # 免费次数本身走活动（签到 / 邀请），不设「每日免费名额」。
    day_start = local_day_start_utc()
    used_cost = (
        db.query(func.coalesce(func.sum(ToolSession.llm_cost), 0))
        .filter(ToolSession.user_id == user_id, ToolSession.created_at >= day_start)
        .scalar()
    )
    from app.services import appsettings

    if float(used_cost or 0) >= appsettings.get_float("session_daily_cost_limit", settings.SESSION_DAILY_COST_LIMIT):
        raise HTTPException(429, "今日分析用量已达上限，请明天再试或先充值")

    # 防白嫖：限制未付费的活跃会话数。**超出上限时自动结束最旧的**，
    # 而不是报错——否则中途退出的残留会话会把用户永久卡死（直到过期）。
    active = (
        db.query(ToolSession)
        .filter(
            ToolSession.user_id == user_id,
            ToolSession.status.in_(ACTIVE_STATES),
            ToolSession.charged == False,  # noqa: E712
        )
        .order_by(ToolSession.id.asc())
        .all()
    )
    from app.services import appsettings

    max_active = appsettings.get_num("session_max_active", settings.SESSION_MAX_ACTIVE)
    overflow = len(active) - (max(1, max_active) - 1)
    for old in active[: max(0, overflow)]:
        _cancel_session(db, old, reason="自动结束：未完成的会话过多")

    d = session_defaults(tool)
    price = int(effective_price_quota(tool, channel))
    s = ToolSession(
        user_id=user_id,
        tool_id=tool.id,
        channel=channel,
        channel_account_id=account_id,
        status="running",
        input_text=text,
        attachment_ids=attachment_ids,
        quota_price=price,
        max_rounds=int(d["max_rounds"]),
        step_started_at=utcnow(),
        expires_at=utcnow() + timedelta(minutes=_session_ttl_minutes()),
    )
    db.add(s)
    db.commit()
    db.refresh(s)

    # 第 1 个子任务（多工具会话：会话可继续追加子任务）
    turn, inv = _create_turn(db, s, tool, text, attachment_ids, seq=1, user_id=user_id)

    if text:
        _msg(db, s, "user", "text", text, turn_seq=turn.seq)
        db.commit()

    enqueue({"type": "session_turn_start", "session_id": s.id, "turn_id": turn.id})
    return {
        "session_id": s.id,
        "turn_id": turn.id,
        "seq": 1,
        "status": s.status,
        "stream": _stream_flag(),
        "free": False,
        "tool_id": tool.id,
        "tool_name": tool.name,
        "analyze_quota": int(tool.analyze_quota or 0),
        "price_quota": price,
        "max_rounds": turn.max_rounds,
        "expires_at": local_iso(turn.expires_at),
    }


@router.post("/session")
def create_session(
    body: dict = Body(default={}),
    request: Request = None,
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    user_id = _require_user(db, openid)
    _guard_session_rate(openid, request)

    tool = (
        db.query(Tool)
        .filter(Tool.id == body.get("tool_id"), Tool.enabled == True)  # noqa: E712
        .first()
    )
    if not tool:
        raise HTTPException(404, "工具不存在或已下架")

    return _start_session(
        db,
        user_id,
        tool,
        (body.get("input") or body.get("text") or "").strip(),
        body.get("attachment_ids") or [],
        (body.get("channel") or "wechat").lower(),
        account_id=account_id_for(db, openid),
    )


@router.post("/session/{sid}/turn")
def add_turn(
    sid: int,
    body: dict = Body(default={}),
    request: Request = None,
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """在同一个会话里追加一个子任务（可换工具）。

    - 带 `tool_id` 直接用该工具；不带则走意图识别（歧义返回 clarify/menu，前端确认后再调）
    - 要求：会话未结束/未过期、当前没有进行中的子任务、子任务数未达上限
    - `use_prev_result=true` 时把上一个子任务的产出节选注入本次输入（显式引用，默认不携带）
    """
    user_id = _require_user(db, openid)
    _guard_session_rate(openid, request)
    s = _load(db, sid, user_id)
    if s.status in ("cancelled", "expired", "failed"):
        raise HTTPException(409, "会话已结束，请新建会话")
    _guard_expired(s)
    if _active_turn(db, sid):
        raise HTTPException(409, "当前子任务尚未完成，请先完成或取消它")
    if int(s.turn_count or 0) >= settings.SESSION_MAX_TURNS_PER_SESSION:
        raise HTTPException(429, "本会话任务数已达上限，请新建会话")

    text = (body.get("input") or body.get("text") or "").strip()
    attachment_ids = body.get("attachment_ids") or []
    # 会话内上一工具（意图识别的延续先验）
    prev = (
        db.query(SessionTurn)
        .filter(SessionTurn.session_id == s.id, SessionTurn.tool_id.isnot(None))
        .order_by(SessionTurn.seq.desc())
        .first()
    )
    last_tool = db.get(Tool, prev.tool_id) if prev else None
    tool, early = _pick_tool(db, text, attachment_ids, user_id, s.channel, body.get("tool_id"), last_tool, account_id=s.channel_account_id)
    if early:
        return early
    if tool.needs_attachment and not attachment_ids:
        return {
            "status": "need_attachment",
            "tool_id": tool.id,
            "tool_name": tool.name,
            "interactive": bool(tool.interactive),
            "needs_attachment": True,
        }

    # 「基于上一条结果」：显式引用（默认不携带，避免上下文膨胀/跨工具语义污染）
    if body.get("use_prev_result"):
        prev = (
            db.query(SessionTurn)
            .filter(SessionTurn.session_id == sid, SessionTurn.result_text.isnot(None))
            .order_by(SessionTurn.seq.desc())
            .first()
        )
        if prev and prev.result_text:
            text = (text + "\n\n【上一任务结果（仅供参考）】\n" + prev.result_text[:4000]).strip()

    seq = int(s.turn_count or 0) + 1
    turn, _inv = _create_turn(db, s, tool, text, attachment_ids, seq=seq, user_id=user_id)
    if text:
        _msg(db, s, "user", "text", text, turn_seq=turn.seq)
        db.commit()

    enqueue({"type": "session_turn_start", "session_id": s.id, "turn_id": turn.id})
    return {
        "status": "session",
        "session_id": s.id,
        "turn_id": turn.id,
        "seq": seq,
        "stream": _stream_flag(),
        "tool_id": tool.id,
        "tool_name": tool.name,
        "analyze_quota": int(tool.analyze_quota or 0),
        "price_quota": int(turn.quota_price or 0),
        "max_rounds": turn.max_rounds,
        "expires_at": local_iso(turn.expires_at),
    }


# ---------------- 通用对话入口（识别前置） ----------------
@router.post("/chat")
def chat_dispatch(
    body: dict = Body(default={}),
    request: Request = None,
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """通用对话入口：带 tool_id 直接开会话；不带则先识别再决定。

    返回 status：session（已开会话）/ clarify（需确认）/ menu（候选菜单）/ need_attachment。
    **所有工具都进会话**（简单工具跳过分析与追问，只保留一次确认扣费）。
    """
    user_id = _require_user(db, openid)
    _guard_session_rate(openid, request)

    text = (body.get("input") or body.get("text") or "").strip()
    attachment_ids = body.get("attachment_ids") or []
    channel = (body.get("channel") or "wechat").lower()

    tool, early = _pick_tool(db, text, attachment_ids, user_id, channel, body.get("tool_id"), account_id=account_id_for(db, openid))
    if early:
        return early

    # 需要附件但还没上传 → 结构化返回（不是错误），让前端引导用户上传
    if tool.needs_attachment and not attachment_ids:
        return {
            "status": "need_attachment",
            "tool_id": tool.id,
            "tool_name": tool.name,
            "interactive": bool(tool.interactive),
            "needs_attachment": True,
        }

    res = _start_session(
        db, user_id, tool, text, attachment_ids, channel,
        account_id=account_id_for(db, openid),
    )
    # ⚠️ _start_session 返回里带会话真实状态（running）；对外的分发语义必须是 "session"，
    # 否则前端按 status 分流会落到兜底分支（历史 bug：dict 合并被后者的 status 覆盖）。
    res["session_status"] = res.get("status")
    res["status"] = "session"
    return res


# ---------------- 轮询会话状态 ----------------
@router.get("/session/{sid}")
def get_session(sid: int, db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    """会话全貌：容器字段 + 各子任务（含各自的 messages）。

    顶层同时保留 active turn 的镜像字段，兼容既有前端。
    """
    user_id = _require_user(db, openid)
    s = _load(db, sid, user_id)
    turns = (
        db.query(SessionTurn)
        .filter(SessionTurn.session_id == sid)
        .order_by(SessionTurn.seq)
        .all()
    )
    msgs = (
        db.query(SessionMessage)
        .filter(SessionMessage.session_id == sid)
        .order_by(SessionMessage.id)
        .limit(400)
        .all()
    )
    tids = {t.tool_id for t in turns if t.tool_id}
    tmap = {t.id: t.name for t in db.query(Tool).filter(Tool.id.in_(tids)).all()} if tids else {}

    all_msgs = [m.to_dict() for m in msgs]
    by_seq: dict = {}
    for d in all_msgs:
        by_seq.setdefault(d.get("turn_seq") or 1, []).append(d)

    active = next((t for t in turns if t.status in TURN_ACTIVE), None)
    ref = active or (turns[-1] if turns else None)  # 顶层兼容字段来源

    interrupt = None
    if ref and ref.status == "awaiting_answer" and ref.current_question:
        interrupt = {"type": "question", "question": ref.current_question}
    elif ref and ref.status == "awaiting_confirm":
        interrupt = {
            "type": "confirm_charge",
            "price_quota": ref.quota_price,
            "quota_remain": int(quota_remain(db, user_id)),
        }

    def _turn_dto(t: SessionTurn) -> dict:
        return {
            "turn_id": t.id,
            "seq": t.seq,
            "tool_id": t.tool_id,
            "tool_name": tmap.get(t.tool_id, t.tool_id or ""),
            "status": t.status,
            "analysis": t.analysis,
            "facts": t.facts,
            "round": t.round_count,
            "max_rounds": t.max_rounds,
            "quota_price": t.quota_price,
            "amount_charged": float(t.amount_charged or 0),
            "charged": bool(t.charged),
            "invocation_id": t.invocation_id,
            "current_question": t.current_question,
            "result_ref": t.result_ref,
            "err": t.err,
            "finished_at": local_iso(t.finished_at),
            "messages": by_seq.get(t.seq, []),
        }

    return {
        "session_id": s.id,
        "status": s.status,
        "stream": _stream_flag(),
        "turn_count": int(s.turn_count or 0),
        "active_turn_id": s.active_turn_id,
        "turns": [_turn_dto(t) for t in turns],
        # ---- 兼容旧字段（取自 active turn，无则最后一个）----
        "tool_id": ref.tool_id if ref else s.tool_id,
        "interrupt": interrupt,
        "analysis": ref.analysis if ref else None,
        "facts": ref.facts if ref else None,
        "round": ref.round_count if ref else 0,
        "max_rounds": ref.max_rounds if ref else s.max_rounds,
        "analyze_quota": int(getattr(db.get(Tool, ref.tool_id), "analyze_quota", 0) or 0) if ref else 0,
        "price_quota": ref.quota_price if ref else s.quota_price,
        "amount_charged": float(ref.amount_charged or 0) if ref else 0.0,
        "charged": bool(ref.charged) if ref else False,
        "quota_remain": int(quota_remain(db, user_id)),
        "invocation_id": ref.invocation_id if ref else None,
        "err": s.err,
        "expires_at": local_iso(s.expires_at),
        "messages": all_msgs,
    }


# ---------------- 回答追问 ----------------
@router.post("/session/{sid}/answer")
def answer(
    sid: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    user_id = _require_user(db, openid)
    s = _load(db, sid, user_id)
    turn = _active_turn(db, sid)
    if not turn or turn.status != "awaiting_answer":
        raise HTTPException(409, "当前没有等待回答的问题")
    _guard_expired(s)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "请填写回答内容")
    try:
        check_input(text, db=db, user_id=user_id)
    except ContentRejected:
        raise HTTPException(400, "内容不合规，已被拦截")

    _msg(
        db, s, "user", "answer", text,
        {"field": (turn.current_question or {}).get("field")}, turn_seq=turn.seq,
    )
    turn.status = "running"
    turn.step_started_at = utcnow()
    s.status = "running"
    s.step_started_at = turn.step_started_at
    db.commit()
    enqueue({"type": "session_resume", "session_id": sid, "turn_id": turn.id, "resume": {"answer": text}})
    return {"ok": True, "status": turn.status, "round": turn.round_count, "turn_id": turn.id}


# ---------------- 跳过剩余追问 ----------------
@router.post("/session/{sid}/skip")
def skip(sid: int, db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    user_id = _require_user(db, openid)
    s = _load(db, sid, user_id)
    turn = _active_turn(db, sid)
    if not turn or turn.status != "awaiting_answer":
        raise HTTPException(409, "当前没有进行中的追问")
    _guard_expired(s)
    _msg(db, s, "user", "answer", "（跳过追问，直接生成）", turn_seq=turn.seq)
    turn.status = "running"
    turn.step_started_at = utcnow()
    s.status = "running"
    s.step_started_at = turn.step_started_at
    db.commit()
    enqueue({"type": "session_resume", "session_id": sid, "turn_id": turn.id, "resume": {"skip": True}})
    return {"ok": True, "status": turn.status, "turn_id": turn.id}


# ---------------- 确认并扣次（产出） ----------------
@router.post("/session/{sid}/confirm")
def confirm(sid: int, db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    user_id = _require_user(db, openid)
    s = _load(db, sid, user_id)  # 归属校验
    # 行锁：防双击重复扣次（锁的是**子任务**行，多子任务下各扣各的）
    turn = (
        db.query(SessionTurn)
        .filter(SessionTurn.session_id == sid, SessionTurn.status.in_(TURN_ACTIVE))
        .order_by(SessionTurn.seq.desc())
        .with_for_update()
        .first()
    )
    if not turn:
        raise HTTPException(404, "没有进行中的子任务")
    # 幂等：该子任务已扣费且已有 invocation → 直接返回，不再扣次
    if turn.charged and turn.invocation_id:
        return {
            "invocation_id": turn.invocation_id, "session_id": sid,
            "turn_id": turn.id, "async": True, "idempotent": True,
        }
    if turn.status != "awaiting_confirm":
        raise HTTPException(409, "当前状态不可确认生成")
    _guard_expired(s)

    tool = db.get(Tool, turn.tool_id)
    price = int(effective_price_quota(tool, s.channel))
    unit = float(user_unit_price(db, user_id))
    inv = db.get(ToolInvocation, turn.invocation_id) if turn.invocation_id else None
    try:
        consume(
            db, user_id, price, turn.invocation_id,
            account_id=(inv.channel_account_id if inv else s.channel_account_id),
        )
    except ValueError:
        raise HTTPException(402, "insufficient_quota")

    # 子任务建立时已创建调用记录（含免费阶段），这里改为**更新**它
    if inv is None:  # 兜底：历史数据/异常
        inv = ToolInvocation(
            user_id=user_id,
            tool_id=tool.id,
            tool_version=tool.version,
            channel_account_id=s.channel_account_id,
            input_text=turn.input_text,
            attachment_ids=turn.attachment_ids,
            intent_confidence=1.0,
            status="pending",
            quota_charged=0,
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        turn.invocation_id = inv.id

    inv.status = "pending"
    # 生成/导出费：累加到已扣的分析费之上（一次调用记录，两笔合计）
    inv.quota_charged = int(inv.quota_charged or 0) + price
    inv.unit_price_snapshot = round(unit, 4)
    inv.amount_charged = round(int(inv.quota_charged) * unit, 2)
    inv.cost_amount = round(float(turn.llm_cost or 0), 6)  # 免费阶段成本并入
    turn.charged = True
    turn.quota_price = price
    turn.unit_price_snapshot = round(unit, 4)
    turn.amount_charged = round(price * unit, 2)
    turn.status = "running"
    turn.step_started_at = utcnow()
    # 会话容器镜像
    s.charged = True
    s.quota_price = price
    s.unit_price_snapshot = turn.unit_price_snapshot
    s.amount_charged = turn.amount_charged
    s.status = "running"
    s.step_started_at = turn.step_started_at
    db.commit()

    enqueue(
        {"type": "session_resume", "session_id": sid, "turn_id": turn.id, "resume": {"invocation_id": inv.id}}
    )
    return {
        "invocation_id": inv.id, "session_id": sid, "turn_id": turn.id,
        "seq": turn.seq, "stream": _stream_flag(),
        "status": turn.status, "async": True,
    }


# ---------------- 取消 ----------------
@router.post("/session/{sid}/cancel")
def cancel(sid: int, db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    """结束整个会话（取消所有未完成子任务并退次）。"""
    user_id = _require_user(db, openid)
    s = _load(db, sid, user_id)
    _cancel_session(db, s)
    return {"ok": True, "status": "cancelled"}


@router.post("/session/{sid}/turn/{seq}/cancel")
def cancel_turn(
    sid: int,
    seq: int,
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """只取消当前这一个子任务（会话保留，可继续做别的）。"""
    user_id = _require_user(db, openid)
    s = _load(db, sid, user_id)
    turn = (
        db.query(SessionTurn)
        .filter(SessionTurn.session_id == sid, SessionTurn.seq == seq)
        .first()
    )
    if not turn:
        raise HTTPException(404, "子任务不存在")
    if turn.status not in TURN_ACTIVE:
        raise HTTPException(409, "该子任务已结束，无需取消")
    _cancel_turn(db, s, turn)
    return {"ok": True, "status": "cancelled", "seq": seq}


# ---------------- 产出流式输出（SSE；开关关闭时前端自动回落轮询） ----------------
@router.get("/session/{sid}/stream")
def stream_session(
    sid: int,
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """SSE 增量流：订阅 Redis 频道，把 worker 产出的文本增量推给前端。

    - 事件：{"type":"start"|"delta"|"done", "seq":..., "text":...}（done 后连接关闭）
    - 心跳注释行防代理空闲断连；JSON ensure_ascii 保证字节流 ASCII 安全
    - 开关关闭时 worker 不发布事件，本连接只会收到心跳——前端以 done 为准，
      超时未收到 done 则回落轮询。
    """
    import json as _json
    import time as _time

    import redis as _redis

    from app.core.config import settings as _settings

    user_id = _require_user(db, openid)
    _load(db, sid, user_id)  # 归属校验

    r = _redis.Redis.from_url(_settings.REDIS_URL, decode_responses=True)
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(f"wanbao:stream:{sid}")

    def gen():
        try:
            yield "retry: 3000\n\n"
            last_beat = _time.time()
            while True:
                msg = pubsub.get_message(timeout=5.0)
                if msg is None:
                    if _time.time() - last_beat > 15:
                        yield ": keepalive\n\n"
                        last_beat = _time.time()
                    continue
                data = msg.get("data")
                if not data:
                    continue
                try:
                    payload = _json.loads(data)
                except Exception:  # noqa: BLE001
                    continue
                yield f"data: {_json.dumps(payload, ensure_ascii=True)}\n\n"
                if payload.get("type") in ("done", "error"):
                    break
        finally:
            try:
                pubsub.close()
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
