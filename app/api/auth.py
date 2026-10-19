from fastapi import APIRouter, Body, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_openid
from app.services.auth import (
    KNOWN_CHANNELS,
    avatar_key,
    bind_phone,
    login,
    save_avatar,
    update_profile,
)

router = APIRouter()

# 头像：允许的图片类型与上限
_AVATAR_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_AVATAR_MAX = 2 * 1024 * 1024
_AVATAR_MIME = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


@router.post("/login")
def do_login(body: dict = Body(default={}), db: Session = Depends(get_db)):
    channel = (body.get("channel") or "wechat").lower()
    code = body.get("code", "mock")
    try:
        return login(db, channel, code, appid=body.get("appid") or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/profile")
def do_update_profile(
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """更新昵称（微信「昵称填写」能力拿到后调用）。"""
    try:
        return update_profile(db, openid, nickname=body.get("nickname"))
    except ValueError as e:
        raise HTTPException(401, str(e))


@router.post("/avatar")
async def do_upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """上传头像（微信 chooseAvatar 的临时文件），存对象存储后返回代理路径。"""
    ext = _AVATAR_EXT.get((file.content_type or "").lower())
    if not ext:
        raise HTTPException(400, "头像仅支持 jpg / png / webp")
    data = await file.read()
    if len(data) > _AVATAR_MAX:
        raise HTTPException(413, "头像不能超过 2MB")
    try:
        return save_avatar(db, openid, data, ext)
    except ValueError as e:
        raise HTTPException(401, str(e))


@router.get("/avatar/{uid}")
def get_avatar(uid: int, db: Session = Depends(get_db)):
    """头像图片（公开可读；`<image>` 需要直接可访问的图片 URL）。"""
    key = avatar_key(db, uid)
    if not key:
        raise HTTPException(404, "no avatar")
    from app.services.attachment import get_object_bytes

    ext = "." + key.rsplit(".", 1)[-1].lower() if "." in key else ""
    data = get_object_bytes(key)
    return Response(content=data, media_type=_AVATAR_MIME.get(ext, "image/jpeg"))


@router.post("/phone")
def do_bind_phone(
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """绑定手机号：前端 getPhoneNumber 回调 code 换取手机号。"""
    code = body.get("code", "")
    if not code:
        raise HTTPException(400, "missing code")
    try:
        return bind_phone(db, openid, code)
    except ValueError as e:
        raise HTTPException(401, str(e))
    except RuntimeError:
        raise HTTPException(400, "手机号获取失败")
