import time
import jwt
from typing import Optional
from fastapi import Header, HTTPException
from app.core.config import settings


def create_token(openid: str, channel: str = "wechat", user_id=None) -> str:
    payload = {"openid": openid, "channel": channel, "exp": int(time.time()) + 60 * 60 * 24 * 30}
    if user_id is not None:
        payload["user_id"] = user_id
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGO)


def verify_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGO])
    except Exception:  # noqa: BLE001
        return None


def get_current_openid(authorization: Optional[str] = Header(None)) -> str:
    """返回 token 中的主渠道用户标识（openid claim = channel_uid）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    payload = verify_token(authorization[7:])
    if not payload or not payload.get("openid"):
        raise HTTPException(status_code=401, detail="invalid token")
    return payload["openid"]
