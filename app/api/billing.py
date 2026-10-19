from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_openid
from app.services.auth import account_id_for
from app.core.timeutil import local_iso
from app.models import User
from app.services.billing import (
    checkin,
    checked_in_today,
    create_recharge,
    get_account,
    join_restock_waitlist,
    list_packages,
    list_user_orders,
    quota_breakdown,
    quota_stats_map,
    user_id_by_openid,
    waitlist_status,
)
from app.services.campaign import checkin_config, user_channel
from app.services.ratelimit import allow

router = APIRouter()


@router.get("/packages")
def packages(channel: str = "wechat", platform: str = "android", db: Session = Depends(get_db)):
    return list_packages(db, channel=channel, platform=platform)


@router.get("/config")
def billing_config(db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    """前端支付相关开关：payment_mock=true 走模拟支付（联调）；支付通道/主体按请求所属渠道账号。"""
    from app.core.config import settings
    from app.models import ChannelAccount

    acc = None
    aid = account_id_for(db, openid)
    if aid:
        acc = db.get(ChannelAccount, aid)
    if acc is None:
        acc = db.query(ChannelAccount).filter(ChannelAccount.id == "default").first()
    return {
        "payment_mock": bool(acc.payment_mock) if acc else True,
        "payment_provider": (acc.payment_provider if acc else None) or "mock",
        "entity": (acc.entity_type if acc else None) or "personal",
    }


@router.post("/recharge/create")
def recharge_create(
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    try:
        order = create_recharge(
            db, user_id, body.get("package_id"),
            body.get("platform", "android"),
            channel=body.get("channel", "wechat"),
            account_id=account_id_for(db, openid),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    from app.services.payment import create_prepay

    return {
        "order_id": order.id,
        "amount": float(order.amount),
        "prepay_params": create_prepay(order),  # TODO 虚拟支付接入后签名需 db 会话
    }


@router.post("/restock/join")
def restock_join(body: dict = Body(default={}), db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    """停售预约（§16.2-B）：仅停售期间可约，幂等。"""
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    if not allow(f"restock:{openid}", limit=5):
        raise HTTPException(429, "too many requests")
    try:
        return join_restock_waitlist(db, user_id, channel=(body.get("channel") or "wechat").lower())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/restock/status")
def restock_status(db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    return waitlist_status(db, user_id)


@router.get("/orders")
def orders(
    limit: int = 50,
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """当前用户的充值记录（订单），按时间倒序。"""
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    return list_user_orders(db, user_id, limit=min(max(limit, 1), 100))


@router.get("/transactions")
def transactions(
    limit: int = 50,
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """当前用户的积分流水（充值/消耗/退款/签到/邀请奖励等），按时间倒序。

    消耗笔（type=consume）带工具名：ref_id 存 tool_invocation.id，批量回查关联工具。
    """
    from app.models import Tool, ToolInvocation, Transaction

    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    rows = (
        db.query(Transaction)
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.id.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    inv_ids = [int(t.ref_id) for t in rows if t.type == "consume" and t.ref_id and str(t.ref_id).isdigit()]
    tool_names = {}
    if inv_ids:
        pairs = (
            db.query(ToolInvocation.id, Tool.name)
            .join(Tool, Tool.id == ToolInvocation.tool_id)
            .filter(ToolInvocation.id.in_(inv_ids))
            .all()
        )
        tool_names = {i: n for i, n in pairs}
    return [
        {
            "id": t.id,
            "type": t.type,
            "quota_delta": int(t.quota_delta or 0),
            "tool_name": tool_names.get(int(t.ref_id)) if (t.type == "consume" and t.ref_id and str(t.ref_id).isdigit()) else None,
            "created_at": local_iso(t.created_at),
        }
        for t in rows
    ]


@router.get("/account/me")
def me(db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    acct = get_account(db, user_id)
    bd = quota_breakdown(db, user_id)
    # 展示口径统一走台账：可用 + 已用 + 已过期作废 = 合计（避免「已用 > 共」的自相矛盾）
    stats = quota_stats_map(db, [user_id]).get(user_id, {})
    u = db.get(User, user_id)
    channel = user_channel(db, user_id)
    ck = checkin_config(db, channel)
    return {
        "balance": float(acct.balance),
        "quota_total": int(stats.get("total", 0)),
        "quota_used": int(stats.get("used", 0)),
        "quota_expired": int(stats.get("expired", 0)),
        # 可用次数：总数 + 付费/免费（赠送）拆分
        "quota_remain": bd["total"],
        "paid_remain": bd["paid"],
        "free_remain": bd["free"],
        "free_expire_at": local_iso(bd["free_expire_at"]),
        # 每日签到（未配置签到活动 → checkin_enabled=False，前端隐藏/置灰）
        "checked_in_today": checked_in_today(db, user_id),
        "checkin_enabled": bool(ck),
        "checkin_reward": int(ck["quota"]) if ck else 0,
        "checkin_valid_days": int(ck["valid_days"]) if ck else 0,
        # 个人资料（头像为后端代理路径，前端拼 baseUrl）
        "nickname": (u.nickname if u else "") or "",
        "avatar": (f"/api/auth/avatar/{user_id}" if (u and u.avatar) else ""),
        "phone": (u.phone[:3] + "****" + u.phone[7:]) if (u and u.phone and len(u.phone) >= 11) else "",
    }


@router.post("/checkin")
def do_checkin(db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    """每日签到（一天一次，幂等）；未配置签到活动则不可用。"""
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    try:
        return checkin(db, user_id)
    except ValueError as e:
        if str(e) == "checkin_disabled":
            raise HTTPException(400, "签到活动未开启")
        raise
