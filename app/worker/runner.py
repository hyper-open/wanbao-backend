"""异步任务执行：从队列取出 invocation，解析附件、运行工具图、回写结果。

失败则退次并标记 failed。重活在独立的 worker 线程/进程中完成，前端轮询获取结果。
reaper：兜底回收"卡在 pending"的任务（worker 崩溃丢消息等），超时标失败并退次。
"""
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Attachment, Tool, ToolInvocation
from app.services.agent.executor import run_tool
from app.services.attachment import clip_text, ensure_parsed
from app.services.billing import refund, _maybe_alert
from app.services.content_security import check_attachment, ContentRejected


def _utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run_invocation(inv_id: int) -> None:
    db: Session = SessionLocal()
    try:
        inv = db.get(ToolInvocation, inv_id)
        if not inv or inv.status != "pending":
            return
        tool = db.get(Tool, inv.tool_id)
        if not tool:
            inv.status = "failed"
            inv.err = "tool missing"
            db.commit()
            return

        # 解析附件文本；图片附件转交视觉模型（不抽文本）
        from app.services.attachment import is_image

        texts = []
        image_keys = []
        for aid in (inv.attachment_ids or []):
            att = db.get(Attachment, aid)
            if not att:
                continue
            if is_image(att):
                image_keys.append(att.object_key)
                continue
            if att:
                t = ensure_parsed(db, att)
                if t:
                    # 附件文本进入内容安全筛查（命中则退次并标记失败）
                    try:
                        check_attachment(t, db=db, user_id=inv.user_id)
                    except ContentRejected:
                        inv.status = "failed"
                        inv.err = "content rejected in attachment"
                        inv.finished_at = _utc()
                        inv.amount_charged = 0  # 已退次，本次不计费
                        db.commit()
                        refund(db, inv.user_id, inv.quota_charged or 0, inv.id)
                        return
                    texts.append(f"【{att.filename or 'attachment'}】\n{clip_text(t, settings.ATTACHMENT_TEXT_MAX_CHARS)}")

        result, model_used, result_ref, cost = run_tool(
            db, tool, inv.input_text or "", texts, inv.id, inv.user_id, image_keys
        )
        inv.status = "success"
        inv.result_text = result
        inv.model_used = model_used
        inv.result_ref = result_ref or inv.result_ref
        inv.cost_amount = cost  # 估算 LLM 成本落库（§16.4）
        inv.finished_at = _utc()
        tool.usage_count = (tool.usage_count or 0) + 1  # 热度计数（§16.1 工具发现）
        db.commit()
        _check_tool_cost_alert(db, tool)
    except Exception as e:  # noqa: BLE001
        db2 = SessionLocal()
        try:
            inv = db2.get(ToolInvocation, inv_id)
            if inv:
                inv.status = "failed"
                inv.err = str(e)[:2000]
                inv.finished_at = _utc()
                inv.amount_charged = 0  # 已退次，本次不计费
                db2.commit()
                refund(db2, inv.user_id, inv.quota_charged or 0, inv_id)
        finally:
            db2.close()
    finally:
        db.close()


def _check_tool_cost_alert(db: Session, tool: Tool) -> None:
    """防亏告警（§16.4-C）：单工具本月累计 LLM 成本超阈值 -> alert_log（当月按工具去重）。"""
    from app.services.analytics import _month_start

    if tool.cost_alert_threshold is not None:
        threshold = float(tool.cost_alert_threshold)
    else:
        # 全局兜底：后台「⚙ 限流参数」可调（tool_cost_alert_threshold），env 仅启动默认
        from app.services import appsettings

        threshold = appsettings.get_float("tool_cost_alert_threshold", settings.TOOL_COST_ALERT_THRESHOLD)
    if not threshold or threshold <= 0:
        return
    total = float(
        db.query(func.coalesce(func.sum(ToolInvocation.cost_amount), 0))
        .filter(
            ToolInvocation.tool_id == tool.id,
            ToolInvocation.created_at >= _month_start(),
        )
        .scalar() or 0
    )
    if total < threshold:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    month_key = f"{now.year}-{now.month:02d}"
    _maybe_alert(
        db, "tool_cost", month_key,
        f"[{month_key}][{tool.id}] 工具本月 LLM 成本 ¥{total:.2f} 已超阈值 ¥{threshold:.2f}，请校准 price_quota（§16.4-C）",
        {"tool_id": tool.id, "cost": round(total, 4), "threshold": threshold},
        dedup_key=tool.id,
    )


def reap_stale_pending() -> int:
    """回收超时任务：pending（队列堆积）与 running（执行中断，如发版重启杀掉 daemon 线程）→ 标 failed + 退次。"""
    cutoff = _utc() - timedelta(minutes=settings.PENDING_TIMEOUT_MINUTES)
    db: Session = SessionLocal()
    n = 0
    try:
        rows = (
            db.query(ToolInvocation)
            .filter(
                ToolInvocation.status.in_(["pending", "running"]),
                ToolInvocation.created_at < cutoff,
            )
            .limit(50)
            .all()
        )
        for inv in rows:
            was_running = inv.status == "running"
            inv.status = "failed"
            inv.err = (
                f"timeout: running > {settings.PENDING_TIMEOUT_MINUTES}min（疑似服务重启中断）"
                if was_running
                else f"timeout: pending > {settings.PENDING_TIMEOUT_MINUTES}min"
            )
            inv.finished_at = _utc()
            inv.amount_charged = 0  # 已退次，本次不计费
            db.commit()
            if inv.quota_charged:
                try:
                    refund(db, inv.user_id, inv.quota_charged, inv.id)
                except Exception:  # noqa: BLE001
                    db.rollback()
            n += 1
        return n
    finally:
        db.close()


def archive_stale_records() -> int:
    """超过 RECORD_ARCHIVE_DAYS 天不活跃的记录自动归档。返回处理条数。"""
    cutoff = _utc() - timedelta(days=settings.RECORD_ARCHIVE_DAYS)
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(ToolInvocation)
            .filter(ToolInvocation.archived == False, ToolInvocation.created_at < cutoff)  # noqa: E712
            .limit(500)
            .all()
        )
        for inv in rows:
            inv.archived = True
            inv.archived_at = _utc()
        if rows:
            db.commit()
        return len(rows)
    finally:
        db.close()
