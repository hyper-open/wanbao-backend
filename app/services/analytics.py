"""经营分析（§16.4 成本闭环）：收入/成本/毛利 + 按工具/按供应商拆分。"""
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Tool, ToolInvocation, Transaction


def _month_start() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _scope_account(q, col, account_id: str | None):
    """按渠道账号收敛统计范围（§9.5 多渠道）；为空则统计全部渠道。"""
    return q.filter(col == account_id) if account_id else q


def biz_metrics(db: Session, account_id: str | None = None) -> dict:
    """本月经营指标；account_id 指定时只统计该渠道账号（收入按订单归属、调用按调用归属）。"""
    start = _month_start()

    recharge = db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.type == "recharge", Transaction.created_at >= start
    )
    paid_total = float(_scope_account(recharge, Transaction.channel_account_id, account_id).scalar() or 0)
    quota_sold = int(
        _scope_account(
            db.query(func.coalesce(func.sum(Transaction.quota_delta), 0)).filter(
                Transaction.type == "recharge", Transaction.created_at >= start
            ),
            Transaction.channel_account_id,
            account_id,
        ).scalar() or 0
    )
    avg_price = (paid_total / quota_sold) if quota_sold else 0.0

    total_calls = _scope_account(
        db.query(func.count(ToolInvocation.id)).filter(ToolInvocation.created_at >= start),
        ToolInvocation.channel_account_id,
        account_id,
    ).scalar()
    ok = _scope_account(
        db.query(ToolInvocation).filter(
            ToolInvocation.created_at >= start, ToolInvocation.status == "success"
        ),
        ToolInvocation.channel_account_id,
        account_id,
    )
    success_calls = ok.count()
    cost_total = float(
        _scope_account(
            db.query(func.coalesce(func.sum(ToolInvocation.cost_amount), 0)).filter(
                ToolInvocation.created_at >= start
            ),
            ToolInvocation.channel_account_id,
            account_id,
        ).scalar() or 0
    )
    quota_consumed = int(
        _scope_account(
            db.query(func.coalesce(func.sum(ToolInvocation.quota_charged), 0)).filter(
                ToolInvocation.created_at >= start, ToolInvocation.status == "success"
            ),
            ToolInvocation.channel_account_id,
            account_id,
        ).scalar() or 0
    )

    margin = paid_total - cost_total
    margin_pct = (margin / paid_total) if paid_total else None

    # 按工具：调用量 / 消耗次数 / 成本 / 估算收入（按本月单次均价折算）与毛利
    rows = _scope_account(
        db.query(
            ToolInvocation.tool_id,
            func.count(ToolInvocation.id),
            func.coalesce(func.sum(ToolInvocation.cost_amount), 0),
            func.coalesce(func.sum(ToolInvocation.quota_charged), 0),
        )
        .filter(ToolInvocation.created_at >= start, ToolInvocation.status == "success")
        .group_by(ToolInvocation.tool_id),
        ToolInvocation.channel_account_id,
        account_id,
    ).all()
    per_tool = []
    tnames = {t.id: t.name for t in db.query(Tool).all()}
    for tid, c, cost, quota in rows:
        cost = float(cost or 0)
        quota = int(quota or 0)
        revenue = avg_price * quota
        per_tool.append(
            {
                "tool_id": tid,
                "tool_name": tnames.get(tid, tid or ""),
                "calls": int(c),
                "quota_charged": quota,
                "cost": round(cost, 4),
                "revenue_est": round(revenue, 4),
                "margin_pct": round((revenue - cost) / revenue, 4) if revenue > 0 else None,
            }
        )
    per_tool.sort(key=lambda x: -x["calls"])

    # 按供应商：model_used 形如 "provider/model"
    per_provider: dict = {}
    mus = _scope_account(
        db.query(ToolInvocation.model_used).filter(
            ToolInvocation.created_at >= start,
            ToolInvocation.status == "success",
            ToolInvocation.model_used.isnot(None),
        ),
        ToolInvocation.channel_account_id,
        account_id,
    ).all()
    for (mu,) in mus:
        pid = (mu or "").split("/")[0]
        per_provider[pid] = per_provider.get(pid, 0) + 1

    return {
        "month": start.strftime("%Y-%m"),
        "account_id": account_id or "",
        "paid_total": round(paid_total, 2),
        "quota_sold": quota_sold,
        "avg_price_per_quota": round(avg_price, 4),
        "calls_total": int(total_calls or 0),
        "calls_success": int(success_calls),
        "quota_consumed": quota_consumed,
        "cost_total": round(cost_total, 4),
        "margin": round(margin, 2),
        "margin_pct": round(margin_pct, 4) if margin_pct is not None else None,
        "per_tool": per_tool,
        "per_provider": per_provider,
    }
