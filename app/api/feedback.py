"""用户反馈/举报（监管要求提供投诉举报渠道，§16.3-C）。"""
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_openid
from app.core.timeutil import local_iso
from app.models import Feedback
from app.services.billing import user_id_by_openid
from app.services.content_security import check_input, ContentRejected
from app.services.ratelimit import allow

router = APIRouter()


@router.get("/mine")
def mine(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """我提交过的反馈/举报（分页，新的在前）；只返回自己的记录。"""
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    q = db.query(Feedback).filter(Feedback.user_id == user_id)
    total = q.count()
    rows = q.order_by(Feedback.id.desc()).offset((page - 1) * size).limit(size).all()

    # 关联产出：反馈若挂在某次工具调用上，带上工具名，便于用户看懂"针对哪次产出"
    inv_ids = [r.invocation_id for r in rows if r.invocation_id]
    tool_names: dict = {}
    if inv_ids:
        from app.models import Tool, ToolInvocation

        for inv in db.query(ToolInvocation).filter(ToolInvocation.id.in_(inv_ids)).all():
            nm = db.get(Tool, inv.tool_id)
            tool_names[inv.id] = (nm.name if nm else "") or inv.tool_id or ""

    return {
        "items": [
            {
                "id": r.id,
                "type": r.type,          # report(举报) / suggestion(建议)
                "content": r.content,
                "status": r.status,      # open(待处理) / processed(已处理) / ignored(已忽略)
                "invocation_id": r.invocation_id,
                "invocation_tool": tool_names.get(r.invocation_id, ""),
                # 管理员回复（可能为空）
                "reply": r.reply or "",
                "replied_at": local_iso(r.replied_at),
                "created_at": local_iso(r.created_at),
            }
            for r in rows
        ],
        "total": int(total),
        "page": page,
        "size": size,
        "has_more": page * size < int(total),
    }


@router.post("")
def submit(
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    if not allow(f"feedback:{openid}", limit=10):
        raise HTTPException(429, "too many requests")

    ftype = body.get("type")
    content = (body.get("content") or "").strip()
    if ftype not in ("report", "suggestion"):
        raise HTTPException(400, "type must be report|suggestion")
    if not content or len(content) > 1000:
        raise HTTPException(400, "content required (<=1000 chars)")

    # 反馈内容同样过内容安全（它是用户输入）
    try:
        check_input(content, db=db, user_id=user_id)
    except ContentRejected:
        raise HTTPException(400, "content rejected")

    fb = Feedback(
        user_id=user_id,
        invocation_id=body.get("invocation_id"),
        type=ftype,
        content=content,
        status="open",
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return {"id": fb.id, "status": "received"}
