import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, extract, or_, case
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import local_iso, to_local, utcnow
from app.models import (
    Account,
    ChannelAccount,
    Order,
    QuotaGrant,
    RechargePackage,
    RestockWaitlist,
    Transaction,
    User,
    AlertLog,
    UserIdentity,
)
from app.services.campaign import quote as campaign_quote

DEFAULT_CHANNEL = "wechat"

# 收款阈值默认值（渠道账号未配置时使用；§16.2 个人主体月限）
DEFAULT_MONTH_LIMIT = 100000
DEFAULT_WARN_THRESHOLD = 80000
DEFAULT_STOP_THRESHOLD = 90000


def _limits_for(db: Session, account_id: str | None = None) -> tuple[int, int, int]:
    """该渠道账号的本月收款阈值 (月限, 预警, 停售)。

    账号未配置（NULL）时回落到代码常量默认；不指定账号时取「default」账号的配置。
    （月限阈值已全部后台化：渠道账号级覆盖 > 代码常量，env 不再参与。）
    """
    acc = None
    if account_id:
        acc = db.get(ChannelAccount, account_id)
    else:
        acc = db.query(ChannelAccount).filter(ChannelAccount.id == "default").first()
    return (
        (acc.month_limit if acc else None) or DEFAULT_MONTH_LIMIT,
        (acc.warn_threshold if acc else None) or DEFAULT_WARN_THRESHOLD,
        (acc.stop_threshold if acc else None) or DEFAULT_STOP_THRESHOLD,
    )


def effective_price_quota(tool, channel: str = DEFAULT_CHANNEL) -> int:
    """工具按渠道生效定价：channel_pricing 覆盖，未配置用 price_quota（§9.5）。"""
    cp = getattr(tool, "channel_pricing", None) or {}
    try:
        return int(cp.get(channel, tool.price_quota or 1))
    except (TypeError, ValueError):
        return tool.price_quota or 1


def package_allows_channel(pkg, channel: str) -> bool:
    chs = pkg.channels or [DEFAULT_CHANNEL]
    return channel in chs


def effective_package_price(pkg, channel: str = DEFAULT_CHANNEL, platform: str = "android") -> float:
    """档位按渠道生效价格：channel_prices 覆盖，未配置回落 price/price_ios（§9.5）。"""
    prices = pkg.channel_prices or {}
    if channel in prices:
        return float(prices[channel])
    return float(pkg.price_ios) if platform == "ios" else float(pkg.price)


def get_account(db: Session, user_id: int) -> Account:
    acct = db.get(Account, user_id)
    if not acct:
        acct = Account(user_id=user_id)
        db.add(acct)
        db.commit()
        db.refresh(acct)
    return acct


def _live_grants(db: Session, user_id: int):
    """未过期且有剩余的配额台账，**最快到期的先扣**（永久额度排最后）。"""
    return (
        db.query(QuotaGrant)
        .filter(
            QuotaGrant.user_id == user_id,
            QuotaGrant.remaining > 0,
            or_(QuotaGrant.expires_at.is_(None), QuotaGrant.expires_at > utcnow()),
        )
        .order_by(
            case((QuotaGrant.expires_at.is_(None), 1), else_=0),
            QuotaGrant.expires_at.asc(),
            QuotaGrant.id.asc(),
        )
    )


def quota_remain(db: Session, user_id: int) -> int:
    """可用次数 = 所有未过期台账的剩余之和（次数余额的唯一真源）。

    展示统计口径统一走 `quota_stats_map()`（台账），
    `account.quota_total` 已不再用于展示（它是账本建立前的旧计数器，会漏算活动赠送）。
    """
    return quota_breakdown(db, user_id)["total"]


# 「免费（赠送）」来源：邀请奖励、签到奖励等；其余（充值/退款/迁移）算「付费」
FREE_SOURCES = ("invite", "checkin")

# 流水类型 / 备注的中文标签（后台展示的单一真源，前端不再自建映射）
TX_TYPE_LABELS = {
    "recharge": "充值",
    "recharge_bonus": "活动赠送",
    "consume": "消耗",
    "refund": "退款",
    "checkin_reward": "签到奖励",
    "invite_reward": "邀请奖励",
    "adjust": "人工调整",
}
TX_REMARK_LABELS = {
    "recharge": "充值到账",
    "campaign_bonus": "充值优惠赠送",
    "consume": "工具消耗",
    "refund": "失败退款",
    "checkin": "每日签到",
    "invite_bind": "邀请成功奖励",
    "invite_recharge": "好友首充加奖",
    "invite_reward": "邀请奖励",
}


def tx_labels(t) -> tuple:
    """(类型中文, 备注中文)；未知值原样返回（备注可能是人工填的自由文本）。"""
    return (
        TX_TYPE_LABELS.get(t.type or "", t.type or ""),
        TX_REMARK_LABELS.get(t.remark or "", t.remark or ""),
    )


def quota_breakdown(db: Session, user_id: int) -> dict:
    """可用次数拆分：付费 / 免费（赠送），并给出免费次数最近到期时间。"""
    rows = (
        db.query(QuotaGrant)
        .filter(
            QuotaGrant.user_id == user_id,
            QuotaGrant.remaining > 0,
            or_(QuotaGrant.expires_at.is_(None), QuotaGrant.expires_at > utcnow()),
        )
        .all()
    )
    paid = sum(int(g.remaining) for g in rows if g.source not in FREE_SOURCES)
    free = sum(int(g.remaining) for g in rows if g.source in FREE_SOURCES)
    expiries = [g.expires_at for g in rows if g.source in FREE_SOURCES and g.expires_at]
    return {
        "total": paid + free,
        "paid": paid,
        "free": free,
        "free_expire_at": min(expiries) if expiries else None,
    }


def grants_remaining_map(db: Session, user_ids: list) -> dict:
    """批量取剩余次数（列表页用，避免 N+1）。"""
    if not user_ids:
        return {}
    rows = (
        db.query(QuotaGrant.user_id, func.coalesce(func.sum(QuotaGrant.remaining), 0))
        .filter(
            QuotaGrant.user_id.in_(user_ids),
            QuotaGrant.remaining > 0,
            or_(QuotaGrant.expires_at.is_(None), QuotaGrant.expires_at > utcnow()),
        )
        .group_by(QuotaGrant.user_id)
        .all()
    )
    return {int(uid): int(s or 0) for uid, s in rows}


def quota_stats_map(db: Session, user_ids: list) -> dict:
    """批量统计次数（统一走台账口径，保证「可用 + 已用 + 已过期 = 合计」恒成立）。

    - available：未过期台账剩余（与 quota_remain 同源）
    - gained   ：台账累计发放（充值 + 活动赠送 + 退款回补 + 历史迁移）
    - used     ：累计消耗（account.quota_used，退款会回退）
    - expired  ：已过期作废 = max(0, gained - available - used)
    - total    ：可解释次数合计 = available + used + expired

    注意：`account.quota_total/quota_used` 是账本建立前的旧计数器，既不含活动赠送、
    也含账前历史消耗，**不能直接当展示口径**（会出现「已用 > 共」这种自相矛盾）。
    """
    if not user_ids:
        return {}
    available = grants_remaining_map(db, user_ids)
    gained = {
        int(uid): int(s or 0)
        for uid, s in db.query(QuotaGrant.user_id, func.coalesce(func.sum(QuotaGrant.quota), 0))
        .filter(QuotaGrant.user_id.in_(user_ids))
        .group_by(QuotaGrant.user_id)
        .all()
    }
    used = {
        int(uid): int(s or 0)
        for uid, s in db.query(Account.user_id, func.coalesce(Account.quota_used, 0))
        .filter(Account.user_id.in_(user_ids))
        .all()
    }
    out = {}
    for raw in user_ids:
        uid = int(raw)
        av, us, gn = available.get(uid, 0), used.get(uid, 0), gained.get(uid, 0)
        ex = max(0, gn - av - us)  # 账前数据可能对不上，只保留非负的作废量
        out[uid] = {
            "available": av, "gained": gn, "used": us, "expired": ex,
            "total": av + us + ex,
        }
    return out


def grant_quota(db: Session, user_id: int, n: int, source: str, ref_id: str = "", expires_at=None) -> QuotaGrant:
    """发放一笔配额；`expires_at=None` 表示永久。"""
    g = QuotaGrant(
        user_id=user_id, source=source, quota=int(n), remaining=int(n),
        ref_id=ref_id, expires_at=expires_at,
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


def _checkin_ref(user_id: int) -> str:
    """签到标识：每个用户每个自然日一条（本地日期）。"""
    return "checkin:" + to_local(utcnow()).strftime("%Y-%m-%d")


def checked_in_today(db: Session, user_id: int) -> bool:
    ref = _checkin_ref(user_id)
    return (
        db.query(QuotaGrant.id)
        .filter(
            QuotaGrant.user_id == user_id,
            QuotaGrant.source == "checkin",
            QuotaGrant.ref_id == ref,
        )
        .first()
        is not None
    )


def checkin(db: Session, user_id: int) -> dict:
    """每日签到：按当前渠道的「签到奖励」活动发放次数（幂等，一天一次）。

    返回 {already, reward, total_remain}。
    """
    from app.services.campaign import checkin_config, user_channel

    if checked_in_today(db, user_id):
        return {"already": True, "reward": 0, "quota_remain": quota_remain(db, user_id)}

    cfg = checkin_config(db, user_channel(db, user_id))
    if not cfg:
        raise ValueError("checkin_disabled")
    n = int(cfg["quota"])
    days = int(cfg["valid_days"])
    ref = _checkin_ref(user_id)
    expires_at = utcnow() + timedelta(days=days) if days > 0 else None
    grant_quota(db, user_id, n, "checkin", ref, expires_at=expires_at)
    db.add(
        Transaction(
            user_id=user_id, type="checkin_reward", amount=0,
            quota_delta=n, ref_id=ref, remark="checkin",
        )
    )
    db.commit()
    return {"already": False, "reward": n, "quota_remain": quota_remain(db, user_id)}


def expire_grants(db: Session) -> int:
    """把已过期台账的剩余清零（由 worker reaper 定时调用）。返回清理条数。"""
    n = (
        db.query(QuotaGrant)
        .filter(
            QuotaGrant.remaining > 0,
            QuotaGrant.expires_at.isnot(None),
            QuotaGrant.expires_at <= utcnow(),
        )
        .update({QuotaGrant.remaining: 0}, synchronize_session=False)
    )
    db.commit()
    return int(n or 0)


def month_paid_total(db: Session, account_id: str | None = None) -> float:
    """本月已支付收款额；account_id 为空时统计全部渠道账号。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    q = (
        db.query(func.coalesce(func.sum(Order.amount), 0))
        .filter(
            Order.status == "paid",
            extract("month", Order.paid_at) == now.month,
            extract("year", Order.paid_at) == now.year,
        )
    )
    if account_id:
        q = q.filter(Order.channel_account_id == account_id)
    return float(q.scalar() or 0)


def list_packages(db: Session, channel: str = DEFAULT_CHANNEL, platform: str = "android") -> list[dict]:
    total = month_paid_total(db)
    _limit, warn_threshold, stop_threshold = _limits_for(db, None)
    sold_out = total >= stop_threshold
    pkgs = (
        db.query(RechargePackage)
        .filter(RechargePackage.enabled == True)
        .order_by(RechargePackage.sort)
        .all()
    )
    plat = "ios" if (platform or "").lower() == "ios" else "android"
    out = []
    for p in pkgs:
        if not package_allows_channel(p, channel):
            continue  # 渠道不可售（§9.5）
        # 按**请求终端**取价（iOS 与 Android 档位价不同）
        origin_pay = effective_package_price(p, channel, plat)
        origin_ios = effective_package_price(p, channel, "ios")
        q_pay = campaign_quote(db, channel, origin_pay)
        q_ios = campaign_quote(db, channel, origin_ios)
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "quota": p.quota,
                "platform": plat,
                "price": q_pay["amount"],
                "price_ios": q_ios["amount"],
                "origin_price": q_pay["origin_amount"],
                "origin_price_ios": q_ios["origin_amount"],
                "discount": q_pay["discount"],
                "campaign_name": q_pay["campaign_name"],
                # 充值优惠「送次数」方式：额外赠送次数（前端展示「送 N 次」）
                "campaign_mode": q_pay["mode"],
                "bonus_percent": int(q_pay.get("bonus_percent") or 0),
                "bonus_quota": (
                    int(p.quota or 0) * int(q_pay.get("bonus_percent") or 0) // 100
                    if q_pay.get("mode") == "bonus"
                    else 0
                ),
                "description": p.description,
                "sold_out": sold_out,
                "upgrade_suggested": total >= warn_threshold,
            }
        )
    return out


def list_user_orders(db: Session, user_id: int, limit: int = 50) -> list[dict]:
    """用户充值记录：按时间倒序返回订单，附带档位名称与次数（供小程序「充值记录」）。"""
    rows = (
        db.query(Order, RechargePackage)
        .outerjoin(RechargePackage, RechargePackage.id == Order.package_id)
        .filter(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for o, p in rows:
        out.append(
            {
                "order_id": o.id,
                "package_name": (p.name if p else "次数包"),
                "quota": (p.quota if p else None),
                "amount": float(o.amount or 0),
                "origin_amount": (
                    float(o.origin_amount) if o.origin_amount is not None else float(o.amount or 0)
                ),
                "discount": float(o.discount) if o.discount is not None else None,
                "campaign_name": o.campaign_name or "",
                # 充值平台（两端档位价不同，记录页据此标注 iOS / 安卓）
                "platform": o.platform or "",
                "status": o.status,  # pending / paid
                "created_at": local_iso(o.created_at),
                "paid_at": local_iso(o.paid_at),
            }
        )
    return out


def monthly_status(db: Session, account_id: str | None = None) -> dict:
    """本月收款监控：总量 / 月限 / 阈值 / 是否停售 / 是否建议升级主体。

    account_id 为空 = 全部渠道（收款额取全局，阈值取 default 账号配置）。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    total = month_paid_total(db, account_id)
    limit, warn_threshold, stop_threshold = _limits_for(db, account_id)
    return {
        "year": now.year,
        "month": now.month,
        "account_id": account_id or "",
        "paid_total": total,
        "month_limit": limit,
        "warn_threshold": warn_threshold,
        "stop_threshold": stop_threshold,
        "ratio": round(total / limit, 4) if limit else 0,
        "warn": total >= warn_threshold,
        "sold_out": total >= stop_threshold,
        "upgrade_suggested": total >= warn_threshold,
    }


def _maybe_alert(db: Session, kind: str, month_key: str, message: str, payload: dict, dedup_key: str = "") -> None:
    """同类型当月告警只记录一次（可按 dedup_key 细分，如按工具），避免重复刷屏。"""
    like = f"%{month_key}%{dedup_key}%" if dedup_key else f"%{month_key}%"
    exists = (
        db.query(AlertLog)
        .filter(AlertLog.kind == kind, AlertLog.message.like(like))
        .first()
    )
    if exists:
        return
    db.add(AlertLog(kind=kind, message=message, payload=payload))
    db.commit()


def _check_month_limit_alert(db: Session, total: float, account_id: str | None = None) -> None:
    """支付到账后评估月限，必要时告警（临近月限 -> 建议升级企业主体；逼近停售 -> 停售）。

    阈值取该订单所属渠道账号的配置。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    month_key = f"{now.year}-{now.month:02d}"
    _limit, warn_threshold, stop_threshold = _limits_for(db, account_id)
    scope = f"[{month_key}][{account_id or 'all'}]"
    if total >= stop_threshold:
        _maybe_alert(
            db, "month_limit_stop", month_key,
            f"{scope} 本月收款达 {total:.0f}，达到停售阈值 {stop_threshold}，已触发停售",
            {"total": total, "account_id": account_id or "", "stop_threshold": stop_threshold},
            dedup_key=account_id or "all",
        )
    elif total >= warn_threshold:
        _maybe_alert(
            db, "month_limit_warn", month_key,
            f"{scope} 本月收款达 {total:.0f}，达到预警阈值 {warn_threshold}，建议升级企业主体以解除收款上限",
            {"total": total, "account_id": account_id or "", "warn_threshold": warn_threshold},
            dedup_key=account_id or "all",
        )


def create_recharge(
    db: Session, user_id: int, package_id: int, platform: str = "android",
    channel: str = DEFAULT_CHANNEL, account_id: str | None = None,
) -> Order:
    acc = db.get(ChannelAccount, account_id) if account_id else None
    _limit, _warn, stop_threshold = _limits_for(db, account_id)
    if month_paid_total(db, account_id) >= stop_threshold:
        raise ValueError("sold_out")
    pkg = db.get(RechargePackage, package_id)
    if not pkg or not pkg.enabled:
        raise ValueError("package_not_found")
    if not package_allows_channel(pkg, channel):
        raise ValueError("channel_not_allowed")
    origin_amount = effective_package_price(pkg, channel, platform)
    q = campaign_quote(db, channel, origin_amount)  # 渠道活动：打折 / 送次数（§营销）
    amount = q["amount"]
    # 「送次数」方式：按档位次数 × 比例 额外赠送，支付到账后发放（此处快照，避免活动变更影响已下单）
    bonus = (
        int(pkg.quota or 0) * int(q.get("bonus_percent") or 0) // 100
        if q.get("mode") == "bonus"
        else 0
    )
    order_id = "R" + datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6]
    order = Order(
        id=order_id,
        user_id=user_id,
        package_id=package_id,
        amount=amount,
        platform=platform,
        channel="apple" if platform == "ios" else "wechat",
        client_channel=channel,
        channel_account_id=account_id,
        # 支付通道与主体按渠道账号配置（后台「渠道账号」）；未配置回落 mock/personal（安全侧）
        payment_provider=(acc.payment_provider if acc else None) or "mock",
        entity=(acc.entity_type if acc else None) or "personal",
        status="pending",
        origin_amount=q["origin_amount"],
        discount=q["discount"],
        bonus_quota=bonus,
        campaign_name=q["campaign_name"],
        prepay_params={"mock": True, "order_id": order_id, "amount": amount},
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def notify_paid(db: Session, order_id: str) -> Order:
    """支付回调（Mock/真实验签后）标记已支付并加次数。"""
    order = db.get(Order, order_id)
    if not order or order.status == "paid":
        return order
    pkg = db.get(RechargePackage, order.package_id)
    order.status = "paid"
    order.paid_at = datetime.now(timezone.utc).replace(tzinfo=None)
    acct = get_account(db, order.user_id)
    bonus = int(order.bonus_quota or 0)   # 「送次数」活动：下单时已快照，到账后发放
    acct.quota_total += pkg.quota + bonus
    acct.balance += order.amount
    # 余额真源是台账：充值发放一笔永久配额
    db.add(
        QuotaGrant(
            user_id=order.user_id, source="recharge", quota=pkg.quota,
            remaining=pkg.quota, ref_id=order.id, expires_at=None,
        )
    )
    db.add(
        Transaction(
            user_id=order.user_id,
            type="recharge",
            amount=order.amount,
            quota_delta=pkg.quota,
            ref_id=order.id,
            remark="recharge",
            channel_account_id=order.channel_account_id,
        )
    )
    if bonus > 0:
        # 活动赠送次数：单独一笔台账 + 流水，便于对账（无金额、永久有效）
        db.add(
            QuotaGrant(
                user_id=order.user_id, source="recharge_bonus", quota=bonus,
                remaining=bonus, ref_id=order.id, expires_at=None,
            )
        )
        db.add(
            Transaction(
                user_id=order.user_id,
                type="recharge_bonus",
                amount=0,
                quota_delta=bonus,
                ref_id=order.id,
                remark="campaign_bonus",
                channel_account_id=order.channel_account_id,
            )
        )
    db.commit()
    # 月限监控：到账后评估是否触发预警/停售告警（阈值取该渠道账号配置）
    _check_month_limit_alert(db, month_paid_total(db, order.channel_account_id), order.channel_account_id)
    # 邀请有奖：被邀请人首次充值成功 → 给邀请人发奖（失败不影响支付结果）
    try:
        from app.services.invite import reward_on_first_paid

        reward_on_first_paid(db, order)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[invite] reward failed: {e}")
    return order


def global_unit_price(db: Session) -> float:
    """全站单次均价（元/次）：已支付订单金额合计 ÷ 对应档位次数合计。"""
    row = (
        db.query(func.sum(Order.amount), func.sum(RechargePackage.quota))
        .join(RechargePackage, RechargePackage.id == Order.package_id)
        .filter(Order.status == "paid")
        .first()
    )
    if row and row[0] and row[1]:
        return float(row[0]) / float(row[1])
    return 0.0


def user_unit_price(db: Session, user_id: int) -> float:
    """用户单次均价（元/次）：该用户累计消费 ÷ 累计获得次数；无充值记录则回落全站均价。

    用于扣费时落「单价快照」，保证历史金额不受日后调价/活动影响。
    """
    row = (
        db.query(func.sum(Order.amount), func.sum(RechargePackage.quota))
        .join(RechargePackage, RechargePackage.id == Order.package_id)
        .filter(Order.user_id == user_id, Order.status == "paid")
        .first()
    )
    if row and row[0] and row[1]:
        return float(row[0]) / float(row[1])
    return global_unit_price(db)


def consume(db: Session, user_id: int, price_quota: int, invocation_id, account_id: str | None = None) -> None:
    # 注意：db 由 FastAPI 依赖注入，已处于 autobegin 事务中，勿再 db.begin()
    acct = db.execute(
        select(Account).where(Account.user_id == user_id).with_for_update()
    ).scalar_one()
    need = int(price_quota)
    # 先扣最快到期的奖励配额（对用户有利），不足则由永久额度兜底
    grants = _live_grants(db, user_id).all()
    if sum(int(g.remaining) for g in grants) < need:
        raise ValueError("insufficient_quota")
    for g in grants:
        if need <= 0:
            break
        take = min(int(g.remaining), need)
        g.remaining = int(g.remaining) - take
        need -= take
    acct.quota_used += int(price_quota)  # 累计消耗（统计口径）
    db.add(
        Transaction(
            user_id=user_id,
            type="consume",
            amount=0,
            quota_delta=-int(price_quota),
            ref_id=str(invocation_id) if invocation_id else None,
            remark="consume",
            channel_account_id=account_id,
        )
    )
    db.commit()


def refund(db: Session, user_id: int, price_quota: int, invocation_id, account_id: str | None = None) -> None:
    acct = db.execute(
        select(Account).where(Account.user_id == user_id).with_for_update()
    ).scalar_one()
    n = int(price_quota)
    acct.quota_used = max(0, (acct.quota_used or 0) - n)
    # 退次回补为「永久」配额（一笔台账），避免把已过期的奖励退还成可用次数
    db.add(
        QuotaGrant(
            user_id=user_id, source="refund", quota=n, remaining=n,
            ref_id=str(invocation_id) if invocation_id else None, expires_at=None,
        )
    )
    db.add(
        Transaction(
            user_id=user_id,
            type="refund",
            amount=0,
            quota_delta=n,
            ref_id=str(invocation_id) if invocation_id else None,
            remark="refund",
            channel_account_id=account_id,
        )
    )
    db.commit()


def user_id_by_openid(db: Session, openid: str) -> int:
    """token 主渠道标识 -> user_id：先查 legacy user.openid，再查渠道身份表（§9.5）。"""
    if not openid:
        return None
    u = db.query(User).filter(User.openid == openid).first()
    if u:
        return u.id
    ident = db.query(UserIdentity).filter(UserIdentity.channel_uid == openid).first()
    return ident.user_id if ident else None


# ---------- 停售预约 waitlist（§16.2-B） ----------
def join_restock_waitlist(db: Session, user_id: int, channel: str = DEFAULT_CHANNEL) -> dict:
    """仅停售期间可约；幂等（一人一条）。"""
    _limit, _warn, stop_threshold = _limits_for(db, None)
    if month_paid_total(db) < stop_threshold:
        raise ValueError("not_sold_out")
    row = db.query(RestockWaitlist).filter_by(user_id=user_id).first()
    if row:
        return {"joined": True, "already": True}
    db.add(RestockWaitlist(user_id=user_id, channel=channel))
    db.commit()
    return {"joined": True, "already": False}


def waitlist_status(db: Session, user_id: int) -> dict:
    _limit, _warn, stop_threshold = _limits_for(db, None)
    row = db.query(RestockWaitlist).filter_by(user_id=user_id).first()
    return {
        "sold_out": month_paid_total(db) >= stop_threshold,
        "joined": bool(row),
        "notified": bool(row and row.notified),
    }


def notify_waitlist_for_new_month(db: Session) -> int:
    """次月 1 号触达：把上月未通知的预约置 notified。

    真实用户触达 = 小程序订阅消息（接入点：此处遍历 rows 逐个发送），MVP 先置位+日志。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (
        db.query(RestockWaitlist)
        .filter(RestockWaitlist.notified == False, RestockWaitlist.created_at < start)
        .all()
    )
    for r in rows:
        r.notified = True
        r.notified_at = now
    if rows:
        db.commit()
    return len(rows)
