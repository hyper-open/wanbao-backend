"""渠道活动（营销）：充值优惠 / 邀请有奖 / 签到奖励。

统一模型：`channel_campaign` 带 `type`，按「渠道 + 类型」取当前生效的一条
（同渠道同类型可配多条，取最近开始的一条，且未结束）。

**没有配置对应活动 = 该活动不启用**（不再回落到代码默认值）；活动存在时，
其 `config` 里缺失的字段才用 DEFAULT_* 补齐。
"""
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models import ChannelCampaign

TYPE_DISCOUNT = "discount"        # 充值优惠（打折 / 送次数，二选一）
TYPE_INVITE = "invite"            # 邀请有奖
TYPE_CHECKIN = "checkin"          # 每日签到奖励

# 充值优惠的两种方式
MODE_DISCOUNT = "discount"        # 打折：实付 = 原价 × 折扣，次数不变
MODE_BONUS = "bonus"              # 送次数：实付不变，按比例额外送次数

TYPES = (TYPE_DISCOUNT, TYPE_INVITE, TYPE_CHECKIN)

# 无生效活动时的兜底默认
DEFAULT_INVITE = {
    "reward_quota": 1,          # ① 好友注册即送（次）
    "recharge_percent": 10,     # ② 好友首充加奖：按首充次数的百分比（向下取整）
    "first_recharge_days": 30,  # ② 首充期限：须在绑定后 N 天内完成首充（0=不限）
    "invite_daily_cap": 10,     # ① 邀请奖励每日上限（人数，非积分）
    "invite_monthly_cap": 100,  # ① 邀请奖励每月上限（人数，非积分）
    "recharge_daily_cap": 10,   # ② 首充加奖每日上限（笔数，非积分）
    "recharge_monthly_cap": 100,  # ② 首充加奖每月上限（笔数，非积分）
    "valid_days": 30,           # 奖励次数有效期（天）
}
DEFAULT_CHECKIN = {"quota": 1, "valid_days": 30}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _active(db: Session, type_: str, channel: str, at: Optional[datetime] = None) -> Optional[ChannelCampaign]:
    """该渠道该类型当前生效的活动（未启用/未开始/已结束都不算）。"""
    at = at or _now()
    rows = (
        db.query(ChannelCampaign)
        .filter(
            ChannelCampaign.type == type_,
            ChannelCampaign.channel == channel,
            ChannelCampaign.enabled == True,  # noqa: E712
            ChannelCampaign.start_at <= at,
        )
        .order_by(ChannelCampaign.start_at.desc(), ChannelCampaign.id.desc())
        .all()
    )
    for c in rows:
        if c.end_at is None or c.end_at > at:
            return c
    return None


def active_discount(db: Session, channel: str = "wechat", at: Optional[datetime] = None) -> Optional[ChannelCampaign]:
    return _active(db, TYPE_DISCOUNT, channel, at)


# 兼容旧调用名（充值优惠）
active_campaign = active_discount


def discounted_amount(origin: float, discount: Optional[float]) -> float:
    """按折扣算实付金额（四舍五入到分）。discount 为空或 >=1 视为不打折。"""
    if not discount or float(discount) >= 1:
        return round(float(origin), 2)
    d = Decimal(str(origin)) * Decimal(str(discount))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def quote(db: Session, channel: str, origin_amount: float, at: Optional[datetime] = None) -> dict:
    """给一个原价加上当前渠道**充值优惠**活动信息。

    充值优惠有两种方式（`config.mode`）：
    - `discount` 打折：`amount` = 原价 × 折扣，次数不变；
    - `bonus`    送次数：`amount` = 原价（不降价），按 `bonus_percent` 额外送次数。
    返回里的 `mode` / `bonus_percent` 供下单与前端展示使用（无活动时均为默认值）。
    """
    base = {
        "origin_amount": round(float(origin_amount), 2),
        "amount": round(float(origin_amount), 2),
        "discount": None,
        "campaign_name": None,
        "mode": MODE_DISCOUNT,
        "bonus_percent": 0,
    }
    c = active_discount(db, channel, at)
    if not c:
        return base

    raw = c.config if isinstance(c.config, dict) else {}
    mode = MODE_BONUS if str(raw.get("mode") or "").lower() == MODE_BONUS else MODE_DISCOUNT
    pct = 0
    try:
        pct = max(0, int(raw.get("bonus_percent", 0)))
    except (TypeError, ValueError):
        pct = 0

    if mode == MODE_BONUS:
        if pct <= 0:  # 送次数比例为 0 视为未配置有效优惠
            return base
        return {**base, "mode": MODE_BONUS, "bonus_percent": pct, "campaign_name": c.name}

    if c.discount is None or float(c.discount) >= 1:
        return base
    return {
        **base,
        "amount": discounted_amount(origin_amount, c.discount),
        "discount": float(c.discount),
        "campaign_name": c.name,
    }


def invite_config(db: Session, channel: str = "wechat") -> dict | None:
    """当前生效的**邀请有奖**活动参数；**没有配置该活动则返回 None**（不启用邀请奖励）。

    活动存在时，缺失字段用 DEFAULT_INVITE 补齐；并兼容早期配置字段名。
    """
    c = _active(db, TYPE_INVITE, channel)
    if not c or not isinstance(c.config, dict):
        return None
    raw = dict(c.config)
    # 兼容早期字段：daily_cap/monthly_cap（未分①②）与 recharge_divisor（除数）
    if "invite_daily_cap" not in raw and "daily_cap" in raw:
        raw["invite_daily_cap"] = raw.get("daily_cap")
        raw["recharge_daily_cap"] = raw.get("daily_cap")
    if "invite_monthly_cap" not in raw and "monthly_cap" in raw:
        raw["invite_monthly_cap"] = raw.get("monthly_cap")
        raw["recharge_monthly_cap"] = raw.get("monthly_cap")
    if "recharge_percent" not in raw and raw.get("recharge_divisor"):
        try:
            d = int(raw["recharge_divisor"])
            if d > 0:
                raw["recharge_percent"] = round(100 / d)
        except (TypeError, ValueError):
            pass

    cfg = dict(DEFAULT_INVITE)
    for k, dv in DEFAULT_INVITE.items():
        try:
            cfg[k] = int(raw.get(k, dv))
        except (TypeError, ValueError):
            pass
    return cfg


def checkin_config(db: Session, channel: str = "wechat") -> dict | None:
    """当前生效的**签到奖励**活动参数；**没有配置该活动则返回 None**（不启用签到）。"""
    c = _active(db, TYPE_CHECKIN, channel)
    if not c or not isinstance(c.config, dict):
        return None
    cfg = dict(DEFAULT_CHECKIN)
    for k, dv in DEFAULT_CHECKIN.items():
        try:
            cfg[k] = int(c.config.get(k, dv))
        except (TypeError, ValueError):
            pass
    return cfg


def user_channel(db: Session, user_id: int) -> str:
    """用户的主渠道（用于取该用户适用的活动）；找不到时回落 wechat。"""
    from app.models import UserIdentity

    row = (
        db.query(UserIdentity.channel)
        .filter(UserIdentity.user_id == user_id)
        .order_by(UserIdentity.id.asc())
        .first()
    )
    return (row[0] if row else None) or "wechat"
