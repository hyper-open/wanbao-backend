from datetime import timedelta
from typing import Optional

import json

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.timeutil import local_iso, local_str_to_utc, utcnow
from app.models import (
    AdminUser,
    AuditLog,
    AlertLog,
    ChannelCampaign,
    Feedback,
    LLMModel,
    LLMProvider,
    LLMRouting,
    Order,
    RechargePackage,
    RestockWaitlist,
    SessionTurn,
    Tool,
    ToolCategory,
    ToolInvocation,
    ToolSession,
    User,
    CATEGORY_NAMES,
)
from app.services import admin_auth
from app.services import attachment as attachment_svc
from app.services.billing import (
    grants_remaining_map,
    monthly_status,
    quota_remain,
    quota_stats_map,
    tx_labels,
)
from app.services.campaign import TYPES as CAMPAIGN_TYPES
from app.services.analytics import biz_metrics
from app.services.agent.graphs import EXECUTOR_LABELS, EXECUTOR_REGISTRY
from app.services.auth import CHANNEL_LABELS, KNOWN_CHANNELS

router = APIRouter()


def _range_dt(val, *, is_to: bool = False):
    """筛选用日期/时间参数：按本地时区解析为 UTC 比较值；只填日期时，结束日含当天。"""
    if not val:
        return None
    try:
        dt = local_str_to_utc(val)
    except ValueError:
        raise HTTPException(400, f"时间格式不正确：{val}")
    if is_to and len(str(val).strip()) == 10:
        dt = dt + timedelta(days=1)
    return dt


def _apply_sort(query, cols: dict, sort: str, direction: str, *, default, tiebreak=None):
    """列表表头排序：白名单列 -> 真实列（防注入）；无匹配回落到 default 次序。

    tiebreak（通常传主键 Column）用于同值行的稳定分页，避免翻页重复/漏行。
    """
    col = cols.get(sort or "")
    if col is None:
        return query.order_by(*default)
    primary = col.asc() if (direction or "").lower() == "asc" else col.desc()
    if tiebreak is not None and tiebreak is not col:
        return query.order_by(primary, tiebreak.desc())
    return query.order_by(primary)


def require_admin(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    """管理员鉴权：仅接受账号登录后签发的 Bearer 令牌（旧共享令牌已移除）。

    兼容 `<img src>` 等无法带 Header 的场景：允许 `?token=` 传令牌（内部后台使用）。
    """
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        token = request.query_params.get("token") or ""
    if not token:
        raise HTTPException(401, "未登录")
    payload = admin_auth.parse_admin_token(token)
    if not payload:
        raise HTTPException(401, "登录已过期，请重新登录")
    return payload


@router.get("/user-avatar/{uid}")
def admin_user_avatar(uid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    """用户头像：后端流式返回图片字节（对象存储对外不可达，不走预签名直链）。"""
    u = db.get(User, uid)
    if not u or not u.avatar:
        raise HTTPException(404, "无头像")
    data = attachment_svc.get_object_bytes(u.avatar)
    mime = "image/jpeg"
    if u.avatar.endswith(".png"):
        mime = "image/png"
    elif u.avatar.endswith(".webp"):
        mime = "image/webp"
    elif u.avatar.endswith(".gif"):
        mime = "image/gif"
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


# ---------- 管理员账号 ----------
@router.post("/login")
def admin_login(body: dict = Body(default={}), db: Session = Depends(get_db)):
    try:
        return admin_auth.login(db, (body.get("username") or "").strip(), body.get("password") or "")
    except ValueError as e:
        raise HTTPException(401, str(e))


@router.get("/me")
def admin_me(me: dict = Depends(require_admin)):
    return {"username": me.get("username"), "role": me.get("role", "admin")}


@router.post("/password")
def admin_change_password(
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    me: dict = Depends(require_admin),
):
    """修改自己的密码。"""
    admin = db.query(AdminUser).filter(AdminUser.username == me.get("username")).first()
    if not admin:
        raise HTTPException(400, "当前为令牌登录，无账号可改密")
    if not admin_auth.verify_password(body.get("old_password") or "", admin.password_hash or ""):
        raise HTTPException(400, "原密码错误")
    new_pw = body.get("new_password") or ""
    if len(new_pw) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    admin.password_hash = admin_auth.hash_password(new_pw)
    db.commit()
    return {"ok": True}


@router.get("/admins")
def admin_list_admins(db: Session = Depends(get_db), _=Depends(require_admin)):
    rows = db.query(AdminUser).order_by(AdminUser.id).all()
    out = []
    for a in rows:
        d = a.to_dict()
        d.pop("password_hash", None)
        out.append(d)
    return out


@router.post("/admins")
def admin_create_admin(body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(400, "用户名必填，密码至少 8 位")
    if db.query(AdminUser).filter(AdminUser.username == username).first():
        raise HTTPException(400, "用户名已存在")
    a = AdminUser(
        username=username,
        password_hash=admin_auth.hash_password(password),
        name=body.get("name") or "",
        role=body.get("role") or "admin",
        status="active",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    d = a.to_dict()
    d.pop("password_hash", None)
    return d


@router.put("/admins/{aid}")
def admin_update_admin(aid: int, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    a = db.get(AdminUser, aid)
    if not a:
        raise HTTPException(404, "not found")
    if body.get("password"):
        if len(body["password"]) < 8:
            raise HTTPException(400, "密码至少 8 位")
        a.password_hash = admin_auth.hash_password(body["password"])
    if body.get("name") is not None:
        a.name = body["name"]
    if body.get("status") in ("active", "disabled"):
        a.status = body["status"]
    db.commit()
    db.refresh(a)
    d = a.to_dict()
    d.pop("password_hash", None)
    return d


@router.delete("/admins/{aid}")
def admin_delete_admin(aid: int, db: Session = Depends(get_db), me: dict = Depends(require_admin)):
    a = db.get(AdminUser, aid)
    if not a:
        raise HTTPException(404, "not found")
    if a.username == me.get("username"):
        raise HTTPException(400, "不能删除当前登录账号")
    db.delete(a)
    db.commit()
    return {"ok": True}


def _user_brief(db: Session, uids: list) -> dict:
    """uid -> 用户简要信息（渠道/openid/昵称/手机号），供后台列表 hover 展示。

    管理员主动查看，这里给出原文（不做脱敏），便于客服核对与排查。
    """
    from app.models import UserIdentity

    uniq = [int(x) for x in {u for u in uids if u}]
    if not uniq:
        return {}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(uniq)).all()}
    chans: dict = {}
    for uid, ch in (
        db.query(UserIdentity.user_id, UserIdentity.channel)
        .filter(UserIdentity.user_id.in_(uniq))
        .all()
    ):
        chans.setdefault(uid, [])
        if ch not in chans[uid]:
            chans[uid].append(ch)
    out = {}
    for uid in uniq:
        u = users.get(uid)
        out[uid] = {
            "user_id": uid,
            "channels": sorted(chans.get(uid, [])),
            "openid": (u.openid if u else "") or "",
            "nickname": (u.nickname if u else "") or "",
            "phone": (u.phone if u else "") or "",
            "avatar": (u.avatar if u else "") or "",
        }
    return out


# ---------- 渠道账号（多渠道多应用） ----------
def _account_user_ids(db: Session, account: str):
    """某渠道账号下的用户 id 子查询（用户/反馈/监控按渠道筛选用）。"""
    from app.models import UserIdentity

    return db.query(UserIdentity.user_id).filter(UserIdentity.channel_account_id == account)

def _account_name_map(db: Session, ids: list) -> dict:
    from app.models import ChannelAccount

    uniq = [x for x in {i for i in ids if i}]
    if not uniq:
        return {}
    return {
        c.id: c.name
        for c in db.query(ChannelAccount).filter(ChannelAccount.id.in_(uniq)).all()
    }


@router.get("/channel-accounts")
def admin_list_channel_accounts(db: Session = Depends(get_db), _=Depends(require_admin)):
    from app.models import ChannelAccount

    return [c.to_dict() for c in db.query(ChannelAccount).order_by(ChannelAccount.id).all()]


@router.post("/channel-accounts")
def admin_create_channel_account(body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    from app.models import ChannelAccount

    aid = (body.get("id") or "").strip()
    if aid and db.get(ChannelAccount, aid):
        raise HTTPException(409, "渠道账号 id 已存在")
    if not aid:
        # 留空则自动生成：类型前缀 + 随机 6 位（保证唯一）
        import secrets as _secrets

        prefix = {"wechat_mp": "wx", "alipay": "ali", "other": "ch"}.get(
            (body.get("type") or "wechat_mp"), "ch"
        )
        for _ in range(20):
            cand = prefix + _secrets.token_hex(3)
            if not db.get(ChannelAccount, cand):
                aid = cand
                break
        else:
            raise HTTPException(500, "生成 id 失败，请重试")
    body = {**body, "id": aid}
    cols = ChannelAccount.__table__.columns.keys()
    acc = ChannelAccount(**{k: v for k, v in body.items() if k in cols})
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc.to_dict()


@router.put("/channel-accounts/{aid}")
def admin_update_channel_account(aid: str, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    from app.models import ChannelAccount

    acc = db.get(ChannelAccount, aid)
    if not acc:
        raise HTTPException(404, "not found")
    for k, v in body.items():
        if k in ChannelAccount.__table__.columns.keys() and k != "id":
            setattr(acc, k, v)
    db.commit()
    db.refresh(acc)
    return acc.to_dict()


@router.get("/channel-accounts/{aid}/virtual-pay")
def admin_get_account_virtual_pay(aid: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    """渠道账户的虚拟支付配置：账户级值 + 全局回落值 + 就绪状态。"""
    from app.models import ChannelAccount
    from app.services import wechat_virtual as wv

    acc = db.get(ChannelAccount, aid)
    if not acc:
        raise HTTPException(404, "not found")
    cfg = wv.account_cfg(acc)
    return {
        "account_id": aid,
        "config": acc.virtual_pay or {},
        "global_fallback": {k: cfg[k] for k in wv.VP_JSON_KEYS},
        "ready": bool(cfg.get("offer_id") and cfg.get("push_token") and cfg.get("aes_key")),
    }


@router.put("/channel-accounts/{aid}/virtual-pay")
def admin_set_account_virtual_pay(aid: str, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    """保存渠道账户级虚拟支付配置（留空字段 = 回落全局）。"""
    from app.models import ChannelAccount

    acc = db.get(ChannelAccount, aid)
    if not acc:
        raise HTTPException(404, "not found")
    from app.services import wechat_virtual as wv

    vp = dict(acc.virtual_pay or {})
    for k in wv.VP_JSON_KEYS:
        if k in body:
            v = body.get(k)
            if k == "sandbox":
                vp[k] = bool(v)
            else:
                v = str(v or "").strip()
                if v:
                    vp[k] = v
                else:
                    vp.pop(k, None)  # 清空 = 回落全局
    acc.virtual_pay = vp or None
    db.commit()
    return {"ok": True, "config": acc.virtual_pay or {}}


@router.delete("/channel-accounts/{aid}")
def admin_delete_channel_account(aid: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    from app.models import ChannelAccount

    acc = db.get(ChannelAccount, aid)
    if not acc:
        raise HTTPException(404, "not found")
    in_use = db.query(Order).filter(Order.channel_account_id == aid).count() or db.query(
        ToolInvocation
    ).filter(ToolInvocation.channel_account_id == aid).count()
    if in_use:
        raise HTTPException(400, "该渠道账号下已有订单或调用记录，不能删除（可改为停用）")
    db.delete(acc)
    db.commit()
    return {"deleted": aid}


# ---------- 渠道活动（营销：充值优惠 / 邀请有奖 / 签到奖励） ----------
@router.get("/campaigns")
def admin_list_campaigns(db: Session = Depends(get_db), _=Depends(require_admin)):
    rows = db.query(ChannelCampaign).order_by(ChannelCampaign.id.desc()).all()
    return [c.to_dict() for c in rows]


def _pi(v, default: int, lo: int, hi: int, label: str) -> int:
    try:
        n = int(default if v in (None, "") else v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{label} 必须是整数")
    if not (lo <= n <= hi):
        raise HTTPException(400, f"{label} 需在 {lo} ~ {hi} 之间")
    return n


def _campaign_payload(body: dict) -> dict:
    def _dt(v):
        # 前端 datetime-local 填的是本地时间，按 APP_TZ 解释后转 UTC 存库（与展示口径一致）
        if not v:
            return None
        try:
            return local_str_to_utc(v)
        except ValueError:
            raise HTTPException(400, f"时间格式不正确：{v}")

    ctype = (body.get("type") or CAMPAIGN_TYPES[0]).strip()
    if ctype not in CAMPAIGN_TYPES:
        raise HTTPException(400, f"未知活动类型：{ctype}")

    # 未填开始时间 = 立即生效。截断到整秒，避免 DATETIME 精度舍入导致"刚建好却短暂未生效"
    start_at = _dt(body.get("start_at")) or utcnow().replace(microsecond=0)
    end_at = _dt(body.get("end_at"))
    if end_at and end_at <= start_at:
        raise HTTPException(400, "结束时间必须晚于开始时间")

    discount = None
    config = None
    cfg = body.get("config") or {}
    if ctype == "discount":
        mode = (cfg.get("mode") or "discount").lower()
        if mode not in ("discount", "bonus"):
            raise HTTPException(400, "优惠方式只支持 打折(discount) 或 送次数(bonus)")
        if mode == "bonus":
            # 送次数：价格不变，按比例额外送次数
            config = {
                "mode": "bonus",
                "bonus_percent": _pi(cfg.get("bonus_percent"), 10, 1, 1000, "送次数比例(%)"),
            }
        else:
            try:
                discount = float(body.get("discount"))
            except (TypeError, ValueError):
                raise HTTPException(400, "折扣必填，如 0.5 表示 5 折")
            if not (0.1 <= discount <= 1):
                raise HTTPException(400, "折扣范围 0.1 ~ 1.0（1.0 为不打折）")
            config = {"mode": "discount"}
    elif ctype == "invite":
        config = {
            "reward_quota": _pi(cfg.get("reward_quota"), 1, 0, 1000, "好友注册即送(次)"),
            "recharge_percent": _pi(cfg.get("recharge_percent"), 10, 0, 100, "首充加奖比例(%)"),
            "first_recharge_days": _pi(cfg.get("first_recharge_days"), 30, 0, 3650, "首充期限(天)"),
            "invite_daily_cap": _pi(cfg.get("invite_daily_cap"), 10, 0, 10000, "邀请奖励每日上限"),
            "invite_monthly_cap": _pi(cfg.get("invite_monthly_cap"), 100, 0, 100000, "邀请奖励每月上限"),
            "recharge_daily_cap": _pi(cfg.get("recharge_daily_cap"), 10, 0, 10000, "首充加奖每日上限"),
            "recharge_monthly_cap": _pi(cfg.get("recharge_monthly_cap"), 100, 0, 100000, "首充加奖每月上限"),
            "valid_days": _pi(cfg.get("valid_days"), 30, 1, 3650, "奖励有效期(天)"),
        }
    elif ctype == "checkin":
        config = {
            "quota": _pi(cfg.get("quota"), 1, 0, 1000, "签到奖励次数"),
            "valid_days": _pi(cfg.get("valid_days"), 30, 0, 3650, "有效期(天，0=不过期)"),
        }
    else:
        raise HTTPException(400, f"未知活动类型：{ctype}")

    return {
        "name": (body.get("name") or "").strip() or "未命名活动",
        "type": ctype,
        "channel": body.get("channel") or "wechat",
        "discount": discount,
        "config": config,
        "start_at": start_at,
        "end_at": end_at,
        "enabled": bool(body.get("enabled", True)),
        "remark": body.get("remark") or "",
    }


@router.post("/campaigns")
def admin_create_campaign(body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    c = ChannelCampaign(**_campaign_payload(body))
    db.add(c)
    db.commit()
    db.refresh(c)
    return c.to_dict()


@router.put("/campaigns/{cid}")
def admin_update_campaign(cid: int, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    c = db.get(ChannelCampaign, cid)
    if not c:
        raise HTTPException(404, "not found")
    for k, v in _campaign_payload(body).items():
        setattr(c, k, v)
    db.commit()
    db.refresh(c)
    return c.to_dict()


@router.delete("/campaigns/{cid}")
def admin_delete_campaign(cid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    c = db.get(ChannelCampaign, cid)
    if not c:
        raise HTTPException(404, "not found")
    db.delete(c)
    db.commit()
    return {"ok": True}


# ---------- 收款明细 / 订单（对账） ----------
# 调用状态中文标签（调用明细 / 用户详情共用）
INV_STATUS_LABELS = {
    "pending": "排队中",
    "running": "执行中",
    "success": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "expired": "已过期",
}

# 会话状态中文标签（会话列表 / 会话详情共用）
SESSION_STATUS_LABELS = {
    "running": "执行中",
    "awaiting_answer": "追问中",
    "awaiting_confirm": "待确认",
    "awaiting_turn": "已完成·可继续",
    "success": "已完成",
    "cancelled": "已取消",
    "expired": "已过期",
    "failed": "失败",
}

# 支付通道（订单收款渠道）：后台「收款明细」筛选用，列表与下拉共用同一套中文标签
_PAYMENT_PROVIDERS = (
    ("mock", "模拟支付"),
    ("wechat_virtual", "微信虚拟支付"),
    ("wechat_merchant", "微信商户支付"),
    ("apple_iap", "Apple 内购"),
)


@router.get("/orders")
def admin_list_orders(
    status: str = "",
    q: str = "",
    channel: str = "",
    platform: str = "",
    provider: str = "",
    campaign: str = "",
    account: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "",
    dir: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """订单/收款明细：支持状态、订单号或用户 ID、渠道、平台、支付通道、活动、时间范围筛选 + 分页，附金额汇总。"""
    from sqlalchemy import func as _func

    from app.models import Account, RechargePackage

    # base：除「支付通道」以外的全部筛选条件（各通道计数也基于它，便于看"切过去能筛出多少"）
    base = db.query(Order)
    if status:
        base = base.filter(Order.status == status)
    if campaign:
        base = base.filter(Order.campaign_name == campaign)
    if channel:
        base = base.filter(Order.client_channel == channel)
    if platform:
        base = base.filter(Order.platform == platform)
    if q.strip():
        kw = q.strip()
        conds = [Order.id.like(f"%{kw}%")]
        if kw.isdigit():
            conds.append(Order.user_id == int(kw))
        base = base.filter(or_(*conds))
    if account:
        base = base.filter(Order.channel_account_id == account)
    for key, val, is_from in (("date_from", date_from, True), ("date_to", date_to, False)):
        dt = _range_dt(val, is_to=not is_from)
        if dt:
            base = base.filter(Order.created_at >= dt) if is_from else base.filter(Order.created_at <= dt)

    query = base.filter(Order.payment_provider == provider) if provider else base
    total = query.count()
    # 汇总（按筛选条件，全量而非当前页）；老订单无 origin_amount 时按实付计
    paid_q = query.filter(Order.status == "paid")
    paid_total = float(paid_q.with_entities(_func.coalesce(_func.sum(Order.amount), 0)).scalar() or 0)
    origin_total = float(
        paid_q.with_entities(
            _func.coalesce(_func.sum(_func.coalesce(Order.origin_amount, Order.amount)), 0)
        ).scalar()
        or 0
    )
    paid_count = paid_q.count()
    pending_count = query.filter(Order.status == "pending").count()

    # 各支付通道的订单数（基于除 provider 外的筛选条件；库中出现但不在枚举里的也一并带上）
    pv_counts = dict(
        base.with_entities(Order.payment_provider, _func.count(Order.id))
        .group_by(Order.payment_provider)
        .all()
    )
    known = dict(_PAYMENT_PROVIDERS)
    providers = [
        {"value": v, "label": lbl, "count": int(pv_counts.get(v) or 0)}
        for v, lbl in _PAYMENT_PROVIDERS
    ]
    for v, n in pv_counts.items():
        if v and v not in known:
            providers.append({"value": v, "label": v, "count": int(n or 0)})

    rows = _apply_sort(
        query,
        {
            "id": Order.id, "created_at": Order.created_at, "user_id": Order.user_id,
            "amount": Order.amount, "origin_amount": Order.origin_amount,
            "discount": Order.discount, "status": Order.status, "paid_at": Order.paid_at,
            "payment_provider": Order.payment_provider,
        },
        sort, dir,
        default=(Order.created_at.desc(), Order.id.desc()),
        tiebreak=Order.id,
    ).offset((page - 1) * size).limit(size).all()

    uids = [o.user_id for o in rows if o.user_id]
    umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {}
    pkg_ids = [o.package_id for o in rows if o.package_id]
    pmap = {}
    if pkg_ids:
        pmap = {p.id: p for p in db.query(RechargePackage).filter(RechargePackage.id.in_(pkg_ids)).all()}
    acct_map = {}
    if uids:
        acct_map = {a.user_id: a for a in db.query(Account).filter(Account.user_id.in_(uids)).all()}
    remain_map = grants_remaining_map(db, list(uids))
    ubrief = _user_brief(db, uids)
    acct_names = _account_name_map(db, [o.channel_account_id for o in rows])

    items = []
    for o in rows:
        u = umap.get(o.user_id)
        p = pmap.get(o.package_id)
        a = acct_map.get(o.user_id)
        items.append(
            {
                "id": o.id,
                "user_id": o.user_id,
                "user": ubrief.get(o.user_id, {}),
                "channel_account_id": o.channel_account_id or "",
                "channel_account_name": acct_names.get(o.channel_account_id or "", ""),
                "user_phone": (u.phone[:3] + "****" + u.phone[7:]) if (u and u.phone) else "",
                "user_nickname": (u.nickname if u else "") or "",
                "package": (p.name if p else ""),
                "quota": (p.quota if p else None),
                "amount": float(o.amount or 0),
                "origin_amount": float(o.origin_amount) if o.origin_amount is not None else None,
                "discount": float(o.discount) if o.discount is not None else None,
                "campaign_name": o.campaign_name or "",
                "platform": o.platform,
                "client_channel": o.client_channel,
                "payment_provider": o.payment_provider,
                # 中文标签由后端统一下发（前端不再自建映射，避免两处不同步）
                "payment_provider_label": known.get(o.payment_provider or "", o.payment_provider or ""),
                "entity": o.entity,
                "status": o.status,
                "created_at": local_iso(o.created_at),
                "paid_at": local_iso(o.paid_at),
                "quota_remain": remain_map.get(o.user_id, 0),
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        # 支付通道下拉：标签 + 当前（除 provider 外）筛选下的订单数
        "providers": providers,
        "summary": {
            "paid_total": round(paid_total, 2),
            "origin_total": round(origin_total, 2),
            "discount_off": round(origin_total - paid_total, 2),
            "paid_count": paid_count,
            "pending_count": pending_count,
        },
    }


@router.get("/transactions")
def admin_list_transactions(
    type: str = "",
    q: str = "",
    sort: str = "",
    dir: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """次数/余额流水（充值、消耗、退款、人工调整）。"""
    from app.models import Transaction

    query = db.query(Transaction)
    if type:
        query = query.filter(Transaction.type == type)
    if q.strip():
        kw = q.strip()
        conds = [Transaction.ref_id.like(f"%{kw}%")]
        if kw.isdigit():
            conds.append(Transaction.user_id == int(kw))
        query = query.filter(or_(*conds))

    total = query.count()
    rows = _apply_sort(
        query,
        {
            "id": Transaction.id, "created_at": Transaction.created_at, "user_id": Transaction.user_id,
            "amount": Transaction.amount, "quota_delta": Transaction.quota_delta, "type": Transaction.type,
        },
        sort, dir,
        default=(Transaction.id.desc(),),
        tiebreak=Transaction.id,
    ).offset((page - 1) * size).limit(size).all()
    uids = [r.user_id for r in rows if r.user_id]
    umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {}
    ubrief = _user_brief(db, uids)
    items = []
    for r in rows:
        d = r.to_dict()
        u = umap.get(r.user_id)
        d["user_phone"] = (u.phone[:3] + "****" + u.phone[7:]) if (u and u.phone) else ""
        d["user"] = ubrief.get(r.user_id, {})
        d["type_label"], d["remark_label"] = tx_labels(r)
        items.append(d)

    # 类型下拉：已知枚举（中文）+ 库中出现过的其它值；count 基于除 type 外的筛选条件
    from sqlalchemy import func as _func

    from app.services.billing import TX_TYPE_LABELS

    cnt_q = db.query(Transaction)
    if q.strip():
        kw = q.strip()
        conds = [Transaction.ref_id.like(f"%{kw}%")]
        if kw.isdigit():
            conds.append(Transaction.user_id == int(kw))
        cnt_q = cnt_q.filter(or_(*conds))
    counts = dict(
        cnt_q.with_entities(Transaction.type, _func.count(Transaction.id)).group_by(Transaction.type).all()
    )
    types = [
        {"value": v, "label": lbl, "count": int(counts.get(v) or 0)}
        for v, lbl in TX_TYPE_LABELS.items()
    ]
    for v, n in counts.items():
        if v and v not in TX_TYPE_LABELS:
            types.append({"value": v, "label": v, "count": int(n or 0)})
    return {"items": items, "total": total, "page": page, "size": size, "types": types}


@router.post("/orders/{oid}/mark-paid")
def admin_mark_order_paid(oid: str, db: Session = Depends(get_db), me: dict = Depends(require_admin)):
    """人工补单：线下/异常订单标记为已支付（会加次数并写流水）。"""
    from app.services.billing import notify_paid

    order = db.get(Order, oid)
    if not order:
        raise HTTPException(404, "订单不存在")
    if order.status == "paid":
        return {"ok": True, "status": "paid", "already": True}
    if order.status == "canceled":
        raise HTTPException(400, "订单已取消，不能补单")
    order = notify_paid(db, oid)
    from app.models import Transaction

    db.add(
        Transaction(
            user_id=order.user_id,
            type="adjust",
            amount=0,
            quota_delta=0,
            ref_id=order.id,
            remark=f"后台人工补单 by {me.get('username')}",
        )
    )
    db.commit()
    return {"ok": True, "status": order.status}


# ---------- 消费总览（全用户聚合） ----------
@router.get("/consumption/users")
def admin_consumption_users(
    q: str = "",
    status: str = "",
    account: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "amount",  # amount | consumed | last_active | id
    dir: str = "desc",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """全用户消费总览：按用户聚合充值金额/消耗次数/剩余次数/最近活跃，支持筛选与排序。"""
    from sqlalchemy import case as _case
    from sqlalchemy import func as _func

    from app.models import ToolInvocation

    def _dt(v: str, key: str):
        if not v:
            return None
        try:
            return local_str_to_utc(v)
        except ValueError:
            raise HTTPException(400, f"{key} 格式应为 YYYY-MM-DD")

    d_from, d_to = _dt(date_from, "date_from"), _dt(date_to, "date_to")

    # 已支付订单聚合（金额、单数）
    oq = db.query(
        Order.user_id.label("uid"),
        _func.count(Order.id).label("order_cnt"),
        _func.coalesce(_func.sum(Order.amount), 0).label("paid_amount"),
    ).filter(Order.status == "paid")
    if d_from:
        oq = oq.filter(Order.created_at >= d_from)
    if d_to:
        oq = oq.filter(Order.created_at <= d_to)
    o_sub = oq.group_by(Order.user_id).subquery()

    # 调用聚合（调用数、消耗次数、成本、最近活跃）
    iq = db.query(
        ToolInvocation.user_id.label("uid"),
        _func.count(ToolInvocation.id).label("calls"),
        _func.coalesce(_func.sum(ToolInvocation.quota_charged), 0).label("quota_consumed"),
        _func.coalesce(_func.sum(ToolInvocation.cost_amount), 0).label("cost"),
        _func.max(ToolInvocation.created_at).label("last_active"),
    )
    if d_from:
        iq = iq.filter(ToolInvocation.created_at >= d_from)
    if d_to:
        iq = iq.filter(ToolInvocation.created_at <= d_to)
    i_sub = iq.group_by(ToolInvocation.user_id).subquery()

    query = (
        db.query(
            User.id,
            User.nickname,
            User.phone,
            User.status,
            User.created_at,
            _func.coalesce(o_sub.c.order_cnt, 0),
            _func.coalesce(o_sub.c.paid_amount, 0),
            _func.coalesce(i_sub.c.calls, 0),
            _func.coalesce(i_sub.c.quota_consumed, 0),
            _func.coalesce(i_sub.c.cost, 0),
            i_sub.c.last_active,
        )
        .outerjoin(o_sub, o_sub.c.uid == User.id)
        .outerjoin(i_sub, i_sub.c.uid == User.id)
    )
    if q.strip():
        kw = q.strip()
        conds = [User.nickname.like(f"%{kw}%"), User.phone.like(f"%{kw}%")]
        if kw.isdigit():
            conds.append(User.id == int(kw))
        query = query.filter(or_(*conds))
    if status:
        query = query.filter(User.status == status)
    if account:
        from app.models import ToolInvocation as _TI

        query = query.filter(
            or_(
                User.id.in_(db.query(Order.user_id).filter(Order.channel_account_id == account)),
                User.id.in_(db.query(_TI.user_id).filter(_TI.channel_account_id == account)),
            )
        )

    total = query.count()
    asc = (dir or "").lower() == "asc"
    if sort == "consumed":
        scol = _func.coalesce(i_sub.c.quota_consumed, 0)
    elif sort == "last_active":
        scol = i_sub.c.last_active
    elif sort == "id":
        scol = User.id
    else:
        scol = _func.coalesce(o_sub.c.paid_amount, 0)
    order = [scol.asc() if asc else scol.desc()]
    if scol is not User.id:
        order.append(User.id.desc())  # 稳定分页
    query = query.order_by(*order)

    rows = query.offset((page - 1) * size).limit(size).all()

    # 汇总（按筛选条件全量）
    paid_total = float(db.query(_func.coalesce(_func.sum(Order.amount), 0)).filter(Order.status == "paid").scalar() or 0)
    consumed_total = int(db.query(_func.coalesce(_func.sum(ToolInvocation.quota_charged), 0)).scalar() or 0)
    active_total = int(db.query(_func.count(_func.distinct(ToolInvocation.user_id))).scalar() or 0)

    items = []
    uids = [r[0] for r in rows]
    remain_map = grants_remaining_map(db, uids)
    stats_map = quota_stats_map(db, uids)   # 统计口径统一走台账
    ubrief = _user_brief(db, uids)
    for r in rows:
        (uid, nickname, phone, st, created_at, order_cnt, paid_amount, calls, consumed, cost, last_active) = r
        items.append(
            {
                "user_id": uid,
                "user": ubrief.get(uid, {}),
                "nickname": nickname or "",
                "phone": (phone[:3] + "****" + phone[7:]) if phone else "",
                "status": st,
                "created_at": local_iso(created_at),
                "order_count": int(order_cnt),
                "paid_amount": round(float(paid_amount), 2),
                "calls": int(calls),
                "quota_consumed": int(consumed),
                "cost": round(float(cost), 4),
                "last_active_at": local_iso(last_active),
                "quota_total": int((stats_map.get(uid) or {}).get("total", 0)),
                "quota_used": int((stats_map.get(uid) or {}).get("used", 0)),
                "quota_expired": int((stats_map.get(uid) or {}).get("expired", 0)),
                "quota_remain": remain_map.get(uid, 0),
            }
        )
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "summary": {
            "paid_total": round(paid_total, 2),
            "consumed_total": consumed_total,
            "active_users": active_total,
            "users_total": db.query(_func.count(User.id)).scalar() or 0,
        },
    }


@router.get("/invocations")
def admin_list_invocations(
    q: str = "",
    tool_id: list[str] = Query(default=[]),
    status: str = "",
    account: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "",
    dir: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """调用明细（按次）：每次请求的 工具 / 扣次数 / 折算金额 / LLM成本 / 状态。

    折算金额 = 扣次数 × 该用户单次均价（用户累计消费 ÷ 累计获得次数；无充值则用全站均价）。
    sort/dir：表头排序（白名单列），空则默认按调用 ID 倒序。
    """
    from sqlalchemy import func as _func

    from app.models import RechargePackage, Tool, ToolInvocation

    def _apply(query, skip_status=False):
        if tool_id:
            query = query.filter(ToolInvocation.tool_id.in_(tool_id))
        if status and not skip_status:
            query = query.filter(ToolInvocation.status == status)
        if q.strip():
            kw = q.strip()
            conds = [ToolInvocation.input_text.like(f"%{kw}%")]
            if kw.isdigit():
                conds.append(ToolInvocation.user_id == int(kw))
            query = query.filter(or_(*conds))
        if account:
            query = query.filter(ToolInvocation.channel_account_id == account)
        for key, val, is_from in (("date_from", date_from, True), ("date_to", date_to, False)):
            dt = _range_dt(val, is_to=not is_from)
            if dt:
                query = query.filter(ToolInvocation.created_at >= dt) if is_from else query.filter(ToolInvocation.created_at <= dt)
        return query

    base = _apply(db.query(ToolInvocation))

    # 全站单次均价（元/次）
    g = (
        db.query(_func.sum(Order.amount), _func.sum(RechargePackage.quota))
        .join(RechargePackage, RechargePackage.id == Order.package_id)
        .filter(Order.status == "paid")
        .first()
    )
    global_unit = (float(g[0]) / float(g[1])) if (g and g[0] and g[1]) else 0.0

    # 按用户算单次均价（充值金额 ÷ 获得次数）
    unit_map: dict = {}
    urows = (
        db.query(Order.user_id, _func.sum(Order.amount), _func.sum(RechargePackage.quota))
        .join(RechargePackage, RechargePackage.id == Order.package_id)
        .filter(Order.status == "paid")
        .group_by(Order.user_id)
        .all()
    )
    for uid, amt, quota in urows:
        if amt and quota:
            unit_map[uid] = float(amt) / float(quota)

    qtotal, ctotal = base.with_entities(
        _func.coalesce(_func.sum(ToolInvocation.quota_charged), 0),
        _func.coalesce(_func.sum(ToolInvocation.cost_amount), 0),
    ).first()

    total = base.count()
    # 表头排序：仅允许白名单列（防注入），并以 id 兜底保证分页稳定
    sort_cols = {
        "id": ToolInvocation.id,
        "created_at": ToolInvocation.created_at,
        "user_id": ToolInvocation.user_id,
        "tool_id": ToolInvocation.tool_id,
        "status": ToolInvocation.status,
        "quota_charged": ToolInvocation.quota_charged,
        "amount_charged": ToolInvocation.amount_charged,
        "cost_amount": ToolInvocation.cost_amount,
    }
    col = sort_cols.get(sort)
    if col is not None:
        primary = col.asc() if (dir or "").lower() == "asc" else col.desc()
        order = (primary, ToolInvocation.id.desc()) if col is not ToolInvocation.id else (primary,)
    else:
        order = (ToolInvocation.id.desc(),)
    rows = base.order_by(*order).offset((page - 1) * size).limit(size).all()

    uids = [r.user_id for r in rows if r.user_id]
    umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {}
    tmap = {t.id: t.name for t in db.query(Tool).all()}
    ubrief = _user_brief(db, uids)
    acct_names = _account_name_map(db, [r.channel_account_id for r in rows])

    # 折算金额合计：优先用快照 amount_charged，历史数据（无快照）按用户单价折算
    stored_total = float(
        base.with_entities(_func.coalesce(_func.sum(ToolInvocation.amount_charged), 0)).scalar() or 0
    )
    legacy_total = 0.0
    for uid, charged in (
        base.filter(ToolInvocation.amount_charged.is_(None))
        .with_entities(ToolInvocation.user_id, _func.coalesce(_func.sum(ToolInvocation.quota_charged), 0))
        .group_by(ToolInvocation.user_id)
        .all()
    ):
        unit = unit_map.get(uid, global_unit)
        legacy_total += float(charged or 0) * unit
    amount_total = stored_total + legacy_total

    items = []
    for r in rows:
        unit = unit_map.get(r.user_id, global_unit)
        charged = int(r.quota_charged or 0)
        u = umap.get(r.user_id)
        # 有快照用真实入账金额，历史无快照的行按当前单价折算（前端标注为估算）
        if r.amount_charged is not None:
            amount, estimated = float(r.amount_charged), False
            unit_shown = float(r.unit_price_snapshot) if r.unit_price_snapshot is not None else round(unit, 4)
        else:
            amount, estimated = round(charged * unit, 2), True
            unit_shown = round(unit, 4)
        items.append(
            {
                "id": r.id,
                "user_id": r.user_id,
                "user": ubrief.get(r.user_id, {}),
                "channel_account_id": r.channel_account_id or "",
                "channel_account_name": acct_names.get(r.channel_account_id or "", ""),
                "user_phone": (u.phone[:3] + "****" + u.phone[7:]) if (u and u.phone) else "",
                "user_nickname": (u.nickname if u else "") or "",
                "tool_id": r.tool_id,
                "tool_name": tmap.get(r.tool_id, r.tool_id or ""),
                "status": r.status,
                "status_label": INV_STATUS_LABELS.get(r.status or "", r.status or ""),
                "quota_charged": charged,
                "unit_price": round(unit_shown, 4),
                "amount_charged": round(amount, 2),
                "amount_is_estimated": estimated,
                "amount_est": round(amount, 2),  # 兼容旧字段名
                "cost_amount": float(r.cost_amount or 0),
                "model_used": r.model_used or "",
                "input_text": (r.input_text or "")[:80],
                "created_at": local_iso(r.created_at),
                "finished_at": local_iso(r.finished_at),
            }
        )
    # 状态下拉：已知枚举（中文）+ 库中出现过的其它值；count 基于除 status 外的筛选条件
    cnt_base = _apply(db.query(ToolInvocation), skip_status=True)
    counts = dict(
        cnt_base.with_entities(ToolInvocation.status, _func.count(ToolInvocation.id))
        .group_by(ToolInvocation.status)
        .all()
    )
    statuses = [
        {"value": v, "label": lbl, "count": int(counts.get(v) or 0)}
        for v, lbl in INV_STATUS_LABELS.items()
    ]
    for v, n in counts.items():
        if v and v not in INV_STATUS_LABELS:
            statuses.append({"value": v, "label": v, "count": int(n or 0)})

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "statuses": statuses,
        "summary": {
            "quota_total": int(qtotal or 0),
            "amount_total": round(amount_total, 2),
            "cost_total": round(float(ctotal or 0), 4),
            "unit_price_global": round(global_unit, 4),
        },
    }


# ---------- 交互式会话（§17） ----------
@router.get("/sessions")
def admin_list_sessions(
    q: str = "",
    status: str = "",
    tool_id: list[str] = Query(default=[]),
    account: str = "",
    sort: str = "",
    dir: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """会话列表。只返回投影的业务字段：**不回传附件正文 / 用户补充内容**（PII），
    input_text 截断展示；排查详情请按 invocation_id 去「调用明细」或库里查。"""
    query = db.query(ToolSession)
    if status:
        query = query.filter(ToolSession.status == status)
    if tool_id:
        # 多工具会话：会话的 tool_id 只是「最近一个子任务」，按工具筛选应命中**任一**子任务
        tids = db.query(SessionTurn.session_id).filter(SessionTurn.tool_id.in_(tool_id))
        query = query.filter(ToolSession.id.in_(tids))
    if account:
        query = query.filter(ToolSession.channel_account_id == account)
    kw = q.strip()
    if kw:
        conds = []
        if kw.isdigit():
            n = int(kw)
            conds = [
                ToolSession.id == n,
                ToolSession.user_id == n,
                ToolSession.invocation_id == n,
            ]
        query = query.filter(or_(*conds)) if conds else query.filter(ToolSession.id == -1)

    total = query.count()
    rows = _apply_sort(
        query,
        {
            "id": ToolSession.id, "created_at": ToolSession.created_at, "updated_at": ToolSession.updated_at,
            "user_id": ToolSession.user_id, "tool_id": ToolSession.tool_id, "status": ToolSession.status,
            "round_count": ToolSession.round_count, "amount_charged": ToolSession.amount_charged,
            "expires_at": ToolSession.expires_at,
        },
        sort, dir,
        default=(ToolSession.id.desc(),),
        tiebreak=ToolSession.id,
    ).offset((page - 1) * size).limit(size).all()
    uids = [r.user_id for r in rows if r.user_id]
    umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {}
    ubrief = _user_brief(db, uids)
    acct_names = _account_name_map(db, [r.channel_account_id for r in rows])
    tmap = {t.id: t.name for t in db.query(Tool).all()}
    # 各会话包含的子任务工具序列（多工具会话展示用）
    sids = [r.id for r in rows]
    turn_tools: dict = {}
    if sids:
        for t in (
            db.query(SessionTurn)
            .filter(SessionTurn.session_id.in_(sids))
            .order_by(SessionTurn.seq)
            .all()
        ):
            turn_tools.setdefault(t.session_id, []).append(t.tool_id)

    items = []
    for r in rows:
        u = umap.get(r.user_id)
        d = r.to_dict()
        d["tool_name"] = tmap.get(r.tool_id, r.tool_id or "")
        seq_tools = turn_tools.get(r.id, [])
        d["tool_ids"] = seq_tools
        d["tool_names"] = [tmap.get(x, x or "") for x in seq_tools]
        d["turn_total"] = len(seq_tools)
        d["status_label"] = SESSION_STATUS_LABELS.get(r.status or "", r.status or "")
        d["channel_account_name"] = acct_names.get(r.channel_account_id or "", "")
        d.pop("attachment_text", None)  # 附件正文不出后台列表
        d.pop("facts", None)            # 用户补充的原文不出后台列表
        d["input_text"] = (d.get("input_text") or "")[:60]
        d["user_phone"] = (u.phone[:3] + "****" + u.phone[7:]) if (u and u.phone) else ""
        d["user"] = ubrief.get(r.user_id, {})
        d["analysis_counts"] = {
            "missing": len((r.analysis or {}).get("missing") or []),
            "suspicious": len((r.analysis or {}).get("suspicious") or []),
        }
        a = r.analysis or {}
        d["analysis"] = {"summary": a.get("summary") or ""}
        items.append(d)
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/sessions/{sid}")
def admin_session_detail(sid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    """会话详情：本会话的全部子任务（多工具会话会含多条）。"""
    s = db.get(ToolSession, sid)
    if not s:
        raise HTTPException(404, "会话不存在")
    rows = (
        db.query(SessionTurn).filter(SessionTurn.session_id == sid).order_by(SessionTurn.seq).all()
    )
    tmap = {t.id: t.name for t in db.query(Tool).all()}
    return {
        "session": {
            "id": s.id,
            "user_id": s.user_id,
            "status": s.status,
            "status_label": SESSION_STATUS_LABELS.get(s.status or "", s.status or ""),
            "turn_count": int(s.turn_count or 0),
            "channel_account_id": s.channel_account_id or "",
            "llm_cost": float(s.llm_cost or 0),
            "created_at": local_iso(s.created_at),
            "expires_at": local_iso(s.expires_at),
            "err": s.err,
        },
        "turns": [
            {
                "id": t.id,
                "seq": t.seq,
                "tool_id": t.tool_id,
                "tool_name": tmap.get(t.tool_id, t.tool_id or ""),
                "status": t.status,
                "status_label": INV_STATUS_LABELS.get(t.status or "", t.status or ""),
                "charged": bool(t.charged),
                "quota_price": t.quota_price,
                "amount_charged": float(t.amount_charged or 0),
                "llm_cost": float(t.llm_cost or 0),
                "prompt_tokens": int(t.prompt_tokens or 0),
                "completion_tokens": int(t.completion_tokens or 0),
                "round_count": t.round_count,
                "invocation_id": t.invocation_id,
                "input_text": (t.input_text or "")[:80],
                "err": t.err,
                "created_at": local_iso(t.created_at),
                "finished_at": local_iso(t.finished_at),
            }
            for t in rows
        ],
    }


# ---------- 工具 Tool ----------
@router.get("/tools")
def admin_list_tools(
    q: str = "",
    category: str = "",
    enabled: str = "",
    channel: str = "",
    hot: str = "",
    executor: str = "",
    async_exec: str = "",
    interactive: str = "",
    attachment: str = "",
    sort_by: str = "",
    sort_dir: str = "desc",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """工具列表（分页）；q 匹配 ID/名称/描述，category 精确匹配，enabled/hot/async_exec/interactive=yes/no 过滤，
    executor=执行器 key（none=纯配置），sort_by/sort_dir 表头排序（白名单字段）。

    channel 渠道筛选：空=全部；none=未绑定渠道（channel_ids 空，全渠道可见）；
    渠道账户 id = 可见于该账户的工具（含未绑定的全渠道工具）。"""
    from sqlalchemy import func as _func

    query = db.query(Tool)
    if category:
        query = query.filter(Tool.category == category)
    if enabled == "yes":
        query = query.filter(Tool.enabled.is_(True))
    elif enabled == "no":
        query = query.filter(Tool.enabled.is_(False))
    if hot == "yes":
        query = query.filter(Tool.is_hot.is_(True))
    elif hot == "no":
        query = query.filter(Tool.is_hot.is_(False))
    if executor == "none":
        query = query.filter(or_(Tool.executor.is_(None), Tool.executor == ""))
    elif executor:
        query = query.filter(Tool.executor == executor)
    if async_exec == "yes":
        query = query.filter(Tool.async_exec.is_(True))
    elif async_exec == "no":
        query = query.filter(Tool.async_exec.is_(False))
    if interactive == "yes":
        query = query.filter(Tool.interactive.is_(True))
    elif interactive == "no":
        query = query.filter(Tool.interactive.is_(False))
    if attachment == "yes":
        query = query.filter(Tool.needs_attachment.is_(True))
    elif attachment == "no":
        query = query.filter(Tool.needs_attachment.is_(False))
    if channel == "none":
        query = query.filter(or_(Tool.channel_ids.is_(None), _func.json_length(Tool.channel_ids) == 0))
    elif channel:
        query = query.filter(
            or_(
                Tool.channel_ids.is_(None),
                _func.json_length(Tool.channel_ids) == 0,
                _func.json_contains(Tool.channel_ids, json.dumps(channel)),
            )
        )
    if q.strip():
        kw = f"%{q.strip()}%"
        query = query.filter(or_(Tool.id.like(kw), Tool.name.like(kw), Tool.description.like(kw)))
    total = query.count()
    # 表头排序：白名单字段 + 方向；未指定时按 sort,id（默认运营顺序）
    sort_cols = {
        # 名称按拼音排序（utf8mb4_zh_0900_as_cs；默认 unicode 码点序对中文不友好）
        "name": Tool.name.collate("utf8mb4_zh_0900_as_cs"),
        "price_quota": Tool.price_quota,
        "async_exec": Tool.async_exec,
        "interactive": Tool.interactive,
        "created_at": Tool.created_at,
        "updated_at": Tool.updated_at,
    }
    col = sort_cols.get(sort_by)
    if col is not None:
        query = query.order_by(col.desc() if sort_dir == "desc" else col.asc(), Tool.id.asc())
    else:
        query = query.order_by(Tool.sort.asc(), Tool.id.asc())
    rows = query.offset((page - 1) * size).limit(size).all()
    items = []
    cat_names = {c.key: c.name for c in db.query(ToolCategory).all()}
    for t in rows:
        d = t.to_dict()
        d["category_name"] = cat_names.get(t.category) or CATEGORY_NAMES.get(t.category, t.category or "其他")
        d["executor_label"] = EXECUTOR_LABELS.get(t.executor, t.executor or "纯配置")
        items.append(d)

    # 分类统计（不受筛选影响）：各分类 全部/启用/停用 数量
    from sqlalchemy import func as _func

    stat_rows = (
        db.query(Tool.category, Tool.enabled, _func.count(Tool.id))
        .group_by(Tool.category, Tool.enabled)
        .all()
    )
    stats: dict = {}
    for cat, en, cnt in stat_rows:
        s = stats.setdefault(cat or "other", {"category": cat or "other", "total": 0, "enabled": 0, "disabled": 0})
        s["total"] += cnt
        if en:
            s["enabled"] += cnt
        else:
            s["disabled"] += cnt
    category_stats = sorted(
        (
            {
                "category": s["category"],
                "name": cat_names.get(s["category"]) or CATEGORY_NAMES.get(s["category"], s["category"]),
                **{k: s[k] for k in ("total", "enabled", "disabled")},
            }
            for s in stats.values()
        ),
        key=lambda x: -x["total"],
    )
    return {
        "items": items, "total": total, "page": page, "size": size,
        "has_more": page * size < total, "category_stats": category_stats,
    }


@router.get("/tools/health")
def admin_tools_health(llm: int = 0, db: Session = Depends(get_db), _=Depends(require_admin)):
    """工具体检：描述完整性 / 混淆对 / 召回自检；llm=1 时额外抽查混淆对能否被分类器区分。"""
    from app.services import tool_health

    return tool_health.report(db, llm_check=bool(llm))


@router.get("/settings/stream")
def admin_get_stream_setting(_=Depends(require_admin)):
    """流式输出开关状态。"""
    from app.services import appsettings

    return {"enabled": appsettings.get_flag(appsettings.STREAM_ENABLED)}


@router.post("/settings/stream")
def admin_set_stream_setting(body: dict = Body(default={}), _=Depends(require_admin)):
    """切换流式输出开关（即时生效；关闭=前端自动回落轮询）。"""
    from app.services import appsettings

    enabled = bool(body.get("enabled"))
    appsettings.set_flag(appsettings.STREAM_ENABLED, enabled)
    return {"enabled": appsettings.get_flag(appsettings.STREAM_ENABLED)}


@router.get("/settings/disclaimer")
def admin_get_disclaimer_setting(_=Depends(require_admin)):
    """免责声明横幅总开关状态（默认开启）。"""
    from app.services import appsettings

    return {"enabled": appsettings.get_flag(appsettings.DISCLAIMER_ENABLED, default=True)}


@router.post("/settings/disclaimer")
def admin_set_disclaimer_setting(body: dict = Body(default={}), _=Depends(require_admin)):
    """切换免责声明横幅总开关（30s 内生效于 C 端工具详情页）。"""
    from app.services import appsettings

    enabled = bool(body.get("enabled"))
    appsettings.set_flag(appsettings.DISCLAIMER_ENABLED, enabled)
    return {"enabled": appsettings.get_flag(appsettings.DISCLAIMER_ENABLED, default=True)}



VP_SETTING_KEYS = (
    "WECHAT_VIRTUAL_OFFER_ID",
    "WECHAT_VIRTUAL_APPKEY",
    "WECHAT_VIRTUAL_SANDBOX_APPKEY",
    "WECHAT_VIRTUAL_PUSH_TOKEN",
    "WECHAT_VIRTUAL_AES_KEY",
)


@router.get("/settings/virtual-pay")
def admin_get_virtual_pay(_=Depends(require_admin)):
    """虚拟支付配置：DB 值 + 就绪状态（后台「支付配置」页；不再回落 .env）。"""
    from app.services import appsettings
    from app.services import wechat_virtual as wv

    return {
        "values": {k: appsettings.get_str(k) for k in VP_SETTING_KEYS},
        "sandbox": wv.global_cfg()["sandbox"],
        "missing": wv.missing_config(),
    }


@router.post("/settings/virtual-pay")
def admin_set_virtual_pay(body: dict = Body(default={}), _=Depends(require_admin)):
    """保存虚拟支付配置（即时生效，无需重启）；留空 = 清除并回落 .env。"""
    from app.services import appsettings
    from app.services import wechat_virtual as wv

    for k in VP_SETTING_KEYS:
        if k in body:
            v = str(body.get(k) or "").strip()
            if v:
                appsettings.set_str(k, v)
            else:
                appsettings.del_str(k)  # 清空 = 回落 .env
    if "WECHAT_VIRTUAL_SANDBOX" in body:
        appsettings.set_str("WECHAT_VIRTUAL_SANDBOX", "true" if body.get("WECHAT_VIRTUAL_SANDBOX") else "false")
    return {"ok": True, "missing": wv.missing_config()}


LIMIT_KEYS = [
    ("rl_recognize_per_min", "识别接口（每 IP/分钟）", 30),
    ("rl_session_user_10m", "新建会话（每用户/10分钟）", 10),
    ("rl_session_ip_10m", "新建会话（每 IP/10分钟）", 30),
    ("rl_global_recognize_per_min", "【全局】识别（全站/分钟）", 300),
    ("rl_global_session_per_min", "【全局】新建会话（全站/分钟）", 60),
    ("rl_global_daily_cost", "【全局】日 LLM 成本上限（元/天）", 10),
    ("session_daily_cost_limit", "单用户日 LLM 成本闸（元/天，可小数）", 2),
    ("tool_cost_alert_threshold", "单工具月成本告警阈值（元/月，tool 级可覆盖）", 50),
    ("session_max_active", "未付费活跃会话上限（个，仅对新会话生效）", 3),
    ("session_ttl_minutes", "会话有效期（分钟，仅对新会话生效）", 30),
]


@router.get("/settings/embedding")
def admin_get_embedding(_=Depends(require_admin)):
    """向量模型配置（后台「🧠 向量」；未配置 provider=local 语义召回降级）。"""
    from app.services.embedding import cfg

    c = cfg()
    return {"provider": c["provider"], "base_url": c["base_url"],
            "api_key": c["api_key"], "model": c["model"]}


@router.post("/settings/embedding")
def admin_set_embedding(body: dict = Body(default={}), _=Depends(require_admin)):
    """保存向量模型配置（30s 内生效；换模型/服务会自动全量重算工具向量）。"""
    from app.services import appsettings

    if body.get("provider") not in ("local", "openai"):
        raise HTTPException(400, "provider 必须为 local 或 openai")
    for k in ("EMBEDDING_PROVIDER", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL"):
        if k in body:
            appsettings.set_str(k, (body.get(k) or "").strip())
    from app.services.embedding import cfg as _cfg

    _cfg()  # 立即刷新缓存
    c = _cfg()
    return {"provider": c["provider"], "base_url": c["base_url"], "model": c["model"]}


@router.get("/settings/limits")
def admin_get_limits(_=Depends(require_admin)):
    """应用层限流阈值（动态可调；未设置项回落 config 默认值）。"""
    from app.core.config import settings as _s
    from app.services import appsettings

    defaults = {
        "rl_recognize_per_min": _s.RATE_LIMIT_PER_MIN,
        "rl_session_user_10m": _s.SESSION_CREATE_PER_10MIN,
        "rl_session_ip_10m": _s.SESSION_CREATE_IP_PER_10MIN,
        "rl_global_recognize_per_min": 300,
        "rl_global_session_per_min": 60,
        "rl_global_daily_cost": 10,
        "session_daily_cost_limit": _s.SESSION_DAILY_COST_LIMIT,
        "tool_cost_alert_threshold": _s.TOOL_COST_ALERT_THRESHOLD,
        "session_max_active": _s.SESSION_MAX_ACTIVE,
        "session_ttl_minutes": _s.SESSION_TTL_MINUTES,
    }
    return {
        "limits": [
            {"key": k, "label": label,
             "value": (appsettings.get_float(k, defaults[k]) if k in ("session_daily_cost_limit", "tool_cost_alert_threshold") else appsettings.get_num(k, defaults[k])),
             "default": defaults[k]}
            for k, label, _ in LIMIT_KEYS
        ],
        "alert_webhook_url": appsettings.get_str("ALERT_WEBHOOK_URL") or _s.ALERT_WEBHOOK_URL,
        "content_security_provider": appsettings.get_str("CONTENT_SECURITY_PROVIDER") or _s.CONTENT_SECURITY_PROVIDER or "mock",
    }


@router.post("/settings/limits")
def admin_set_limits(body: dict = Body(default={}), _=Depends(require_admin)):
    """保存限流阈值（即时生效，无需重启）。"""
    from app.services import appsettings

    saved = []
    for k, _label, _d in LIMIT_KEYS:
        if k not in body:
            continue
        try:
            if k in ("session_daily_cost_limit", "tool_cost_alert_threshold"):
                if body[k] in ("", None):
                    appsettings.set_str(k, "")  # 清空 = 恢复默认
                    saved.append({"key": k, "value": "default"})
                    continue
                v = max(0.1, float(body[k]))  # 成本类阈值允许小数元
                appsettings.set_str(k, str(v))
            else:
                v = max(1, int(body[k]))
                appsettings.set_num(k, v)
            saved.append({"key": k, "value": v})
        except (TypeError, ValueError):
            continue
    if "alert_webhook_url" in body:
        appsettings.set_str("ALERT_WEBHOOK_URL", (body.get("alert_webhook_url") or "").strip())
        saved.append({"key": "ALERT_WEBHOOK_URL", "value": (body.get("alert_webhook_url") or "").strip()})
    if "content_security_provider" in body:
        v = (body.get("content_security_provider") or "mock").strip().lower()
        if v not in ("mock", "wechat"):
            raise HTTPException(400, "content_security_provider 必须为 mock 或 wechat")
        appsettings.set_str("CONTENT_SECURITY_PROVIDER", v)
        saved.append({"key": "CONTENT_SECURITY_PROVIDER", "value": v})
    return {"saved": saved}


@router.get("/settings/llm-mock")
def admin_get_llm_mock(_=Depends(require_admin)):
    """LLM Mock 模式开关状态（默认关闭=真实供应商+降级链）。"""
    from app.services import appsettings

    return {"enabled": appsettings.get_flag("llm_mock", default=False)}


@router.post("/settings/llm-mock")
def admin_set_llm_mock(body: dict = Body(default={}), _=Depends(require_admin)):
    """切换 LLM Mock 模式（30s 内生效；开启后全部模型调用返回假数据，无需 API Key）。"""
    from app.services import appsettings

    enabled = bool(body.get("enabled"))
    appsettings.set_flag("llm_mock", enabled)
    return {"enabled": appsettings.get_flag("llm_mock", default=False)}


# 限流监控的拒绝类别 → 中文标签（与各 allow() 调用点的 category 对应）
MONITOR_DENY_CATEGORIES = [
    ("recognize", "识别（每 IP）"),
    ("global_recognize", "识别（全局）"),
    ("session_user", "建会话（每用户）"),
    ("session_ip", "建会话（每 IP）"),
    ("global_session", "建会话（全局）"),
    ("global_recall", "语义召回（全局）"),
    ("feedback", "反馈提交"),
    ("restock", "补货操作"),
]


def _limits_snapshot() -> dict:
    """供看板与限流弹窗共用的系统健康快照。"""
    import json as _json
    import time as _time
    import urllib.request as _urlreq

    import redis as _redis

    from app.core.config import settings as _s
    from app.services import appsettings
    from app.services.ratelimit import global_daily_cost_get

    r = _redis.Redis.from_url(_s.REDIS_URL, decode_responses=True, socket_timeout=2)
    redis_ok = bool(r.ping())

    from app.services import appsettings as _as

    embed_provider = (_as.get_str("EMBEDDING_PROVIDER") or "local").strip().lower()
    embed_base = (_as.get_str("EMBEDDING_BASE_URL") or "").strip()
    embed_ok = False
    if embed_provider == "openai" and embed_base:
        try:
            health = embed_base[: -len("/v1")] if embed_base.endswith("/v1") else embed_base
            with _urlreq.urlopen(health + "/health", timeout=2) as resp:
                embed_ok = resp.status == 200
        except Exception:  # noqa: BLE001
            embed_ok = False

    minute = int(_time.time()) // 60
    denies = []
    for key, label in MONITOR_DENY_CATEGORIES:
        last_min = int(r.get(f"m:deny:{key}:{minute}") or 0)
        last10 = sum(int(r.get(f"m:deny:{key}:{minute - i}") or 0) for i in range(10))
        today = int(r.get(f"m:deny:{key}:today") or 0)
        denies.append({"key": key, "label": label, "last_min": last_min, "last10": last10, "today": today})

    cost_limit = appsettings.get_num("rl_global_daily_cost", 10)
    cost_today = round(global_daily_cost_get(), 4)
    return {
        "components": {"redis": redis_ok, "embed": embed_ok},
        "embed_provider": embed_provider,
        "global_cost": {"today": cost_today, "limit": cost_limit, "pct": round(cost_today / cost_limit * 100, 1) if cost_limit else 0},
        "denies": denies,
    }


@router.get("/monitor/limits")
def admin_monitor_limits(_=Depends(require_admin)):
    """限流矩阵监控：组件健康 + 全局成本水位 + 各层限流触发次数（近 1 分钟/10 分钟/今日）。"""
    return _limits_snapshot()


@router.get("/monitor/dashboard")
def admin_monitor_dashboard(days: int = 14, db: Session = Depends(get_db), _=Depends(require_admin)):
    """运营看板：KPI 卡 + 14 天趋势 + 工具排行 + 零调用清单 + 系统健康。"""
    from datetime import date, timedelta

    from sqlalchemy import func as _func

    from app.core.timeutil import local_day_start_utc
    from app.models import Order, Tool, ToolSession, ToolStatsDaily, ToolInvocation, User

    days = max(1, min(int(days), 30))
    today = date.today()
    since = today - timedelta(days=days - 1)
    since7 = today - timedelta(days=6)
    day_start_utc = local_day_start_utc()  # 今日 0 点（本地）对应 UTC

    # ---- 今日 KPI（实时查 tool_invocation / order / user / tool_session）----
    inv_q = db.query(ToolInvocation).filter(ToolInvocation.created_at >= day_start_utc)
    kpi_calls = inv_q.count()
    kpi_success = inv_q.filter(ToolInvocation.status == "success").count()
    kpi_dau = db.query(_func.count(_func.distinct(ToolInvocation.user_id))).filter(
        ToolInvocation.created_at >= day_start_utc).scalar() or 0
    kpi_cost = float(db.query(_func.coalesce(_func.sum(ToolInvocation.cost_amount), 0)).filter(
        ToolInvocation.created_at >= day_start_utc).scalar() or 0)
    kpi_sessions = db.query(ToolSession).filter(ToolSession.created_at >= day_start_utc).count()
    kpi_users = db.query(User).filter(User.created_at >= day_start_utc).count()
    kpi_revenue = float(db.query(_func.coalesce(_func.sum(Order.amount), 0)).filter(
        Order.status == "paid", Order.created_at >= day_start_utc).scalar() or 0)

    # ---- 14 天趋势（tool_stats_daily）----
    trend_rows = (
        db.query(ToolStatsDaily)
        .filter(ToolStatsDaily.stat_date >= since)
        .all()
    )
    by_date = {}
    for r in trend_rows:
        d = by_date.setdefault(r.stat_date, {"calls": 0, "success": 0, "llm_cost": 0.0, "amount": 0.0})
        d["calls"] += int(r.calls or 0)
        d["success"] += int(r.success or 0)
        d["llm_cost"] += float(r.llm_cost or 0)
        d["amount"] += float(r.amount_charged or 0)
    trend = []
    for i in range(days):
        d = since + timedelta(days=i)
        v = by_date.get(d, {"calls": 0, "success": 0, "llm_cost": 0.0, "amount": 0.0})
        trend.append({"date": d.isoformat(), **{k: round(v[k], 4) for k in v}})

    # ---- 工具排行（近 7 天，按调用量）----
    rank_rows = (
        db.query(
            ToolStatsDaily.tool_id,
            _func.sum(ToolStatsDaily.calls).label("calls"),
            _func.sum(ToolStatsDaily.success).label("success"),
            _func.sum(ToolStatsDaily.llm_cost).label("cost"),
        )
        .filter(ToolStatsDaily.stat_date >= since7)
        .group_by(ToolStatsDaily.tool_id)
        .order_by(_func.sum(ToolStatsDaily.calls).desc())
        .all()
    )
    _rank_tools = db.query(Tool).filter(Tool.id.in_([r[0] for r in rank_rows])).all() if rank_rows else []
    from app.models import CATEGORY_NAMES as _CAT
    tmap = {t.id: t.name for t in _rank_tools}
    cmap = {t.id: _CAT.get(t.category, t.category or "其他") for t in _rank_tools}
    ranking = [
        {"tool_id": tid, "name": tmap.get(tid, tid), "calls": int(calls),
         "success": int(success), "fail_rate": round((int(calls) - int(success)) / int(calls) * 100, 1) if calls else 0,
         "success_rate": round(int(success) / int(calls) * 100, 1) if calls else None,
         "category": cmap.get(tid, ""), "llm_cost": round(float(cost), 4)}
        for tid, calls, success, cost in rank_rows
    ][:10]

    # ---- 零调用清单（启用 & 上架≥14 天 & 近 14 天无调用）----
    active_ids = {r[0] for r in db.query(ToolStatsDaily.tool_id).filter(ToolStatsDaily.stat_date >= since).all()}
    cutoff = today - timedelta(days=14)
    zero_tools = (
        db.query(Tool)
        .filter(Tool.enabled == True, Tool.created_at <= cutoff)  # noqa: E712
        .all()
    )
    zero_calls = [{"tool_id": t.id, "name": t.name, "created_at": t.created_at.isoformat()[:10]}
                  for t in zero_tools if t.id not in active_ids]

    # ---- 失败率红榜（近 7 天：调用≥10 且失败率≥20%）----
    tmap_all = {t.id: t.name for t in db.query(Tool).all()}
    fail_rows = (
        db.query(
            ToolStatsDaily.tool_id,
            _func.sum(ToolStatsDaily.calls).label("calls"),
            _func.sum(ToolStatsDaily.failed).label("failed"),
        )
        .filter(ToolStatsDaily.stat_date >= since7)
        .group_by(ToolStatsDaily.tool_id)
        .all()
    )
    fail_red = []
    for tid, calls, failed in fail_rows:
        calls, failed = int(calls or 0), int(failed or 0)
        if calls >= 10 and failed / calls >= 0.2:
            fail_red.append({"tool_id": tid, "name": tmap_all.get(tid, tid), "calls": calls, "failed": failed,
                             "rate": round(failed / calls * 100, 1)})
    fail_red.sort(key=lambda x: -x["rate"])

    # ---- 退次榜（近 7 天：扣费后退回，用户不满信号）----
    refund_rows = (
        db.query(
            ToolInvocation.tool_id,
            _func.count(ToolInvocation.id).label("refunded"),
        )
        .filter(
            ToolInvocation.created_at >= since7,
            ToolInvocation.quota_charged > 0,
            ToolInvocation.status == "failed",
        )
        .group_by(ToolInvocation.tool_id)
        .all()
    )
    charged_map = dict(
        db.query(ToolInvocation.tool_id, _func.count(ToolInvocation.id))
        .filter(ToolInvocation.created_at >= since7, ToolInvocation.quota_charged > 0)
        .group_by(ToolInvocation.tool_id)
        .all()
    )
    refund_list = [
        {"tool_id": tid, "name": tmap_all.get(tid, tid), "refunded": int(refunded),
         "charged": int(charged_map.get(tid, 0)),
         "rate": round(int(refunded) / int(charged_map.get(tid, 0)) * 100, 1) if charged_map.get(tid) else 100.0}
        for tid, refunded in refund_rows
    ]
    refund_list.sort(key=lambda x: -x["refunded"])

    return {
        "kpi": {
            "calls": kpi_calls, "success": kpi_success,
            "success_rate": round(kpi_success / kpi_calls * 100, 1) if kpi_calls else None,
            "dau": kpi_dau, "sessions": kpi_sessions, "users_new": kpi_users,
            "revenue": kpi_revenue, "llm_cost": round(kpi_cost, 4),
        },
        "trend": trend,
        "ranking": ranking,
        "zero_calls": zero_calls,
        "fail_red": fail_red,
        "refund_list": refund_list,
        "health": _limits_snapshot(),
    }


@router.get("/tool-options")
def admin_tool_options(db: Session = Depends(get_db), _=Depends(require_admin)):
    """工具表单可选值：分类 / 执行器。

    已知枚举给出中文名；同时并入库中已存在的历史值（免受枚举收敛影响）。
    """
    cats = [
        {"value": c.key, "label": c.name}
        for c in db.query(ToolCategory).filter(ToolCategory.enabled == True).order_by(ToolCategory.sort.asc()).all()  # noqa: E712
    ]
    known_cats = {c["value"] for c in cats}
    # 字典缺默认枚举/历史值时补上，避免下拉缺项
    for k, v in CATEGORY_NAMES.items():
        if k not in known_cats:
            cats.append({"value": k, "label": v})
            known_cats.add(k)
    for (c,) in db.query(Tool.category).distinct().all():
        if c and c not in known_cats:
            cats.append({"value": c, "label": c})
    execs = [{"value": "", "label": "纯配置（默认 simple 图）"}]
    known_execs = set(EXECUTOR_REGISTRY)
    for k in EXECUTOR_REGISTRY:
        execs.append({"value": k, "label": EXECUTOR_LABELS.get(k, k)})
    for (e,) in db.query(Tool.executor).distinct().all():
        if e and e not in known_execs:
            execs.append({"value": e, "label": f"{e}（未注册）"})
    return {"categories": cats, "executors": execs}


@router.get("/channel-options")
def admin_channel_options(db: Session = Depends(get_db), _=Depends(require_admin)):
    """可售渠道下拉项（充值档位）；并入库中出现过的历史渠道值。"""
    out = [{"value": c, "label": CHANNEL_LABELS.get(c, c)} for c in KNOWN_CHANNELS]
    known = set(KNOWN_CHANNELS)
    for (chs,) in db.query(RechargePackage.channels).all():
        for c in (chs or []):
            if c and c not in known:
                known.add(c)
                out.append({"value": c, "label": c})
    return {"channels": out}


@router.get("/tools/{tid}")
def admin_get_tool(tid: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.get(Tool, tid)
    if not t:
        raise HTTPException(404, "not found")
    return t.to_dict()


@router.post("/tools")
def admin_create_tool(body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    tid = body.get("id")
    if not tid:
        raise HTTPException(400, "id required")
    if db.get(Tool, tid):
        raise HTTPException(409, "tool id exists")
    t = Tool(**{k: v for k, v in body.items() if k in Tool.__table__.columns.keys()})
    db.add(t)
    db.commit()
    db.refresh(t)
    return t.to_dict()


@router.put("/tools/{tid}")
def admin_update_tool(tid: str, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.get(Tool, tid)
    if not t:
        raise HTTPException(404, "not found")
    for k, v in body.items():
        if k in Tool.__table__.columns.keys() and k != "id":
            setattr(t, k, v)
    t.version = (t.version or 1) + 1
    db.commit()
    db.refresh(t)
    return t.to_dict()


@router.delete("/tools/{tid}")
def admin_delete_tool(tid: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.get(Tool, tid)
    if not t:
        raise HTTPException(404, "not found")
    db.delete(t)
    db.commit()
    return {"deleted": tid}


@router.post("/tools/batch")
def admin_batch_tools(body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    """工具批量操作：{ids: [...], action, channel_ids?}。

    action：enable / disable（启用停用）、hot / unhot（热门标记）、delete（删除）、
    channel（改可见渠道，需 channel_ids 列表，空列表 = 清空即全渠道可见）。"""
    ids = body.get("ids") or []
    action = body.get("action") or ""
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids required")
    rows = db.query(Tool).filter(Tool.id.in_(ids)).all()
    if action in ("enable", "disable"):
        for t in rows:
            t.enabled = action == "enable"
            t.version = (t.version or 1) + 1
    elif action in ("hot", "unhot"):
        for t in rows:
            t.is_hot = action == "hot"
    elif action == "channel":
        ch = body.get("channel_ids")
        if not isinstance(ch, list):
            raise HTTPException(400, "channel_ids required")
        for t in rows:
            t.channel_ids = ch
            t.version = (t.version or 1) + 1
    elif action == "delete":
        for t in rows:
            db.delete(t)
    else:
        raise HTTPException(400, "unknown action: " + str(action))
    db.commit()
    return {"ok": True, "action": action, "count": len(rows)}


# ---------- 工具分类字典（C 端类目顺序 / 名称的唯一来源） ----------
@router.get("/categories")
def admin_list_categories(db: Session = Depends(get_db), _=Depends(require_admin)):
    """分类字典全量（含停用）：按 sort 升序，附工具数（不限启用态）。"""
    from sqlalchemy import func as _func

    counts = dict(db.query(Tool.category, _func.count(Tool.id)).group_by(Tool.category).all())
    return [
        {
            "key": c.key, "name": c.name, "sort": c.sort,
            "enabled": bool(c.enabled), "tool_count": counts.get(c.key, 0),
        }
        for c in db.query(ToolCategory).order_by(ToolCategory.sort.asc(), ToolCategory.key.asc()).all()
    ]


@router.post("/categories")
def admin_create_category(body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    key = (body.get("key") or "").strip()
    name = (body.get("name") or "").strip()
    if not key or not name:
        raise HTTPException(400, "key 和 name 不能为空")
    if db.get(ToolCategory, key):
        raise HTTPException(409, f"分类 {key} 已存在")
    from sqlalchemy import func as _func

    max_sort = db.query(_func.max(ToolCategory.sort)).scalar() or 0
    db.add(ToolCategory(
        key=key, name=name,
        sort=int(body.get("sort") or max_sort + 10),
        enabled=bool(body.get("enabled", True)),
    ))
    db.commit()
    return {"ok": True}


@router.put("/categories/{key}")
def admin_update_category(key: str, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    c = db.get(ToolCategory, key)
    if not c:
        raise HTTPException(404, "category not found")
    if "name" in body and (body["name"] or "").strip():
        c.name = body["name"].strip()
    if "sort" in body and body["sort"] is not None:
        c.sort = int(body["sort"])
    if "enabled" in body:
        c.enabled = bool(body["enabled"])
    db.commit()
    return {"ok": True}


@router.delete("/categories/{key}")
def admin_delete_category(key: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    c = db.get(ToolCategory, key)
    if not c:
        raise HTTPException(404, "category not found")
    used = db.query(Tool).filter(Tool.category == key).count()
    if used:
        raise HTTPException(400, f"仍有 {used} 个工具属于该分类，请先调整工具归属或改用停用")
    db.delete(c)
    db.commit()
    return {"deleted": key}


# ---------- 充值档位 RechargePackage ----------
@router.get("/packages")
def admin_list_packages(db: Session = Depends(get_db), _=Depends(require_admin)):
    return [p.to_dict() for p in db.query(RechargePackage).order_by(RechargePackage.sort).all()]


@router.get("/packages/{pid}")
def admin_get_package(pid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    p = db.get(RechargePackage, pid)
    if not p:
        raise HTTPException(404, "not found")
    return p.to_dict()


@router.post("/packages")
def admin_create_package(body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    cols = RechargePackage.__table__.columns.keys()
    p = RechargePackage(**{k: v for k, v in body.items() if k in cols})
    db.add(p)
    db.commit()
    db.refresh(p)
    return p.to_dict()


@router.put("/packages/{pid}")
def admin_update_package(pid: int, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    p = db.get(RechargePackage, pid)
    if not p:
        raise HTTPException(404, "not found")
    for k, v in body.items():
        if k in RechargePackage.__table__.columns.keys() and k != "id":
            setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p.to_dict()


@router.delete("/packages/{pid}")
def admin_delete_package(pid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    p = db.get(RechargePackage, pid)
    if not p:
        raise HTTPException(404, "not found")
    db.delete(p)
    db.commit()
    return {"deleted": pid}


# ---------- LLM 供应商 / 模型 / 路由 ----------
def _provider_model_stats(db: Session) -> dict:
    """provider_id -> (模型总数, 启用中模型数)。"""
    stat: dict[str, tuple[int, int]] = {}
    for m in db.query(LLMModel).all():
        total, en = stat.get(m.provider_id, (0, 0))
        stat[m.provider_id] = (total + 1, en + (1 if m.enabled else 0))
    return stat


def _chain_snapshot(db: Session) -> tuple[dict, dict, dict]:
    """(供应商启用表, 模型表, 模型启用表) —— 供「有效节点」判定复用。"""
    prov_enabled = {p.id: bool(p.enabled) for p in db.query(LLMProvider).all()}
    models = {m.id: m for m in db.query(LLMModel).all()}
    return prov_enabled, models, {mid: bool(m.enabled) for mid, m in models.items()}


def _usable(node: dict, models: dict, prov_enabled: dict, model_enabled: dict) -> bool:
    """节点是否真正参与调用：模型存在 且 模型启用 且 其供应商启用。

    供应商以「模型当前所属」为准（链节点里存的 provider_id 可能已过期）。
    """
    m = models.get(node.get("model_id"))
    return bool(m and model_enabled.get(m.id) and prov_enabled.get(m.provider_id, False))


def _roles_broken_by(db: Session, *, provider: str | None = None, model: int | None = None) -> list[str]:
    """模拟「停用某供应商 / 某模型」后，哪些角色的降级链会失去**全部**可用节点。

    仅当该角色当前还有可用节点时才提示，避免对本来就断掉的链反复刷屏。
    """
    now_prov, models, now_model = _chain_snapshot(db)
    after_prov, after_model = dict(now_prov), dict(now_model)
    if provider is not None:
        if not now_prov.get(provider):
            return []  # 本来就停用
        after_prov[provider] = False
    if model is not None:
        if not now_model.get(model):
            return []  # 本来就停用
        after_model[model] = False

    broken = []
    for r in db.query(LLMRouting).all():
        chain = r.chain or []
        if not chain:
            continue
        if any(_usable(n, models, now_prov, now_model) for n in chain) and not any(
            _usable(n, models, after_prov, after_model) for n in chain
        ):
            broken.append(r.role)
    return broken


def _model_chains(db: Session) -> dict:
    """model_id -> 该模型出现在哪些角色的降级链里（模型可被多条链复用）。"""
    out: dict = {}
    for r in db.query(LLMRouting).all():
        for n in r.chain or []:
            mid = n.get("model_id")
            if mid is None:
                continue
            out.setdefault(int(mid), []).append(r.role)
    return out


@router.get("/providers")
def admin_list_providers(db: Session = Depends(get_db), _=Depends(require_admin)):
    """供应商列表：附带其下模型数量与「有效模型数」。

    供应商停用时其下模型在调用链中被跳过，因此有效模型数为 0 ——
    模型自身的 enabled 不做级联改写（避免丢失管理员对单个模型的设置）。
    """
    stat = _provider_model_stats(db)
    out = []
    for p in db.query(LLMProvider).order_by(LLMProvider.priority).all():
        d = p.to_dict()
        total, enabled_cnt = stat.get(p.id, (0, 0))
        d["model_count"] = total
        d["model_enabled_count"] = enabled_cnt
        d["effective_model_count"] = enabled_cnt if p.enabled else 0
        out.append(d)
    return out


@router.get("/providers/{pid}")
def admin_get_provider(pid: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    p = db.get(LLMProvider, pid)
    if not p:
        raise HTTPException(404, "not found")
    d = p.to_dict()
    total, enabled_cnt = _provider_model_stats(db).get(pid, (0, 0))
    d["model_count"] = total
    d["model_enabled_count"] = enabled_cnt
    d["effective_model_count"] = enabled_cnt if p.enabled else 0
    # 停用该供应商会「打断」哪些角色的降级链 —— 供前端弹确认框提示
    d["disable_breaks_roles"] = _roles_broken_by(db, provider=pid)
    return d


@router.post("/providers")
def admin_create_provider(body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    cols = LLMProvider.__table__.columns.keys()
    p = LLMProvider(**{k: v for k, v in body.items() if k in cols})
    db.add(p)
    db.commit()
    db.refresh(p)
    return p.to_dict()


@router.put("/providers/{pid}")
def admin_update_provider(pid: str, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    p = db.get(LLMProvider, pid)
    if not p:
        raise HTTPException(404, "not found")
    for k, v in body.items():
        if k in LLMProvider.__table__.columns.keys() and k != "id":
            setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return p.to_dict()


@router.delete("/providers/{pid}")
def admin_delete_provider(pid: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    p = db.get(LLMProvider, pid)
    if not p:
        raise HTTPException(404, "not found")
    db.delete(p)
    db.commit()
    return {"deleted": pid}


@router.get("/models")
def admin_list_models(db: Session = Depends(get_db), _=Depends(require_admin)):
    """模型目录：附带供应商启用状态、「有效启用」与「所属降级链」。

    有效启用 = 供应商启用 AND 模型启用，与 LLM 网关运行时的判定一致。
    chains = 该模型当前被哪些角色的降级链引用（同一模型可被多条链复用）。
    """
    prov_enabled = {p.id: bool(p.enabled) for p in db.query(LLMProvider).all()}
    chains = _model_chains(db)
    out = []
    for m in db.query(LLMModel).all():
        d = m.to_dict()
        pe = prov_enabled.get(m.provider_id, False)
        d["provider_enabled"] = pe
        d["effective_enabled"] = bool(pe and m.enabled)
        d["chains"] = chains.get(m.id, [])
        out.append(d)
    return out


@router.get("/models/{mid}")
def admin_get_model(mid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    m = db.get(LLMModel, mid)
    if not m:
        raise HTTPException(404, "not found")
    d = m.to_dict()
    prov_enabled = {p.id: bool(p.enabled) for p in db.query(LLMProvider).all()}
    pe = prov_enabled.get(m.provider_id, False)
    d["provider_enabled"] = pe
    d["effective_enabled"] = bool(pe and m.enabled)
    d["chains"] = _model_chains(db).get(m.id, [])
    # 停用该模型会打断哪些角色的降级链 —— 供前端弹确认框提示
    d["disable_breaks_roles"] = _roles_broken_by(db, model=mid)
    return d


@router.post("/models")
def admin_create_model(body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    cols = LLMModel.__table__.columns.keys()
    m = LLMModel(**{k: v for k, v in body.items() if k in cols})
    db.add(m)
    db.commit()
    db.refresh(m)
    return m.to_dict()


@router.put("/models/{mid}")
def admin_update_model(mid: int, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    m = db.get(LLMModel, mid)
    if not m:
        raise HTTPException(404, "not found")
    for k, v in body.items():
        if k in LLMModel.__table__.columns.keys() and k != "id":
            setattr(m, k, v)
    db.commit()
    db.refresh(m)
    return m.to_dict()


@router.put("/models/{mid}/chains")
def admin_set_model_chains(mid: int, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    """设置该模型参与哪些角色的降级链（多选）。

    body: {"chains": ["classifier", "executor"]}
    - 勾选的角色若无该节点 → 追加到链尾（顺序可到「路由」页调整）
    - 未勾选的角色若已有该节点 → 移除
    同一模型可同时参与多条链，无需重复建模型记录。
    """
    m = db.get(LLMModel, mid)
    if not m:
        raise HTTPException(404, "not found")
    wanted = body.get("chains")
    if not isinstance(wanted, list):
        raise HTTPException(400, "chains 必须是数组")
    wanted_set = {str(r) for r in wanted}

    routing = {r.role: r for r in db.query(LLMRouting).all()}
    unknown = sorted(wanted_set - set(routing.keys()))
    if unknown:
        raise HTTPException(400, f"未知角色：{', '.join(unknown)}")

    for role, r in routing.items():
        chain = list(r.chain or [])
        has = any(n.get("model_id") == mid for n in chain)
        if role in wanted_set and not has:
            chain.append({"provider_id": m.provider_id, "model_id": m.id})
        elif role not in wanted_set and has:
            chain = [n for n in chain if n.get("model_id") != mid]
        r.chain = chain
    db.commit()

    d = m.to_dict()
    d["chains"] = _model_chains(db).get(mid, [])
    return d


@router.delete("/models/{mid}")
def admin_delete_model(mid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    m = db.get(LLMModel, mid)
    if not m:
        raise HTTPException(404, "not found")
    db.delete(m)
    db.commit()
    return {"deleted": mid}


def _node_view(node: dict, model_by_id: dict, prov_enabled: dict) -> dict:
    """降级链节点 -> 附带可读标签与有效状态（供后台直接展示，无需手写 JSON）。"""
    mid = node.get("model_id")
    m = model_by_id.get(mid)
    if not m:
        return {**node, "missing": True, "effective": False, "label": f"（模型 #{mid} 已不存在）"}
    pe = prov_enabled.get(m.provider_id, False)
    label = f"{m.provider_id} / {m.model_actual}"
    if m.alias:
        label += f"（{m.alias}）"
    return {
        **node,
        "provider_id": m.provider_id,
        "model_actual": m.model_actual,
        "alias": m.alias,
        "tier": m.tier,
        "enabled": bool(m.enabled),
        "provider_enabled": pe,
        "effective": bool(m.enabled and pe),
        "missing": False,
        "label": label,
    }


@router.get("/routing")
def admin_list_routing(db: Session = Depends(get_db), _=Depends(require_admin)):
    """各角色的降级链（含可读标签与有效状态）。

    成员归属由「模型」页勾选「参与角色」决定（PUT /models/{id}/chains）——
    归属只有一个入口；PUT /routing/{role} 仅用于调整顺序或移出。
    """
    prov_enabled = {p.id: bool(p.enabled) for p in db.query(LLMProvider).all()}
    model_by_id = {m.id: m for m in db.query(LLMModel).all()}
    roles = [
        {
            "role": r.role,
            "chain": [_node_view(n, model_by_id, prov_enabled) for n in (r.chain or [])],
        }
        for r in db.query(LLMRouting).all()
    ]
    return {"roles": roles}


@router.put("/routing/{role}")
def admin_update_routing(role: str, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    """更新降级链：**只用于调整顺序与移出节点**。

    模型归属采用「单一入口」：成员由「模型」页勾选「参与角色」决定
    （PUT /models/{id}/chains）。此处若传入尚未在该链中的模型，直接拒绝，
    避免出现「没勾选该角色却被加进链里」的不一致。
    节点只给 model_id，provider_id 由模型推导（防止与所属供应商不一致）。
    """
    r = db.query(LLMRouting).filter(LLMRouting.role == role).first()
    if not r:
        raise HTTPException(404, "role not found")
    chain = body.get("chain")
    if chain is not None:
        if not isinstance(chain, list):
            raise HTTPException(400, "chain 必须是数组")
        current_ids = {n.get("model_id") for n in (r.chain or [])}
        cleaned: list[dict] = []
        seen: set[int] = set()
        for idx, node in enumerate(chain, 1):
            if not isinstance(node, dict):
                raise HTTPException(400, f"第 {idx} 个节点格式错误")
            mid = node.get("model_id")
            m = db.get(LLMModel, mid) if mid is not None else None
            if not m:
                raise HTTPException(400, f"第 {idx} 个节点：模型不存在（model_id={mid}）")
            if m.id not in current_ids:
                raise HTTPException(
                    400,
                    f"第 {idx} 个节点：模型「{m.model_actual}」尚未加入 {role} 链。"
                    f"加入请到「模型」页勾选该模型的「参与角色」，此处只调整顺序或移出。",
                )
            if m.id in seen:
                raise HTTPException(400, f"第 {idx} 个节点：模型「{m.model_actual}」重复")
            seen.add(m.id)
            cleaned.append({"provider_id": m.provider_id, "model_id": m.id})
        r.chain = cleaned
    db.commit()
    db.refresh(r)
    return r.to_dict()


# ---------- 监控 Monitoring ----------
def _channel_row(db: Session, cid: str, name: str, ctype: str = "", enabled: bool = True) -> dict:
    """一个渠道账号（或「全部渠道」）的本月经营对比行。"""
    ms = monthly_status(db, cid or None)
    bz = biz_metrics(db, cid or None)
    return {
        "id": cid,
        "name": name,
        "type": ctype,
        "enabled": enabled,
        "month_limit": ms["month_limit"],
        "warn_threshold": ms["warn_threshold"],
        "stop_threshold": ms["stop_threshold"],
        "paid_total": ms["paid_total"],
        "ratio": ms["ratio"],
        "warn": ms["warn"],
        "sold_out": ms["sold_out"],
        "quota_sold": bz["quota_sold"],
        "calls_total": bz["calls_total"],
        "calls_success": bz["calls_success"],
        "quota_consumed": bz["quota_consumed"],
        "cost_total": bz["cost_total"],
        "margin": bz["margin"],
        "margin_pct": bz["margin_pct"],
    }


@router.get("/monitor")
def admin_monitor(account: str = "", db: Session = Depends(get_db), _=Depends(require_admin)):
    """运营监控：本月收款/月限、经营指标、渠道对比、内容安全命中、告警历史、模型备案状态。"""
    ms = monthly_status(db, account or None)
    audit_q = db.query(AuditLog).filter(AuditLog.passed == False)  # noqa: E712
    if account:
        audit_q = audit_q.filter(AuditLog.user_id.in_(_account_user_ids(db, account)))
    audit_hits = audit_q.order_by(AuditLog.id.desc()).limit(20).all()
    alerts = db.query(AlertLog).order_by(AlertLog.id.desc()).limit(20).all()
    audit_ub = _user_brief(db, [a.user_id for a in audit_hits])
    providers = [
        {"id": p.id, "name": p.name, "filing_no": p.filing_no or "", "enabled": p.enabled}
        for p in db.query(LLMProvider).order_by(LLMProvider.priority).all()
    ]
    from app.services.compliance import model_filings

    from app.models import ChannelAccount

    accounts = db.query(ChannelAccount).order_by(ChannelAccount.id).all()
    channels = [
        _channel_row(db, c.id, c.name or c.id, c.type or "", bool(c.enabled)) for c in accounts
    ]
    channels_total = _channel_row(db, "", "全部渠道")

    return {
        "monthly": ms,
        "biz": biz_metrics(db, account or None),
        # 渠道对比：始终统计全部渠道（不受上面的 account 筛选影响），便于横向比较
        "channels": channels,
        "channels_total": channels_total,
        "audit_hits": [
            {**a.to_dict(), "user": audit_ub.get(a.user_id, {})}
            for a in audit_hits
        ],
        "alerts": [a.to_dict() for a in alerts],
        "providers": providers,
        # 备案以模型维度为准（模型优先、供应商兜底，§16.3-B）
        "model_filings": model_filings(db),
    }


# ---------- 反馈/举报 Feedback（§16.3-C） ----------
@router.get("/feedback")
def admin_list_feedback(
    status: str = "",
    type: str = "",
    q: str = "",
    account: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """反馈列表：支持状态/类型/关键词/时间筛选 + 分页。"""
    query = db.query(Feedback)
    if account:
        query = query.filter(Feedback.user_id.in_(_account_user_ids(db, account)))
    if status:
        query = query.filter(Feedback.status == status)
    if type:
        query = query.filter(Feedback.type == type)
    if q.strip():
        kw = f"%{q.strip()}%"
        query = query.filter(Feedback.content.like(kw))
    for key, val, op in (("date_from", date_from, "ge"), ("date_to", date_to, "le")):
        dt = _range_dt(val, is_to=(op == "le"))
        if dt:
            query = query.filter(Feedback.created_at >= dt) if op == "ge" else query.filter(Feedback.created_at <= dt)

    total = query.count()
    rows = query.order_by(Feedback.id.desc()).offset((page - 1) * size).limit(size).all()
    items = [f.to_dict() for f in rows]

    # 补充对应用户的联系方式脱敏信息，方便回访
    uids = [i["user_id"] for i in items if i.get("user_id")]
    if uids:
        umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()}
        for i in items:
            u = umap.get(i.get("user_id"))
            i["user_phone"] = (u.phone[:3] + "****" + u.phone[7:]) if (u and u.phone) else ""
            i["user_nickname"] = (u.nickname if u else "") or ""
        ub = _user_brief(db, uids)
        for i in items:
            i["user"] = ub.get(i.get("user_id"), {})
    return {"items": items, "total": total, "page": page, "size": size}


@router.put("/feedback/{fid}")
def admin_update_feedback(fid: int, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    f = db.get(Feedback, fid)
    if not f:
        raise HTTPException(404, "not found")
    st = body.get("status")
    if st and st not in ("open", "processed", "ignored"):
        raise HTTPException(400, "status must be open|processed|ignored")
    if st:
        f.status = st
    # 回复（用户端「我的反馈记录」可见）；回复即视为已处理
    if "reply" in body:
        reply = (body.get("reply") or "").strip()
        if len(reply) > 1000:
            raise HTTPException(400, "回复不能超过 1000 字")
        f.reply = reply or None
        f.replied_at = utcnow() if reply else None
        if reply and not st:
            f.status = "processed"
    db.commit()
    db.refresh(f)
    return f.to_dict()


# ---------- 停售预约 waitlist（§16.2-B） ----------
@router.get("/waitlist")
def admin_waitlist(db: Session = Depends(get_db), _=Depends(require_admin)):
    rows = db.query(RestockWaitlist).order_by(RestockWaitlist.id.desc()).limit(200).all()
    return [r.to_dict() for r in rows]


@router.put("/waitlist/{wid}")
def admin_update_waitlist(wid: int, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    """人工标记已触达（月度任务也会自动置位）。"""
    r = db.get(RestockWaitlist, wid)
    if not r:
        raise HTTPException(404, "not found")
    r.notified = bool(body.get("notified", True))
    from app.models import utcnow

    r.notified_at = utcnow() if r.notified else None
    db.commit()
    db.refresh(r)
    return r.to_dict()


# ---------- 用户管理（封禁/恢复，§16.3-C） ----------
@router.get("/users")
def admin_list_users(
    q: str = "",
    status: str = "",
    phone_bound: str = "",
    channel: str = "",
    account: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """用户列表：支持关键词/状态/手机号绑定/渠道筛选 + 分页。"""
    query = db.query(User)
    if q.strip():
        kw = q.strip()
        conds = [User.nickname.like(f"%{kw}%"), User.phone.like(f"%{kw}%")]
        if kw.isdigit():
            conds.append(User.id == int(kw))
        query = query.filter(or_(*conds))
    if status:
        query = query.filter(User.status == status)
    if phone_bound == "yes":
        query = query.filter(User.phone.isnot(None), User.phone != "")
    elif phone_bound == "no":
        query = query.filter(or_(User.phone.is_(None), User.phone == ""))
    if channel:
        from app.models import UserIdentity

        sub = db.query(UserIdentity.user_id).filter(UserIdentity.channel == channel)
        query = query.filter(User.id.in_(sub))
    if account:
        query = query.filter(User.id.in_(_account_user_ids(db, account)))

    total = query.count()
    users = query.order_by(User.id.desc()).offset((page - 1) * size).limit(size).all()

    # 仅统计当前页用户的内容安全命中次数
    ids = [u.id for u in users]
    hits: dict = {}
    if ids:
        hit_rows = (
            db.query(AuditLog.user_id, AuditLog.id)
            .filter(AuditLog.passed == False, AuditLog.user_id.in_(ids))  # noqa: E712
            .all()
        )
        for uid, _id in hit_rows:
            hits[uid] = hits.get(uid, 0) + 1

    # 当前页用户的账户（剩余次数）与累计消费（已支付订单）
    from sqlalchemy import func as _func

    from app.models import Account

    acct_map = {}
    if ids:
        for a in db.query(Account).filter(Account.user_id.in_(ids)).all():
            acct_map[a.user_id] = a

    paid_map: dict = {}
    if ids:
        rows = (
            db.query(
                Order.user_id,
                _func.count(Order.id),
                _func.coalesce(_func.sum(Order.amount), 0),
            )
            .filter(Order.user_id.in_(ids), Order.status == "paid")
            .group_by(Order.user_id)
            .all()
        )
        for uid, cnt, amt in rows:
            paid_map[uid] = (int(cnt), float(amt or 0))

    out = []
    uids = [u.id for u in users]
    remain_map = grants_remaining_map(db, uids)  # 可用次数（按台账）
    stats_map = quota_stats_map(db, uids)       # 次数统计口径（台账）
    ubrief = _user_brief(db, uids)
    # 用户所属渠道（一个用户可多渠道，如先微信后支付宝）
    chans: dict = {}
    if uids:
        from app.models import UserIdentity

        for uid, ch in db.query(UserIdentity.user_id, UserIdentity.channel).filter(UserIdentity.user_id.in_(uids)).all():
            chans.setdefault(uid, [])
            if ch not in chans[uid]:
                chans[uid].append(ch)

    for u in users:
        d = u.to_dict()
        d["openid"] = (u.openid[:10] + "***") if u.openid else ""
        d["phone"] = (u.phone[:3] + "****" + u.phone[7:]) if u.phone else ""
        d["audit_hit_count"] = hits.get(u.id, 0)
        d["channels"] = sorted(chans.get(u.id, []))  # 为空说明是历史数据（无 identity 记录）
        d["channel"] = d["channels"][0] if d["channels"] else ""
        d["user"] = ubrief.get(u.id, {})
        st = stats_map.get(u.id) or {}
        d["quota_total"] = int(st.get("total", 0))
        d["quota_used"] = int(st.get("used", 0))
        d["quota_expired"] = int(st.get("expired", 0))
        d["quota_remain"] = remain_map.get(u.id, 0)
        cnt, amt = paid_map.get(u.id, (0, 0.0))
        d["order_count"] = cnt
        d["paid_amount"] = round(amt, 2)
        out.append(d)
    return {"items": out, "total": total, "page": page, "size": size}


@router.get("/users/{uid}")
def admin_user_detail(uid: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    """单个用户的消费/使用明细：账户、汇总、最近订单、流水、调用记录。"""
    from sqlalchemy import case as _case
    from sqlalchemy import func as _func

    from app.models import Account, RechargePackage, ToolInvocation, Transaction

    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "用户不存在")

    acct = db.get(Account, uid)
    st = quota_stats_map(db, [uid]).get(uid, {})  # 次数统计（台账口径）
    row = db.query(
        _func.count(Order.id),
        _func.coalesce(_func.sum(_case((Order.status == "paid", Order.amount), else_=0)), 0),
    ).filter(Order.user_id == uid).first()
    order_count = int(row[0] or 0)
    paid_amount = float(row[1] or 0)

    inv_row = db.query(
        _func.count(ToolInvocation.id),
        _func.coalesce(_func.sum(ToolInvocation.quota_charged), 0),
        _func.coalesce(_func.sum(ToolInvocation.cost_amount), 0),
        _func.max(ToolInvocation.created_at),
    ).filter(ToolInvocation.user_id == uid).first()
    inv_count, quota_charged, cost_total, last_active = int(inv_row[0] or 0), int(inv_row[1] or 0), float(inv_row[2] or 0), inv_row[3]

    paid_quota = 0
    pkg_ids = [o.package_id for o in db.query(Order.package_id).filter(Order.user_id == uid, Order.status == "paid").all() if o.package_id]
    if pkg_ids:
        paid_quota = int(
            db.query(_func.coalesce(_func.sum(RechargePackage.quota), 0))
            .filter(RechargePackage.id.in_(pkg_ids))
            .scalar()
            or 0
        )

    orders = (
        db.query(Order).filter(Order.user_id == uid).order_by(Order.created_at.desc()).limit(10).all()
    )
    pmap = {}
    if orders:
        ids = [o.package_id for o in orders if o.package_id]
        if ids:
            pmap = {p.id: p for p in db.query(RechargePackage).filter(RechargePackage.id.in_(ids)).all()}
    txs = (
        db.query(Transaction).filter(Transaction.user_id == uid).order_by(Transaction.id.desc()).limit(20).all()
    )
    invs = (
        db.query(ToolInvocation).filter(ToolInvocation.user_id == uid).order_by(ToolInvocation.id.desc()).limit(10).all()
    )
    tmap = {t.id: t.name for t in db.query(Tool).all()}
    chans = []
    from app.models import UserIdentity

    for (_uid, ch) in db.query(UserIdentity.user_id, UserIdentity.channel).filter(UserIdentity.user_id == uid).all():
        if ch not in chans:
            chans.append(ch)

    return {
        "user": {
            "id": u.id,
            "openid": (u.openid[:10] + "***") if u.openid else "",
            "phone": (u.phone[:3] + "****" + u.phone[7:]) if u.phone else "",
            "nickname": u.nickname or "",
            "status": u.status,
            "created_at": local_iso(u.created_at),
            "channels": sorted(chans),
        },
        "account": {
            "balance": float(acct.balance) if acct else 0,
            # 次数统计统一走台账，保证「可用 + 已用 + 已过期 = 合计」
            "quota_total": int(st.get("total", 0)),
            "quota_used": int(st.get("used", 0)),
            "quota_expired": int(st.get("expired", 0)),
            "quota_remain": quota_remain(db, u.id),
        },
        "stats": {
            "order_count": order_count,
            "paid_amount": round(paid_amount, 2),
            "paid_quota": paid_quota,
            "invocation_count": inv_count,
            "quota_charged": quota_charged,
            "cost_total": round(cost_total, 4),
            "last_active_at": local_iso(last_active),
        },
        "orders": [
            {
                "id": o.id,
                "package": (pmap.get(o.package_id).name if pmap.get(o.package_id) else ""),
                "amount": float(o.amount or 0),
                "origin_amount": float(o.origin_amount) if o.origin_amount is not None else None,
                "discount": float(o.discount) if o.discount is not None else None,
                "campaign_name": o.campaign_name or "",
                "status": o.status,
                "created_at": local_iso(o.created_at),
                "paid_at": local_iso(o.paid_at),
            }
            for o in orders
        ],
        "transactions": [
            {**t.to_dict(), "type_label": tx_labels(t)[0], "remark_label": tx_labels(t)[1]}
            for t in txs
        ],
        "invocations": [
            {
                "id": i.id,
                "tool_id": i.tool_id,
                "tool_name": tmap.get(i.tool_id, i.tool_id or ""),
                "status": i.status,
                "status_label": INV_STATUS_LABELS.get(i.status or "", i.status or ""),
                "quota_charged": i.quota_charged,
                "unit_price": float(i.unit_price_snapshot) if i.unit_price_snapshot is not None else None,
                "amount_charged": float(i.amount_charged) if i.amount_charged is not None else None,
                "cost_amount": float(i.cost_amount or 0),
                "input_text": (i.input_text or "")[:60],
                "created_at": local_iso(i.created_at),
            }
            for i in invs
        ],
    }


@router.put("/users/{uid}")
def admin_update_user(uid: int, body: dict = Body(default={}), db: Session = Depends(get_db), _=Depends(require_admin)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "not found")
    st = body.get("status")
    if st not in ("normal", "ban"):
        raise HTTPException(400, "status must be normal|ban")
    u.status = st
    db.commit()
    db.refresh(u)
    return u.to_dict()
