import uuid

import httpx
from sqlalchemy.orm import Session

from app.core.security import create_token
from app.models import User, Account, UserIdentity

KNOWN_CHANNELS = ("wechat", "alipay")
# 渠道中文名（后台充值档位「可售渠道」等处展示）
CHANNEL_LABELS = {"wechat": "微信", "alipay": "支付宝"}


def _account_for(db: Session, channel: str, appid: str | None = None):
    """按 appid 找渠道账号（多渠道）；找不到时回落默认账号 / 环境变量。"""
    from app.models import ChannelAccount

    if channel != "wechat":
        return None
    if appid:
        acc = (
            db.query(ChannelAccount)
            .filter(ChannelAccount.appid == appid, ChannelAccount.enabled == True)  # noqa: E712
            .first()
        )
        if acc:
            return acc
    return (
        db.query(ChannelAccount)
        .filter(ChannelAccount.type == "wechat_mp", ChannelAccount.id == "default")
        .first()
        or db.query(ChannelAccount).filter(ChannelAccount.type == "wechat_mp").first()
    )


def account_id_for(db: Session, channel_uid: str) -> str | None:
    """该用户标识来自哪个渠道账号（订单/调用/会话归属用）。"""
    from app.models import UserIdentity

    row = (
        db.query(UserIdentity.channel_account_id)
        .filter(UserIdentity.channel_uid == channel_uid)
        .first()
    )
    return (row[0] if row else None) or None




def _wechat_uid(code: str, acc=None) -> str:
    """微信 jscode2session 换 openid；无凭证时走 mock。

    凭证优先取渠道账号（多渠道），其次环境变量。失败一律抛 ValueError（由 API 层映射为 400）：
    code 是一次性凭证且 5 分钟过期，重试/过期很常见，不应作为服务端错误（500）返回。
    """
    appid = (acc.appid if acc else None) or ""
    secret = (acc.secret if acc else None) or ""
    if ((acc.mock if acc else False) or not appid):
        return "mock_openid_" + uuid.uuid4().hex[:8]
    try:
        resp = httpx.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={
                "appid": appid,
                "secret": secret,
                "js_code": code,
                "grant_type": "authorization_code",
            },
            timeout=5,
        )
        data = resp.json()
    except Exception as e:  # noqa: BLE001  网络/解析异常统一转成可重试错误
        raise ValueError("微信登录服务暂时不可用，请重试") from e
    openid = data.get("openid")
    if not openid:
        raise ValueError(
            f"微信登录失败（{data.get('errcode')}: {data.get('errmsg') or '未知错误'}），请重试"
        )
    return openid


def _channel_uid(channel: str, code: str, acc=None) -> str:
    """渠道适配器：按渠道换取平台用户标识。新渠道在此接入真实 OAuth。"""
    if channel == "wechat":
        return _wechat_uid(code, acc)
    # 其他渠道 mock（支付宝等真实接入时替换为对应 OAuth 换取逻辑）
    return f"mock_{channel}_" + uuid.uuid4().hex[:8]


def login(db: Session, channel: str, code: str, appid: str | None = None) -> dict:
    """统一登录：渠道身份 -> 统一账户 -> 发渠道无关 JWT（§9.5 多渠道）。

    `appid`：小程序端上报自己的 appid（wx.getAccountInfoSync），后端据此选用对应渠道账号的
    凭证换 openid —— 多个小程序各自独立，openid 不会串。
    """
    channel = (channel or "wechat").lower()
    if channel not in KNOWN_CHANNELS:
        raise ValueError(f"unknown channel: {channel}")
    acc = _account_for(db, channel, appid)
    uid = _channel_uid(channel, code, acc)

    ident = db.query(UserIdentity).filter_by(channel=channel, channel_uid=uid).first()
    if ident:
        user = db.get(User, ident.user_id)
        if acc and not ident.channel_account_id:
            ident.channel_account_id = acc.id
            db.commit()
    else:
        # 微信渠道继续写 user.openid 以兼容 legacy 查询；其他渠道 openid 置空
        user = User(openid=uid if channel == "wechat" else None)
        db.add(user)
        db.commit()
        db.refresh(user)
        db.add(Account(user_id=user.id))
        ident = UserIdentity(
            user_id=user.id, channel=channel, channel_uid=uid,
            channel_account_id=(acc.id if acc else None),
        )
        db.add(ident)
        db.commit()

    return {
        "token": create_token(uid, channel=channel, user_id=user.id),
        "openid": uid,          # 兼容字段：现为主渠道用户标识
        "user_id": user.id,
        "channel": channel,
        "account_id": (acc.id if acc else None),
        "phone_bound": bool(user.phone),
    }


# ---------- 手机号绑定（微信手机号快速验证组件） ----------

_access_token_cache: dict = {}   # appid -> {"token","expire"}（多渠道各自缓存）


def _wxa_access_token(appid: str | None = None, secret: str | None = None) -> str:
    """小程序接口调用凭证（cgi-bin/token）。**按 appid 分别缓存**，提前 60s 过期。

    多渠道下每个应用有独立的 access_token，必须用对应渠道账号的 appid/secret 获取。
    """
    import time

    appid = appid or ""
    secret = secret or ""
    if not appid:
        raise RuntimeError("该渠道未配置微信凭据（channel_account.appid/secret），无法获取 access_token")
    now = int(time.time())
    c = _access_token_cache.get(appid)
    if c and c.get("token") and c.get("expire", 0) > now + 60:
        return c["token"]
    resp = httpx.post(
        "https://api.weixin.qq.com/cgi-bin/stable_token",
        json={"grant_type": "client_credential", "appid": appid, "secret": secret},
        timeout=5,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError("get access_token failed: " + resp.text)
    _access_token_cache[appid] = {
        "token": data["access_token"],
        "expire": now + int(data.get("expires_in", 7200)),
    }
    return data["access_token"]


def account_creds_for(db: Session, channel_uid: str | None) -> tuple:
    """该用户标识所属渠道账号的 (appid, secret, mock)；无匹配渠道时 ("","",False)。"""
    from app.models import ChannelAccount, UserIdentity

    appid = secret = ""
    if channel_uid:
        row = (
            db.query(UserIdentity.channel_account_id)
            .filter(UserIdentity.channel_uid == channel_uid)
            .first()
        )
        if row and row[0]:
            acc = db.get(ChannelAccount, row[0])
            if acc:
                appid, secret = (acc.appid or ""), (acc.secret or "")
    return (appid, secret, bool(acc.mock) if acc else False)


def phone_by_code(code: str, appid: str | None = None, secret: str | None = None, mock: bool = False) -> str:
    """getPhoneNumber 回调 code -> 手机号（凭证取该用户所属渠道账号）。"""
    if mock or not appid:
        return "13800138000"
    resp = httpx.post(
        "https://api.weixin.qq.com/wxa/business/getuserphonenumber",
        params={"access_token": _wxa_access_token(appid, secret)},
        json={"code": code},
        timeout=5,
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError("getuserphonenumber failed: " + resp.text)
    return data["phone_info"]["purePhoneNumber"]


def _user_by_channel_uid(db: Session, channel: str, uid: str) -> User | None:
    ident = db.query(UserIdentity).filter_by(channel=channel, channel_uid=uid).first()
    return db.get(User, ident.user_id) if ident else None


def update_profile(db: Session, openid: str, nickname: str | None = None) -> dict:
    """更新昵称（头像走 avatars 上传接口）。"""
    user = _user_by_channel_uid(db, "wechat", openid)
    if not user:
        raise ValueError("user not found")
    if nickname is not None:
        user.nickname = (nickname or "").strip()[:64]
    db.commit()
    return {"nickname": user.nickname or "", "avatar": f"/api/auth/avatar/{user.id}" if user.avatar else ""}


def save_avatar(db: Session, openid: str, data: bytes, ext: str) -> dict:
    """保存头像到对象存储，返回可访问的代理路径。"""
    from app.services.attachment import upload

    user = _user_by_channel_uid(db, "wechat", openid)
    if not user:
        raise ValueError("user not found")
    key = f"avatar/{user.id}/{uuid.uuid4().hex[:12]}{ext}"
    upload(key, data)
    user.avatar = key
    db.commit()
    return {"avatar": f"/api/auth/avatar/{user.id}"}


def avatar_key(db: Session, user_id: int) -> str | None:
    user = db.get(User, user_id)
    return user.avatar if user else None


def bind_phone(db: Session, openid: str, code: str) -> dict:
    """绑定手机号到当前用户，返回脱敏号码。"""
    user = (
        db.query(User)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .filter(UserIdentity.channel == "wechat", UserIdentity.channel_uid == openid)
        .first()
    )
    if not user:
        raise ValueError("user not found")

    appid, secret, mock = account_creds_for(db, openid)
    phone = phone_by_code(code, appid, secret, mock)
    user.phone = phone
    user.phone_verified = True
    db.commit()
    return {"phone": phone[:3] + "****" + phone[7:]}
