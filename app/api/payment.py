import json
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import get_current_openid
from app.models import Order
from app.services.billing import notify_paid, user_id_by_openid

router = APIRouter()


def _payment_mock_for(db: Session, order_id) -> bool:
    """订单归属渠道的支付 Mock 开关；无渠道信息默认 True（安全侧，联调可用）。"""
    from app.models import ChannelAccount

    order = db.get(Order, order_id) if order_id else None
    if order and order.channel_account_id:
        acc = db.get(ChannelAccount, order.channel_account_id)
        if acc:
            return bool(acc.payment_mock)
    acc = db.query(ChannelAccount).filter(ChannelAccount.id == "default").first()
    return bool(acc.payment_mock) if acc else True


@router.post("/notify")
def notify(
    body: dict = Body(default={}),
    x_pay_sign: str = Header(None),
    db: Session = Depends(get_db),
):
    """支付异步回调（真实环境）。

    生产要求：PAYMENT_MOCK=false 且配置 PAYMENT_NOTIFY_SECRET，回调需带 X-Pay-Sign；
    完整接入时此处替换为微信支付平台证书验签 + 解密回调报文。
    """
    # 回调是否免验签由订单归属渠道的 payment_mock 决定（后台渠道账号配置）
    if _payment_mock_for(db, body.get("order_id")):
        raise HTTPException(400, "当前渠道为模拟支付模式，请使用 /api/payment/mock-pay（仅限本人订单）")
    secret = settings.PAYMENT_NOTIFY_SECRET
    if not secret:
        raise HTTPException(500, "未配置 PAYMENT_NOTIFY_SECRET，支付回调不可用")
    if x_pay_sign != secret:
        raise HTTPException(401, "回调验签失败")

    order_id = body.get("order_id")
    if not order_id:
        raise HTTPException(400, "order_id required")
    order = notify_paid(db, order_id)
    return {"status": order.status if order else "not_found"}


@router.post("/mock-pay")
def mock_pay(
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """模拟支付成功（仅该渠道开启支付 Mock 时可用，且只能支付自己的待付款订单）。"""
    order_id = body.get("order_id")
    if not order_id:
        raise HTTPException(400, "order_id required")
    if not _payment_mock_for(db, order_id):
        raise HTTPException(400, "模拟支付已关闭，请走真实支付流程")

    user_id = user_id_by_openid(db, openid)
    order = db.get(Order, order_id)
    if not order or order.user_id != user_id:
        raise HTTPException(404, "订单不存在")
    if order.status == "paid":
        return {"status": "paid", "order_id": order.id, "already": True}
    order = notify_paid(db, order_id)
    return {"status": order.status, "order_id": order.id, "amount": float(order.amount)}


# ---------- 微信虚拟支付（wechat_virtual）：消息推送回调 ----------
# 多渠道账户：每个小程序渠道账户有独立 Token/AESKey（后台「渠道账号 → 支付配置」），
# 未配置的账户回落全局「支付配置」。回调按 Token 逐账户验签识别来源小程序。


def _push_candidates(db: Session):
    """可匹配的账户推送配置；为空时给出明确指引。"""
    from app.services import wechat_virtual as wv

    candidates = wv.iter_push_configs(db)
    if not candidates:
        raise HTTPException(
            500,
            "无可用虚拟支付推送配置：请在后台「渠道账号 → 支付配置」或「支付配置」页填写 Token 与 EncodingAESKey（即时生效）",
        )
    return candidates


@router.get("/virtual-push")
def virtual_push_verify(
    signature: str = Query(""),
    timestamp: str = Query(""),
    nonce: str = Query(""),
    echostr: str = Query(""),
    db: Session = Depends(get_db),
):
    """消息推送 URL 校验（每个小程序的 mp 后台保存时触发）：验签后返回解密出的 echostr 明文。"""
    from app.services import wechat_virtual as wv

    matched = wv.resolve_push_config(db, signature, timestamp, nonce, echostr)
    if not matched:
        _push_candidates(db)  # 无配置时给出明确 500；有配置但不匹配 → 401
        raise HTTPException(401, "验签失败：Token 不匹配任何已配置的渠道账户")
    _, cfg = matched
    return PlainTextResponse(wv.decrypt_push(echostr, cfg["aes_key"]))


@router.post("/virtual-push")
async def virtual_push(
    request: Request,
    signature: str = Query(""),
    timestamp: str = Query(""),
    nonce: str = Query(""),
    db: Session = Depends(get_db),
):
    """虚拟支付异步通知（走消息推送通道，多小程序共用本 URL）。

    流程：按 Token 识别来源账户 → AES 解密事件 JSON → 按 out_trade_no 入账
    （notify_paid 幂等，微信会重试直到收到成功回包）→ 用同账户密钥加密回包。
    TODO: 事件字段名（如 OutTradeNo / PaidTime）与报文形态（JSON 或 form）
    联调时对照虚拟支付 2.0 文档确认，此处两者兼容。
    """
    from app.services import wechat_virtual as wv

    raw = await request.body()
    text = (raw or b"").decode()
    body: dict = {}
    if text:
        try:
            body = json.loads(text)
        except ValueError:
            body = {k: v[0] for k, v in parse_qs(text).items()}
    encrypt = body.get("Encrypt") or ""
    matched = wv.resolve_push_config(db, signature, timestamp, nonce, encrypt)
    if not matched:
        _push_candidates(db)
        raise HTTPException(401, "回调验签失败：Token 不匹配任何已配置的渠道账户")
    acc, cfg = matched
    try:
        event = json.loads(wv.decrypt_push(encrypt, cfg["aes_key"]))
    except Exception:  # noqa: BLE001 解密失败按失败回包让微信重试
        raise HTTPException(400, "报文解密失败")

    # 支付成功事件：以商户单号定位订单，入账幂等（已 paid 直接返回成功）
    event_name = str(event.get("Event") or event.get("event") or "").lower()
    out_trade_no = event.get("OutTradeNo") or event.get("out_trade_no") or ""
    if out_trade_no and event_name in ("xpay_goods_deliver", "order_paid", "pay_ok", "paid"):
        order = db.get(Order, out_trade_no)
        if order and order.channel_account_id and order.channel_account_id != acc.id:
            raise HTTPException(400, f"单号归属账户 {order.channel_account_id} 与推送来源 {acc.id} 不一致")
        notify_paid(db, out_trade_no)  # 内部幂等：非 pending 订单直接跳过

    # 回包加密 JSON（ErrCode=0 表示接收成功；字段名 TODO 核对文档）
    reply = wv.encrypt_push_reply(json.dumps({"ErrCode": 0, "ErrMsg": "ok"}, separators=(",", ":")), cfg["aes_key"])
    return PlainTextResponse(reply)
