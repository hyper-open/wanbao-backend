"""会话步骤执行器（worker 内运行）—— 按「子任务 (turn)」跑/恢复交互图。

职责：跑/恢复交互图，并把**图状态单向投影**到业务表。
- 图的权威状态在 `lg_checkpoint*`（LangGraph checkpointer），**每个子任务一个独立线程** `{session_id}:{seq}`
- 本模块是 `session_turn` / `tool_session` / `session_message` 的**唯一写入点**（避免两处状态冲突）
- `tool_session` 是会话容器，其同名列只是 **active turn 的镜像**（既有读者无需改动）

幂等守卫：`turn.status != "running"` 或该 turn 不是会话的 active turn → 直接返回
（队列重复投递 / 重放 / 旧子任务的迟到消息都安全）。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from langgraph.types import Command
from sqlalchemy import func

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.llm.checkpoint import MySQLCheckpointSaver
from app.models import Attachment, SessionMessage, SessionTurn, Tool, ToolInvocation, ToolSession, utcnow
from app.services.agent.session import build_graph
from app.services.billing import refund
from app.services.content_security import ContentRejected

# 等待用户输入的 turn 状态
WAITING = ("awaiting_answer", "awaiting_confirm")
# 仍在进行中（占用会话）的 turn 状态
ACTIVE_TURN = ("running", "awaiting_answer", "awaiting_confirm")


def _thread_id(session_id: int, seq: int) -> str:
    """子任务的 checkpoint 线程名（每 turn 一个独立线程）。"""
    return f"{session_id}:{seq}"


def _cfg(session_id: int, seq: int) -> dict:
    return {"configurable": {"thread_id": _thread_id(session_id, seq)}}


def _active_turn(db, session_id: int) -> Optional[SessionTurn]:
    return (
        db.query(SessionTurn)
        .filter(SessionTurn.session_id == session_id, SessionTurn.status.in_(ACTIVE_TURN))
        .order_by(SessionTurn.seq.desc())
        .first()
    )


def _mirror_session(db, s: ToolSession, turn: SessionTurn) -> None:
    """把 active turn 的关键字段镜像到会话容器（既有读者依赖这些列）。"""
    active = turn.status in ACTIVE_TURN
    s.tool_id = turn.tool_id
    s.status = turn.status if active else "awaiting_turn"
    s.input_text = turn.input_text
    s.attachment_ids = turn.attachment_ids
    s.attachment_text = turn.attachment_text
    s.analysis = turn.analysis
    s.facts = turn.facts
    s.current_question = turn.current_question if active else None
    s.round_count = turn.round_count
    s.max_rounds = turn.max_rounds
    s.quota_price = turn.quota_price
    s.unit_price_snapshot = turn.unit_price_snapshot
    s.amount_charged = turn.amount_charged
    s.charged = turn.charged
    s.invocation_id = turn.invocation_id
    s.active_turn_id = turn.id if active else None
    s.turn_count = max(int(s.turn_count or 0), int(turn.seq or 0))
    # 会话累计成本 = 各子任务之和（日成本上限查询依赖它）
    total = (
        db.query(func.coalesce(func.sum(SessionTurn.llm_cost), 0))
        .filter(SessionTurn.session_id == s.id)
        .scalar()
    )
    s.llm_cost = round(float(total or 0), 6)


# ---------------- 失败处理（含已扣次退次） ----------------
def _fail(session_id: int, turn_id: int, err: str) -> None:
    """标记子任务失败，并退还该子任务已扣的次数（分析费 / 含生成费）。"""
    db = SessionLocal()
    try:
        turn = db.get(SessionTurn, turn_id)
        if not turn:
            return
        prev_status = turn.status
        turn.status = "failed"
        turn.err = err[:2000]
        turn.step_started_at = None
        turn.finished_at = utcnow()
        # 运营日统计：终态迁移（failed）计一次（成本计入，收入不计——会退次）
        if prev_status != "failed":
            _stats_daily_add(db, turn, "failed")
        inv = db.get(ToolInvocation, turn.invocation_id) if turn.invocation_id else None
        charged = 0
        if inv and inv.status in ("running", "pending"):
            charged = int(inv.quota_charged or 0)
            inv.status = "failed"
            inv.err = err[:2000]
            inv.finished_at = utcnow()
            inv.cost_amount = round(float(turn.llm_cost or 0), 6)
            if charged > 0:
                inv.amount_charged = 0  # 已退次，本次不计费
        s = db.get(ToolSession, session_id)
        if s:
            _mirror_session(db, s, turn)
        db.commit()
        if charged > 0:
            try:
                refund(db, inv.user_id, charged, inv.id, account_id=inv.channel_account_id)
            except Exception:  # noqa: BLE001  退次失败不掩盖主错误
                db.rollback()
    finally:
        db.close()


# ---------------- 投影：图状态 → 业务表 ----------------
def _stats_daily_add(db, turn, status: str, seconds: int = 0) -> None:
    """运营日统计累加：仅子任务进入终态（success/failed）时调用一次。"""
    from app.models import ToolStatsDaily
    from app.core.timeutil import to_local

    day = to_local(turn.finished_at or utcnow()).date() if turn.finished_at else to_local(utcnow()).date()
    row = db.get(ToolStatsDaily, (day, str(turn.tool_id)))
    if not row:
        row = ToolStatsDaily(stat_date=day, tool_id=str(turn.tool_id))
        db.add(row)
    row.calls = int(row.calls or 0) + 1
    if status == "success":
        row.success = int(row.success or 0) + 1
        row.quota_charged = int(row.quota_charged or 0) + int(turn.quota_price or 0)
        row.amount_charged = float(row.amount_charged or 0) + float(turn.amount_charged or 0)
        row.total_seconds = int(row.total_seconds or 0) + int(seconds or 0)
    else:
        row.failed = int(row.failed or 0) + 1
    row.llm_cost = float(row.llm_cost or 0) + float(turn.llm_cost or 0)
    # 失败率异常告警：当日调用 ≥10 次且失败率 ≥20%
    calls = int(row.calls or 0)
    if calls >= 10:
        fail_rate = int(row.failed or 0) / calls
        if fail_rate >= 0.2:
            from app.services.ratelimit import _alert

            _alert(
                f"tool_fail_rate:{turn.tool_id}",
                f"工具「{turn.tool_id}」今日失败率 {fail_rate*100:.0f}%（{row.failed}/{calls}），请检查 prompt 或依赖",
            )


def _project(session_id: int, turn_id: int, out: dict) -> None:
    db = SessionLocal()
    try:
        s = db.get(ToolSession, session_id)
        turn = db.get(SessionTurn, turn_id)
        if not s or not turn:
            return
        tool = db.get(Tool, turn.tool_id)
        prev_status = turn.status  # 终态迁移判定用（success/failed 只计一次）

        # 全局日 LLM 成本累计：增量 = 本次产出后的 turn 成本 - 之前的（含免费阶段）
        from app.services import appsettings
        from app.services.ratelimit import global_daily_cost_add

        prev_cost = float(turn.llm_cost or 0)
        global_daily_cost_add(
            float(out.get("llm_cost") or 0) - prev_cost,
            limit_yuan=appsettings.get_num("rl_global_daily_cost", 10),
        )

        turn.llm_cost = float(out.get("llm_cost") or 0)
        turn.prompt_tokens = int(out.get("prompt_tokens") or 0)
        turn.completion_tokens = int(out.get("completion_tokens") or 0)
        turn.round_count = int(out.get("round") or 0)
        turn.step_started_at = None
        turn.expires_at = utcnow() + timedelta(minutes=settings.SESSION_TTL_MINUTES)
        s.expires_at = turn.expires_at

        # 分析结果（有实质内容才记录；轻量工具跳过分析 → 空结果不留痕）
        analysis = out.get("analysis") or {}
        has_analysis = bool(
            analysis.get("summary") or analysis.get("missing") or analysis.get("suspicious")
        )
        if has_analysis and not turn.analysis:
            turn.analysis = analysis
            db.add(
                SessionMessage(
                    session_id=s.id, turn_seq=turn.seq, role="assistant", type="analysis",
                    content=analysis.get("summary") or "", payload=analysis,
                )
            )

        intr = out.get("__interrupt__")
        kind = None
        if intr:
            val = getattr(intr[0], "value", None) or {}
            kind = val.get("type")

        q = out.get("question")
        if kind == "question" and q:
            if (turn.current_question or {}).get("text") != q.get("text"):
                turn.current_question = q
                db.add(
                    SessionMessage(
                        session_id=s.id, turn_seq=turn.seq, role="assistant", type="question",
                        content=q.get("text") or "", payload=q,
                    )
                )
            turn.status = "awaiting_answer"
        elif kind == "confirm_charge":
            turn.current_question = None
            turn.status = "awaiting_confirm"
        elif intr:
            turn.status = "awaiting_answer"  # 未知中断类型：保守等待用户
        else:
            # 本子任务跑完 → 产出完成（会话仍可继续下一个子任务）
            turn.status = "success"
            turn.current_question = None
            turn.finished_at = utcnow()
            turn.result_text = out.get("result") or ""
            turn.result_ref = out.get("result_ref")
            # 运营日统计：终态迁移（success）计一次
            if prev_status != "success":
                seconds = 0
                if turn.created_at and turn.finished_at:
                    seconds = max(0, int((turn.finished_at - turn.created_at).total_seconds()))
                _stats_daily_add(db, turn, "success", seconds)
            inv = db.get(ToolInvocation, turn.invocation_id) if turn.invocation_id else None
            if inv:
                inv.status = "success"
                inv.result_text = out.get("result") or ""
                inv.result_ref = out.get("result_ref") or inv.result_ref
                inv.model_used = out.get("model_used") or ""
                # 免费阶段成本并入本次调用，喂给 §16.4 单工具成本告警
                inv.cost_amount = round(float(out.get("llm_cost") or 0), 6)
                inv.finished_at = utcnow()
            if tool:
                tool.usage_count = (tool.usage_count or 0) + 1
            # 优化前/后对比（可配置；前端据此在会话页展示对比卡）
            diff = out.get("diff")
            if diff:
                db.add(
                    SessionMessage(
                        session_id=s.id, turn_seq=turn.seq, role="assistant", type="diff",
                        content=diff.get("summary") or "", payload=diff,
                    )
                )
            db.add(
                SessionMessage(
                    session_id=s.id, turn_seq=turn.seq, role="assistant", type="result",
                    content=(out.get("result") or "")[:4000],
                )
            )

        _mirror_session(db, s, turn)
        db.commit()
    finally:
        db.close()


# ---------------- 主入口 ----------------
def run_session_step(session_id: int, turn_id: Optional[int] = None, resume: Optional[dict] = None) -> None:
    db = SessionLocal()
    resolved_id: Optional[int] = turn_id
    try:
        s = db.get(ToolSession, session_id)
        if not s or s.status in ("cancelled", "expired", "failed"):
            return  # 会话已终止
        turn = db.get(SessionTurn, turn_id) if turn_id else _active_turn(db, session_id)
        if not turn or turn.session_id != session_id:
            return
        # 幂等守卫：必须是「进行中」且是会话当前的 active turn
        if turn.status != "running":
            return
        if s.active_turn_id and s.active_turn_id != turn.id:
            return  # 不是当前子任务（迟到的重复消息）
        resolved_id = turn.id
        tool = db.get(Tool, turn.tool_id)
        if not tool:
            turn.status = "failed"
            turn.err = "tool missing"
            turn.finished_at = utcnow()
            _mirror_session(db, s, turn)
            db.commit()
            return

        # 附件解析（仅首次；文本缓存进 turn，避免每轮重复解析）
        if turn.attachment_text is None and turn.attachment_ids:
            from app.services.attachment import clip_text, ensure_parsed, is_image
            from app.services.content_security import check_attachment

            texts = []
            image_keys = []
            for aid in turn.attachment_ids:
                att = db.get(Attachment, aid)
                if not att:
                    continue
                if is_image(att):
                    image_keys.append(att.object_key)
                    continue
                t = ensure_parsed(db, att)
                if t:
                    check_attachment(t, db=db, user_id=s.user_id)
                    t = clip_text(t, settings.ATTACHMENT_TEXT_MAX_CHARS)
                    texts.append(f"【{att.filename or '附件'}】\n{t}")
            # 合计也设上限：多附件叠加同样会撑爆单次请求
            turn.attachment_text = clip_text(
                "\n\n".join(texts), settings.SESSION_ATTACHMENT_TEXT_MAX_CHARS
            )
            turn.attachment_images = image_keys or None  # 图片不进文本解析，直供视觉模型
            db.commit()

        payload = (
            {
                "tool_id": turn.tool_id,
                "channel": s.channel,
                "user_id": s.user_id,
                "input_text": turn.input_text or "",
                "attachment_text": turn.attachment_text or "",
                "attachment_images": turn.attachment_images or [],
                "facts": {},
                "round": 0,
                "llm_cost": 0.0,
            }
            if resume is None
            else Command(resume=resume)
        )
        graph = build_graph()
        # 流式开关：开启时注册增量发布回调（produce 节点按 invocation_id 取用）
        from app.services import appsettings
        from app.worker import stream_registry

        streaming = appsettings.get_flag(appsettings.STREAM_ENABLED)
        if streaming and turn.invocation_id:
            stream_registry.register(turn.invocation_id, session_id, turn.seq)
            # 恢复图前先发 start（确认扣费后前端立即打开流连接）
            cb = stream_registry.get(turn.invocation_id)
            cb({"type": "start"})
        try:
            out = graph.invoke(payload, config=_cfg(session_id, turn.seq))
        finally:
            if streaming and turn.invocation_id:
                cb = stream_registry.get(turn.invocation_id)
                if cb:
                    cb({"type": "done"})
                stream_registry.pop(turn.invocation_id)
        _project(session_id, turn.id, out)
    except ContentRejected as e:
        if resolved_id:
            _fail(session_id, resolved_id, f"内容安全拦截（{e.stage}）")
    except Exception as e:  # noqa: BLE001
        if resolved_id:
            _fail(session_id, resolved_id, str(e)[:2000])
    finally:
        db.close()


def _close_turn_inv(db, s: ToolSession, turn: SessionTurn, status: str, err: str) -> None:
    """回收子任务时收尾调用记录，并退还本次已扣的次数（保留免费阶段成本）。"""
    inv = db.get(ToolInvocation, turn.invocation_id) if turn.invocation_id else None
    charged = 0
    if inv and inv.status in ("running", "pending"):
        charged = int(inv.quota_charged or 0)
        inv.status = status
        inv.err = inv.err or err
        inv.cost_amount = round(float(turn.llm_cost or 0), 6)
        inv.finished_at = utcnow()
        if charged > 0:
            inv.amount_charged = 0  # 已退次
    db.commit()
    if charged > 0:
        try:
            refund(db, inv.user_id, charged, inv.id, account_id=inv.channel_account_id)
        except Exception:  # noqa: BLE001
            db.rollback()


def reap_sessions() -> int:
    """① running 卡死的子任务 → failed（已扣次则退次）；
    ② 等待用户的子任务过期 → expired 并清理其 checkpoint；
    ③ 无进行中子任务且会话过期的会话 → expired。"""
    db = SessionLocal()
    n = 0
    saver = MySQLCheckpointSaver()
    try:
        now = utcnow()

        # ① 卡死在 running 的子任务
        stuck = (
            db.query(SessionTurn)
            .filter(
                SessionTurn.status == "running",
                SessionTurn.step_started_at.isnot(None),
                SessionTurn.step_started_at
                < now - timedelta(minutes=settings.SESSION_STEP_TIMEOUT_MINUTES),
            )
            .limit(50)
            .all()
        )
        for t in stuck:
            s = db.get(ToolSession, t.session_id)
            t.status = "failed"
            t.err = f"timeout: running > {settings.SESSION_STEP_TIMEOUT_MINUTES}min"
            t.step_started_at = None
            t.finished_at = now
            if s:
                _close_turn_inv(db, s, t, "failed", t.err)
                _mirror_session(db, s, t)
            n += 1

        # ② 等待用户但已过期的子任务
        expired_turns = (
            db.query(SessionTurn)
            .filter(SessionTurn.status.in_(WAITING), SessionTurn.expires_at < now)
            .limit(50)
            .all()
        )
        for t in expired_turns:
            s = db.get(ToolSession, t.session_id)
            t.status = "expired"
            t.err = "session expired"
            t.current_question = None
            t.finished_at = now
            if s:
                _close_turn_inv(db, s, t, "expired", "session expired")
                _mirror_session(db, s, t)
            db.commit()
            try:
                saver.delete_thread(_thread_id(t.session_id, t.seq))  # 清理 checkpoint，避免表膨胀
            except Exception:  # noqa: BLE001
                pass
            n += 1

        # ③ 会话整体过期（无进行中子任务，且已过 TTL）
        idle = (
            db.query(ToolSession)
            .filter(
                ToolSession.status == "awaiting_turn",
                ToolSession.expires_at.isnot(None),
                ToolSession.expires_at < now,
            )
            .limit(50)
            .all()
        )
        for s in idle:
            s.status = "expired"
            s.err = s.err or "session expired (idle)"
            s.active_turn_id = None
            s.current_question = None
            n += 1

        db.commit()
    finally:
        db.close()
    return n
