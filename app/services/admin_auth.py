"""后台管理员账号：密码哈希（pbkdf2，标准库）+ 登录会话令牌。"""
import hashlib
import hmac
import os
import time
from typing import Optional

import jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AdminUser, utcnow

ITERATIONS = 120_000
TOKEN_TTL = 60 * 60 * 12  # 12 小时


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """pbkdf2_sha256$迭代次数$盐$哈希（标准库实现，无第三方依赖）。"""
    salt = salt or os.urandom(12).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    # 兼容手写的明文历史数据（登录成功后应尽快改密）
    if not stored.startswith("pbkdf2_sha256$"):
        return hmac.compare_digest(password, stored)
    try:
        _, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:  # noqa: BLE001
        return False


def create_admin_token(admin: AdminUser) -> str:
    payload = {
        "adm": True,
        "sub": str(admin.id),  # PyJWT >=2.10 要求 sub 为字符串
        "username": admin.username,
        "role": admin.role or "operator",
        "exp": int(time.time()) + TOKEN_TTL,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGO)


def parse_admin_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGO])
    except Exception:  # noqa: BLE001
        return None
    return payload if payload.get("adm") else None


def login(db: Session, username: str, password: str) -> dict:
    admin = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not admin or not verify_password(password, admin.password_hash or ""):
        raise ValueError("用户名或密码错误")
    if admin.status == "disabled":
        raise ValueError("账号已停用")
    admin.last_login_at = utcnow()
    db.commit()
    return {
        "token": create_admin_token(admin),
        "id": admin.id,
        "username": admin.username,
        "name": admin.name or admin.username,
        "role": admin.role or "operator",
    }
