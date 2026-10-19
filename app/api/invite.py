"""邀请有奖（C 端）。"""
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_openid
from app.core.timeutil import local_iso
from app.models import User
from app.services import invite as invite_svc
from app.services.auth import _wxa_access_token, account_creds_for
from app.services.billing import user_id_by_openid

router = APIRouter()


def _uid(db: Session, openid: str) -> int:
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    return user_id


def _inviter(db: Session, openid: str) -> User:
    """当前登录用户（懒生成邀请码后返回）。"""
    u = db.get(User, _uid(db, openid))
    invite_svc.ensure_code(db, u)
    return u


@router.post("/bind")
def bind(body: dict = Body(default={}), db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    """登录后绑定邀请关系（由前端携带的邀请码触发，幂等）。"""
    return invite_svc.bind(db, _uid(db, openid), body.get("code") or "")


@router.get("/me")
def me(db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    """我的邀请码与奖励概览。"""
    user_id = _uid(db, openid)
    u = db.get(User, user_id)
    invite_svc.ensure_code(db, u)
    return invite_svc.summary(db, user_id, u)


@router.get("/records")
def records(page: int = 1, size: int = 15, db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    """邀请记录（分页）。"""
    r = invite_svc.records(db, _uid(db, openid), page, size)
    for it in r["items"]:
        it["created_at"] = local_iso(it["created_at"])
        it["rewarded_at"] = local_iso(it["rewarded_at"])
    return r


@router.get("/qrcode")
def qrcode(
    env_version: str = Query("release"),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """带邀请码 scene 的小程序码（PNG 二进制）—— 邀请海报/扫码进入用。

    凭据取用户所属渠道账号；mock 或未配置凭据时返回 400（前端海报回落展示邀请码文本）。
    """
    u = _inviter(db, openid)
    appid, secret, mock = account_creds_for(db, openid)
    if mock or not appid:
        raise HTTPException(400, "当前渠道未配置微信凭据，无法生成小程序码")
    token = _wxa_access_token(appid, secret)
    resp = httpx.post(
        f"https://api.weixin.qq.com/wxa/getwxacodeunlimit?access_token={token}",
        json={
            "scene": u.invite_code or "",
            "page": "pages/index/index",
            "check_path": False,
            "env_version": env_version if env_version in ("release", "trial", "develop") else "release",
            "width": 430,
            "auto_color": False,
            "line_color": {"r": 76, "g": 29, "b": 149},
        },
        timeout=15,
    )
    body = resp.content
    # 微信默认返回 JPEG（\xff\xd8 开头）；按 content-type 判断是否为图片
    if not resp.headers.get("content-type", "").startswith("image/"):
        try:
            err = resp.json()
        except Exception:  # noqa: BLE001
            err = {"errmsg": resp.text[:200]}
        raise HTTPException(400, f"小程序码生成失败: {err.get('errmsg') or err}")
    return Response(content=body, media_type=resp.headers.get("content-type", "image/jpeg"))


@router.post("/link")
def link(body: dict = Body(default={}), db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    """生成 URL Link（短信/邮件/外部浏览器拉起小程序）。

    需小程序已发布上线；body.env_version: release(默认) / trial / develop——
    体验版/开发版链接仅同一主体可打开。
    """
    u = _inviter(db, openid)
    appid, secret, mock = account_creds_for(db, openid)
    if mock or not appid:
        raise HTTPException(400, "当前渠道未配置微信凭据，无法生成链接")
    env = body.get("env_version") or "release"
    env = env if env in ("release", "trial", "develop") else "release"
    token = _wxa_access_token(appid, secret)
    resp = httpx.post(
        f"https://api.weixin.qq.com/wxa/generate_urllink?access_token={token}",
        json={
            "path": "pages/index/index",
            "query": "invite=" + (u.invite_code or ""),
            "env_version": env,
        },
        timeout=15,
    )
    data = resp.json()
    if "url_link" not in data:
        # 85407：无 scheme/link 权限（需小程序已发布且完成认证）
        if data.get("errcode") == 85407:
            raise HTTPException(400, "当前小程序暂无 URL Link 权限（需已发布并完成认证）")
        raise HTTPException(400, "链接生成失败: " + str(data.get("errmsg") or data))
    return {"url": data["url_link"]}
