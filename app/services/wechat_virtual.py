"""微信小程序虚拟支付（Virtual Pay 2.0）适配层 —— 骨架。

背景：个人主体小程序唯一合规收款路径（2026-08-31 起开放）。只支持虚拟商品
（本项目的「次数包」），月支付限额 10 万，费率非 iOS 1% / iOS 12%（含 Apple 佣金）。

与普通微信支付商户号（mchid + V3 回调）的关键差异：
  - 支付参数不走 prepay_id，而是服务端对 signData 做 HMAC-SHA256 签名后，
    小程序端调 wx.requestVirtualPayment({signData, sign, mode})；
  - 支付回调走「消息推送」通道（每个小程序各自配置 URL + Token + EncodingAESKey），
    验签算法为 msg_signature（token/timestamp/nonce/密文 字典序 sha1），
    报文为 AES-256-CBC 加密，回包也必须加密 JSON，否则微信按失败重试。

多渠道账户：每个 wechat_mp 渠道账户（= 一个小程序）可有独立的一套
OfferID / AppKey / 推送密钥（channel_account.virtual_pay，后台「渠道账号 → 支付配置」维护），
未覆盖的字段回落全局配置（后台「支付配置」/ .env）。回调按 Token 逐账户匹配识别来源小程序。

待办（联调时对照虚拟支付 2.0 官方文档确认后去掉 TODO）：
  - 回调事件字段名（下方按 OutTradeNo 取商户单号，需实测确认）；
  - 查单 / 退款 API 的完整路径与签名方式（需各小程序 access_token）；
  - iOS 端 IAP 模式的 signData 差异（platform=ios 时苹果佣金 12%）。
"""
import base64
import hashlib
import hmac
import json
import os
import struct

from Crypto.Cipher import AES

from app.models import ChannelAccount, Order

# 全局配置键（app_setting 表；**不回落 .env**——虚拟支付配置一律后台维护；账户级 virtual_pay 未覆盖的字段用它们）
CONFIG_KEYS = (
    "WECHAT_VIRTUAL_OFFER_ID",
    "WECHAT_VIRTUAL_APPKEY",
    "WECHAT_VIRTUAL_SANDBOX_APPKEY",
    "WECHAT_VIRTUAL_PUSH_TOKEN",
    "WECHAT_VIRTUAL_AES_KEY",
)

VP_JSON_KEYS = ("offer_id", "appkey", "sandbox_appkey", "push_token", "aes_key", "sandbox")


def get_config(name: str) -> str:
    """全局配置解析：仅读 app_setting（后台「支付配置」改，即时生效）；不再回落 .env。"""
    from app.services import appsettings

    return appsettings.get_str(name)


def missing_config() -> list[str]:
    """全局回落配置中回调必需但缺失的项（后台「支付配置」页据此提示）。"""
    return [k for k in ("WECHAT_VIRTUAL_PUSH_TOKEN", "WECHAT_VIRTUAL_AES_KEY") if not get_config(k)]


# ---------- 配置合成：渠道账户覆盖 > 全局回落 ----------

def _global_cfg() -> dict:
    from app.services import appsettings

    stored = appsettings.get_str("WECHAT_VIRTUAL_SANDBOX")
    # 未配置时默认沙箱（安全侧：联调环境误发生产回调比反之危害小）
    sandbox = (
        stored.strip().lower() in ("1", "true", "on", "yes")
        if stored
        else True
    )
    return {
        "offer_id": get_config("WECHAT_VIRTUAL_OFFER_ID"),
        "appkey": get_config("WECHAT_VIRTUAL_APPKEY"),
        "sandbox_appkey": get_config("WECHAT_VIRTUAL_SANDBOX_APPKEY"),
        "push_token": get_config("WECHAT_VIRTUAL_PUSH_TOKEN"),
        "aes_key": get_config("WECHAT_VIRTUAL_AES_KEY"),
        "sandbox": sandbox,
    }


def global_cfg() -> dict:
    """全局回落配置（后台「支付配置」/ .env）。"""
    return _global_cfg()


def account_cfg(acc: ChannelAccount | None) -> dict:
    """渠道账户生效配置：virtual_pay 覆盖全局回落（acc=None 时即全局）。"""
    cfg = _global_cfg()
    for k in VP_JSON_KEYS:
        v = (acc.virtual_pay or {}).get(k) if acc else None
        if v not in (None, ""):
            cfg[k] = v
    if acc and acc.virtual_pay and "sandbox" in acc.virtual_pay:
        cfg["sandbox"] = bool(acc.virtual_pay["sandbox"])
    return cfg


def effective_appkey(cfg: dict) -> str:
    return str(cfg.get("sandbox_appkey") or "") if cfg.get("sandbox") else str(cfg.get("appkey") or "")


def iter_push_configs(db) -> list[tuple[ChannelAccount, dict]]:
    """可参与回调匹配的账户：启用的 wechat_mp 且（合成后）具备 Token + AESKey。"""
    out = []
    accs = (
        db.query(ChannelAccount)
        .filter(ChannelAccount.enabled == True, ChannelAccount.type == "wechat_mp")  # noqa: E712
        .all()
    )
    for acc in accs:
        cfg = account_cfg(acc)
        if cfg.get("push_token") and cfg.get("aes_key"):
            out.append((acc, cfg))
    return out


def resolve_push_config(db, signature: str, timestamp: str, nonce: str, encrypt: str):
    """按 Token 逐账户验签，识别推送来自哪个小程序；返回 (账户, 配置) 或 None。"""
    for acc, cfg in iter_push_configs(db):
        if verify_msg_signature(signature, cfg["push_token"], timestamp, nonce, encrypt):
            return acc, cfg
    return None


# ---------- 下单签名 ----------

def create_prepay_params(order: Order, db) -> dict:
    """充值下单：构造 wx.requestVirtualPayment 所需参数（按订单归属渠道账户取配置）。

    buyQuantity 单位为「分」（TODO: 以官方文档为准核对单位与字段名）。
    """
    acc = db.get(ChannelAccount, order.channel_account_id) if order.channel_account_id else None
    cfg = account_cfg(acc)
    owner = acc.id if acc else "全局"
    offer_id = cfg.get("offer_id")
    appkey = effective_appkey(cfg)
    if not offer_id or not appkey:
        raise ValueError(
            f"渠道账户 {owner} 未配置虚拟支付（后台「渠道账号 → 支付配置」或「支付配置」页填写 OfferID/AppKey）"
        )
    params = {
        "offerId": offer_id,
        "env": "1" if cfg.get("sandbox") else "0",  # 1=沙箱 0=现网（TODO 核对）
        "platform": order.platform or "android",
        "currencyType": "CNY",
        "buyQuantity": int(round(float(order.amount) * 100)),
        "outTradeNo": order.id,
    }
    sign_data_str = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    sign = hmac.new(appkey.encode(), sign_data_str.encode(), hashlib.sha256).hexdigest()
    return {
        "provider": "wechat_virtual",
        "account_id": acc.id if acc else None,
        "mode": "short_series_goods_page",  # TODO: 按后台商品配置的发货模式确认
        "signData": sign_data_str,
        "sign": sign,
    }


# ---------- 消息推送加解密（回调验签 / 回包加密；按账户密钥参数化） ----------

def _aes_key(aes_key_str: str) -> bytes:
    """EncodingAESKey(43位) + '=' 还原出 AES-256 key。"""
    return base64.b64decode(aes_key_str + "=")


def msg_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """msg_signature = sha1(sort(token, timestamp, nonce, encrypt))。"""
    raw = "".join(sorted([token, timestamp, nonce, encrypt]))
    return hashlib.sha1(raw.encode()).hexdigest()


def verify_msg_signature(signature: str, token: str, timestamp: str, nonce: str, encrypt: str) -> bool:
    """回调验签：比对推送携带的 signature 与本地计算值（hmac.compare_digest 防时序）。"""
    return hmac.compare_digest(signature or "", msg_signature(token, timestamp, nonce, encrypt))


def decrypt_push(encrypt: str, aes_key_str: str) -> str:
    """解密推送报文：明文 = random(16) + msg_len(4, 网络序) + msg + receiveid。"""
    key = _aes_key(aes_key_str)
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    plain = cipher.decrypt(base64.b64decode(encrypt))
    msg = plain[16:]
    msg_len = struct.unpack(">I", msg[:4])[0]
    return msg[4 : 4 + msg_len].decode()


def encrypt_push_reply(plain: str, aes_key_str: str) -> str:
    """回包加密（同一套 AES-256-CBC），否则微信视为推送失败而重试。"""
    key = _aes_key(aes_key_str)
    msg = plain.encode()
    payload = os.urandom(16) + struct.pack(">I", len(msg)) + msg + key[:16]  # receiveid 用 key 前 16 位占位
    pad = 32 - (len(payload) % 32)
    payload += bytes([pad]) * pad
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    return base64.b64encode(cipher.encrypt(payload)).decode()


# ---------- 查单 / 退款（TODO: 联调时按官方 API 补全；需各小程序 access_token） ----------

async def query_order(acc: ChannelAccount, out_trade_no: str) -> dict:
    raise NotImplementedError("TODO: 接入虚拟支付查单 API")


async def refund(acc: ChannelAccount, out_trade_no: str, quantity: int) -> dict:
    raise NotImplementedError("TODO: 接入虚拟支付退款 API")
