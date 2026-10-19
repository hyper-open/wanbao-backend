import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import get_current_openid
from app.services.attachment import save_upload
from app.services.billing import user_id_by_openid

router = APIRouter()

ALLOWED = {m.strip() for m in settings.ATTACHMENT_ALLOWED_MIME.split(",") if m.strip()}
EXT_MAP = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
SUPPORTED_HINT = ".docx / .pdf / .pptx / .txt / .md / 图片(jpg/png/webp)"


@router.post("/upload")
async def upload(
    files: list[UploadFile] = File(...),
    filename: str = Form(None),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)

    if not files:
        raise HTTPException(400, "no file")

    # 后端硬校验：单文件 <=10MB、格式受限（前端 UI 仅单文件，后端支持多文件）
    max_bytes = settings.ATTACHMENT_MAX_BYTES
    ids = []
    for f in files:
        data = await f.read()
        # 小程序临时路径常不带扩展名，原始文件名由 formData.filename 传入
        name = (filename if len(files) == 1 else None) or f.filename or "file"
        if len(data) > max_bytes:
            raise HTTPException(413, f"{name} 超过 {max_bytes//1024//1024}MB 上限，请压缩后再上传")
        ext = os.path.splitext(name)[1].lower()
        mime = f.content_type or EXT_MAP.get(ext, "")
        if mime not in ALLOWED and ext not in EXT_MAP:
            if ext == ".doc":
                raise HTTPException(400, "暂不支持 .doc 格式，请在 Word / WPS 中另存为 .docx 或 .pdf 后再上传")
            raise HTTPException(
                400,
                f"不支持 {ext or name} 格式，仅支持 {SUPPORTED_HINT}",
            )
        att = save_upload(db, user_id, name, data, mime)
        ids.append(att.id)

    return {"attachment_ids": ids, "count": len(ids)}
