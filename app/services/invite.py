"""邀请有奖（两段式，奖励都发给**邀请人**）。

规则（由后台「营销活动」里该渠道的**邀请有奖**活动配置）：
1. **好友注册即送**：新用户首次用邀请码绑定 → 立即送 `reward_quota` 次；
2. **好友首充加奖**：被邀请人在**绑定后 `first_recharge_days` 天内**首次充值成功
   → 再送 `floor(首充档位次数 × recharge_percent / 100)` 次（期限内未首充则不再加奖）。

- 奖励次数有效 `valid_days` 天（走配额台账，过期自动作废）；
- 每段奖励各自受邀请人的**每日 / 每月上限**约束（按奖励事件计数）；
- 被邀请人唯一（一个用户只能被邀请一次），且不能邀请自己。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.ids import gen_invite_code
from app.core.timeutil import local_day_start_utc, local_month_start_utc, utcnow
from app.models import Invite, Order, RechargePackage, Transaction, User
from app.services.campaign import invite_config, user_channel


def ensure_code(db: Session, user: User) -> str:
    """用户的邀请码（懒生成，唯一）。"""
    if user.invite_code:
        return user.invite_code
    for _ in range(10):
        code = gen_invite_code()
        if not db.query(User).filter(User.invite_code == code).first():
            user.invite_code = code
            db.commit()
            return code
    raise RuntimeError("invite code exhausted")


def _cap_reached(db: Session, inviter_id: int, stamp, daily_cap: int, monthly_cap: int) -> bool:
    """该邀请人本阶段奖励是否已达日/月上限（按奖励时间戳计数）。"""
    def count(since) -> int:
        return int(
            db.query(func.count(Invite.id))
            .filter(Invite.inviter_id == inviter_id, stamp.isnot(None), stamp >= since)
            .scalar()
            or 0
        )

    if daily_cap and count(local_day_start_utc()) >= daily_cap:
        return True
    if monthly_cap and count(local_month_start_utc()) >= monthly_cap:
        return True
    return False


def _grant(db: Session, user_id: int, n: int, ref: str, valid_days: int, remark: str) -> None:
    """给邀请人发 n 次（配额台账 + 流水）。"""
    # 本地 import：billing 反向调用本模块，避免循环依赖
    from app.services.billing import grant_quota

    grant_quota(db, user_id, n, "invite", ref, expires_at=utcnow() + timedelta(days=valid_days))
    db.add(
        Transaction(
            user_id=user_id, type="invite_reward", amount=0,
            quota_delta=n, ref_id=ref, remark=remark,
        )
    )


def bind(db: Session, invitee_id: int, code: str) -> dict:
    """绑定邀请关系（幂等）；绑定成功即给邀请人发第一段奖励 x 次。"""
    code = (code or "").strip().upper()
    if not code:
        return {"bound": False, "reason": "empty"}
    if db.query(Invite).filter(Invite.invitee_id == invitee_id).first():
        return {"bound": False, "reason": "already_invited"}
    inviter = db.query(User).filter(User.invite_code == code).first()
    if not inviter:
        return {"bound": False, "reason": "code_not_found"}
    if inviter.id == invitee_id:
        return {"bound": False, "reason": "self"}

    inv = Invite(inviter_id=inviter.id, invitee_id=invitee_id, code=code, status="pending")
    db.add(inv)
    db.commit()
    db.refresh(inv)

    # —— 第一段：邀请成功，立即发 x 次（未配置邀请活动则不发奖）——
    cfg = invite_config(db, user_channel(db, inviter.id))
    if not cfg:
        return {"bound": True, "reason": "ok", "reward_quota": 0, "disabled": True}
    x = int(cfg["reward_quota"])
    valid_days = int(cfg["valid_days"])
    if x > 0 and not _cap_reached(
        db, inviter.id, Invite.bind_rewarded_at,
        int(cfg["invite_daily_cap"]), int(cfg["invite_monthly_cap"]),
    ):
        _grant(db, inviter.id, x, str(inv.id), valid_days, remark="invite_bind")
        inv.bind_quota = x
        inv.bind_rewarded_at = utcnow()
        inv.reward_quota = int(inv.reward_quota or 0) + x
        db.commit()

    return {"bound": True, "reason": "ok", "reward_quota": int(inv.bind_quota or 0)}


def reward_on_first_paid(db: Session, order: Order) -> None:
    """被邀请人**首次充值成功**时给邀请人发第二段奖励（由 billing.notify_paid 调用）。

    - 非首充（该用户已有其他已支付订单）→ 该邀请标记 invalid；
    - 首充奖励次数 = floor(档位次数 / recharge_divisor)；
    - 邀请人当日/当月首充奖励超限 → 该邀请标记 capped。
    """
    inv = (
        db.query(Invite)
        .filter(Invite.invitee_id == order.user_id, Invite.status == "pending")
        .first()
    )
    if not inv:
        return

    paid_cnt = (
        db.query(func.count(Order.id))
        .filter(Order.user_id == order.user_id, Order.status == "paid")
        .scalar()
        or 0
    )
    if int(paid_cnt) != 1:
        inv.status = "invalid"  # 非首充不算有效邀请
        db.commit()
        return

    # 奖励参数来自「该渠道当前生效的邀请活动」；未配置邀请活动 → 不发奖
    cfg = invite_config(db, user_channel(db, inv.inviter_id))
    if not cfg:
        return

    pkg = db.get(RechargePackage, order.package_id)
    y = int(getattr(pkg, "quota", 0) or 0)          # 首充档位给的次数
    pct = max(0, int(cfg.get("recharge_percent") or 0))
    n = y * pct // 100
    window_days = int(cfg.get("first_recharge_days") or 0)
    valid_days = int(cfg["valid_days"])

    inv.order_id = order.id
    # 首充期限：须在绑定后 N 天内完成首充，超时不再加奖
    if window_days > 0 and inv.created_at and utcnow() > inv.created_at + timedelta(days=window_days):
        inv.status = "expired"
        db.commit()
        return

    if n > 0:
        if _cap_reached(
            db, inv.inviter_id, Invite.rewarded_at,
            int(cfg["recharge_daily_cap"]), int(cfg["recharge_monthly_cap"]),
        ):
            inv.status = "capped"
        else:
            _grant(db, inv.inviter_id, n, str(inv.id), valid_days, remark="invite_recharge")
            inv.recharge_quota = n
            inv.rewarded_at = utcnow()
            inv.reward_quota = int(inv.reward_quota or 0) + n
            inv.status = "rewarded"
    else:
        inv.status = "rewarded"  # 首充次数不足一次奖励，首充阶段直接结算
    db.commit()


def summary(db: Session, user_id: int, user: User) -> dict:
    """我的邀请概览：邀请码、累计有效邀请、累计获得次数、①②各自的今日/本月已用与上限。"""
    code = user.invite_code or ""
    total = db.query(func.count(Invite.id)).filter(Invite.inviter_id == user_id).scalar() or 0
    rewarded = (
        db.query(func.count(Invite.id))
        .filter(Invite.inviter_id == user_id, Invite.status == "rewarded")
        .scalar()
        or 0
    )
    # 累计获得次数：按**实际已发放**统计（含绑定段的即时奖励；
    # pending/invalid/capped/expired 里已发放的绑定奖励同样已入账，不收回）
    got_total = (
        db.query(func.coalesce(func.sum(Invite.reward_quota), 0))
        .filter(Invite.inviter_id == user_id)
        .scalar()
        or 0
    )

    def _count(stamp, since) -> int:
        return int(
            db.query(func.count(Invite.id))
            .filter(Invite.inviter_id == user_id, stamp.isnot(None), stamp >= since)
            .scalar()
            or 0
        )

    day, month = local_day_start_utc(), local_month_start_utc()
    base = {
        "invite_code": code,
        "invited_total": int(total),
        "rewarded_total": int(rewarded),
        "quota_got_total": int(got_total),
        # ① 邀请奖励的今日/本月已用
        "invite_got_today": _count(Invite.bind_rewarded_at, day),
        "invite_got_month": _count(Invite.bind_rewarded_at, month),
        # ② 首充加奖的今日/本月已用
        "recharge_got_today": _count(Invite.rewarded_at, day),
        "recharge_got_month": _count(Invite.rewarded_at, month),
    }

    cfg = invite_config(db, user_channel(db, user_id))
    if not cfg:  # 未配置邀请有奖活动 → 不启用（前端据此提示"活动未开启"）
        base["enabled"] = False
        return base

    base.update(
        {
            "enabled": True,
            "reward_quota": int(cfg["reward_quota"]),
            "recharge_percent": int(cfg["recharge_percent"]),
            "first_recharge_days": int(cfg["first_recharge_days"]),
            "valid_days": int(cfg["valid_days"]),
            "invite_daily_cap": int(cfg["invite_daily_cap"]),
            "invite_monthly_cap": int(cfg["invite_monthly_cap"]),
            "recharge_daily_cap": int(cfg["recharge_daily_cap"]),
            "recharge_monthly_cap": int(cfg["recharge_monthly_cap"]),
        }
    )
    return base


def records(db: Session, user_id: int, page: int = 1, size: int = 15) -> dict:
    """邀请记录（分页）：只暴露脱敏昵称与状态，不泄露被邀请人身份。

    `pending` 且已超过首充期限的，对外呈现为 `expired`（无需等定时任务改库）。
    """
    size = min(max(int(size), 1), 50)
    page = max(int(page), 1)
    q = db.query(Invite).filter(Invite.inviter_id == user_id)
    total = q.count()
    rows = q.order_by(Invite.id.desc()).offset((page - 1) * size).limit(size).all()
    cfg = invite_config(db, user_channel(db, user_id))
    window_days = int(cfg["first_recharge_days"]) if cfg else 0
    now = utcnow()
    items = []
    for r in rows:
        status = r.status
        if status == "pending" and window_days > 0 and r.created_at and now > r.created_at + timedelta(days=window_days):
            status = "expired"
        items.append(
            {
                "id": r.id,
                "nickname": _mask(db, r.invitee_id),
                # pending(待首充) / rewarded(已完成) / invalid(非首充) / capped(超限) / expired(超时未首充)
                "status": status,
                "reward_quota": int(r.reward_quota or 0),
                "bind_quota": int(r.bind_quota or 0),
                "recharge_quota": int(r.recharge_quota or 0),
                "created_at": r.created_at,
                "rewarded_at": r.rewarded_at,
            }
        )
    return {"items": items, "total": int(total), "page": page, "size": size, "has_more": page * size < int(total)}


def _mask(db: Session, uid: int) -> str:
    u = db.get(User, uid)
    if not u:
        return "用户"
    if u.nickname:
        return u.nickname[:1] + "**"
    if u.phone and len(u.phone) >= 11:
        return u.phone[:3] + "****" + u.phone[7:]
    return f"用户{uid}"
