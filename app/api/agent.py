from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import get_current_openid
from app.core.timeutil import local_iso, local_str_to_utc, utcnow
from app.models import SessionTurn, Tool, ToolInvocation, ToolSession, User
from app.services.agent.router import recognize
from app.services.auth import account_id_for
from app.services.attachment import presign_get_url
from app.services.billing import (
    consume,
    effective_price_quota,
    get_account,
    quota_remain,
    user_id_by_openid,
    user_unit_price,
)
from app.services.content_security import check_input, ContentRejected
from app.services.ratelimit import allow
from app.worker.queue import enqueue

router = APIRouter()

# 生成文档的 MIME 映射。容器精简镜像没有 /etc/mime.types，
# 标准库 mimetypes 无法识别 .docx/.pptx，会退化成 octet-stream，
# 导致小程序侧无法判断文件类型，故这里显式声明。
_DOC_MIME = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def _result_filename(inv: ToolInvocation) -> str | None:
    """从 result_ref 取原始文件名（含扩展名）。"""
    ref = inv.result_ref
    if not ref:
        return None
    return str(ref).split("?")[0].rstrip("/").rsplit("/", 1)[-1] or None


def _doc_media(filename: str) -> str:
    import os

    ext = os.path.splitext(filename or "")[1].lower()
    return _DOC_MIME.get(ext, "application/octet-stream")


def _client_ip(request: Request) -> str:
    # Nginx 前置时取真实 IP
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _download_url(inv: ToolInvocation) -> str | None:
    """生成文档交付地址。

    FILE_DELIVERY=proxy（默认）：返回后端代理路径，前端拼 baseUrl 并带 token 下载，
      不依赖公网可达的对象存储域名（小程序 downloadFile 合法域名只需 API 域名）。
    FILE_DELIVERY=presign：返回 S3 预签名直链（需 S3_PUBLIC_ENDPOINT 对手机可达）。
    历史数据若存的是完整 URL 则原样返回。
    """
    ref = inv.result_ref
    if not ref:
        return None
    if ref.startswith("http"):
        return ref
    from app.core.config import settings

    if settings.FILE_DELIVERY == "presign":
        return presign_get_url(ref)
    # URL 末尾带上真实文件名，确保 wx.downloadFile 的临时文件保留扩展名，
    # wx.openDocument 才能识别格式。
    name = _result_filename(inv) or f"{inv.id}.docx"
    return f"/api/agent/invocation/{inv.id}/file/{name}"


@router.get("/invocation/{inv_id}/file/{filename}")
@router.get("/invocation/{inv_id}/file")
def api_invocation_file(
    inv_id: int,
    filename: str | None = None,
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """代理下载生成文档（校验归属，直接回源对象存储）。

    URL 可带 /{filename} 以便小程序临时文件保留扩展名；不带时按 result_ref 推断。
    """
    from urllib.parse import quote

    from fastapi import Response

    from app.services.attachment import get_object_bytes

    user_id = user_id_by_openid(db, openid)
    inv = (
        db.query(ToolInvocation)
        .filter(ToolInvocation.id == inv_id, ToolInvocation.user_id == user_id)
        .first()
    )
    if not inv or not inv.result_ref or str(inv.result_ref).startswith("http"):
        raise HTTPException(404, "文件不存在")
    data = get_object_bytes(inv.result_ref)
    name = _result_filename(inv) or filename or f"{inv_id}.docx"
    return Response(
        content=data,
        media_type=_doc_media(name),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.post("/recognize")
def api_recognize(
    body: dict = Body(default={}),
    request: Request = None,
    db: Session = Depends(get_db),
):
    # 匿名可调 -> 按 IP 限流，防 classifier LLM 被刷（阈值后台动态可调）
    from app.services import appsettings

    if not allow(
        f"recognize:{_client_ip(request)}",
        limit=appsettings.get_num("rl_recognize_per_min", settings.RATE_LIMIT_PER_MIN),
        category="recognize",
    ):
        raise HTTPException(429, "too many requests")
    # 全站聚合限流：所有用户共享配额（动态阈值 rl_global_recognize_per_min，默认 300/min）
    if not allow("global:recognize", limit=appsettings.get_num("rl_global_recognize_per_min", 300), category="global_recognize"):
        raise HTTPException(429, "当前使用人数过多，请稍后再试")
    text = body.get("text", "")
    if not text:
        raise HTTPException(400, "text required")
    try:
        check_input(text, db=db)
    except ContentRejected:
        raise HTTPException(400, "content rejected")
    # 多渠道矩阵：按小程序 appid 过滤意图识别候选池（未带 appid = 全量）
    appid = (body.get("appid") or "").strip()
    account_id = None
    if appid:
        from app.models import ChannelAccount

        acc = (
            db.query(ChannelAccount)
            .filter(ChannelAccount.appid == appid, ChannelAccount.enabled == True)  # noqa: E712
            .first()
        )
        account_id = acc.id if acc else None
    return recognize(db, text, channel=(body.get("channel") or "wechat").lower(), account_id=account_id)


@router.post("/confirm")
def api_confirm(
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    if not allow(f"confirm:{openid}"):
        raise HTTPException(429, "too many requests")
    u = db.get(User, user_id)
    if u and u.status == "ban":
        raise HTTPException(403, "account banned")

    tool_id = body.get("tool_id")
    text = body.get("input") or body.get("text", "")
    attachment_ids = body.get("attachment_ids") or []

    tool = db.query(Tool).filter(Tool.id == tool_id, Tool.enabled == True).first()
    if not tool:
        raise HTTPException(404, "tool not found")

    try:
        check_input(text or "", db=db, user_id=user_id)
    except ContentRejected:
        raise HTTPException(400, "content rejected")

    # 需附件的工具必须先上传附件
    if tool.needs_attachment and not attachment_ids:
        raise HTTPException(400, "attachment required")

    # 按渠道生效定价（§9.5）：channel_pricing 覆盖默认 price_quota
    channel = (body.get("channel") or "wechat").lower()
    price = effective_price_quota(tool, channel)
    # 扣费快照：该用户当前单次均价（元/次）
    unit_price = user_unit_price(db, user_id)

    acc_id = account_id_for(db, openid)
    # 原子扣次
    try:
        consume(db, user_id, price, None, account_id=acc_id)
    except ValueError:
        raise HTTPException(402, "insufficient_quota")

    inv = ToolInvocation(
        user_id=user_id,
        tool_id=tool.id,
        tool_version=tool.version,
        channel_account_id=acc_id,
        input_text=text,
        attachment_ids=attachment_ids,
        intent_confidence=1.0,
        status="pending",
        quota_charged=price,
        # 扣费快照：写死本次单价与应付金额，保证日后调价/活动不影响历史对账
        unit_price_snapshot=round(unit_price, 4),
        amount_charged=round(price * unit_price, 2),
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)

    # 全量异步：LLM 调用不占请求线程，统一入队 + 前端轮询
    enqueue({"type": "tool_async", "invocation_id": inv.id})
    return {"invocation_id": inv.id, "async": True, "status": "pending"}


@router.post("/records/{inv_id}/archive")
def api_archive_record(
    inv_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """归档 / 取消归档一条记录（body: {"archived": true|false}）。"""
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    inv = (
        db.query(ToolInvocation)
        .filter(ToolInvocation.id == inv_id, ToolInvocation.user_id == user_id)
        .first()
    )
    if not inv:
        raise HTTPException(404, "记录不存在")
    inv.archived = bool(body.get("archived", True))
    inv.archived_at = utcnow() if inv.archived else None
    db.commit()
    return {"ok": True, "invocation_id": inv.id, "archived": inv.archived}


@router.get("/invocation/{inv_id}")
def api_invocation(inv_id: int, db: Session = Depends(get_db), openid: str = Depends(get_current_openid)):
    user_id = user_id_by_openid(db, openid)
    inv = db.query(ToolInvocation).filter(ToolInvocation.id == inv_id, ToolInvocation.user_id == user_id).first()
    if not inv:
        raise HTTPException(404, "not found")
    acct = get_account(db, user_id)
    return {
        "invocation_id": inv.id,
        "status": inv.status,
        "result_text": inv.result_text,
        "result_ref": inv.result_ref,
        "filename": _result_filename(inv),
        "download_url": _download_url(inv),
        "model_used": inv.model_used,
        "cost_amount": float(inv.cost_amount or 0),
        "err": inv.err,
        "quota_remain": quota_remain(db, user_id),
    }


@router.get("/records")
def api_records(
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    archived: str = "unarchived",
    page: int = 1,
    size: int = 15,
    db: Session = Depends(get_db),
    openid: str = Depends(get_current_openid),
):
    """记录列表（服务端分页）。

    - `archived`：``（不限）/ `archived`（已归档）/ `unarchived`（未归档，默认）
    - 关键字 q：匹配工具名或输入摘要
    - 时间范围：date_from / date_to（本地日期，date_to 含当日）
    """
    user_id = user_id_by_openid(db, openid)
    if not user_id:
        raise HTTPException(401)
    size = min(max(size, 1), 50)
    page = max(page, 1)
    offset = (page - 1) * size

    query = db.query(ToolInvocation).filter(ToolInvocation.user_id == user_id)
    if archived == "archived":
        query = query.filter(ToolInvocation.archived == True)  # noqa: E712
    elif archived == "unarchived":
        query = query.filter(ToolInvocation.archived == False)  # noqa: E712
    if q.strip():
        kw = f"%{q.strip()}%"
        query = query.outerjoin(Tool, Tool.id == ToolInvocation.tool_id).filter(
            or_(ToolInvocation.input_text.like(kw), Tool.name.like(kw))
        )
    since = local_str_to_utc(date_from) if date_from else None
    until = local_str_to_utc(date_to) if date_to else None
    if until and len(str(date_to).strip()) == 10:
        until = until + timedelta(days=1)  # 结束日期含当日
    if since:
        query = query.filter(ToolInvocation.created_at >= since)
    if until:
        query = query.filter(ToolInvocation.created_at <= until)

    total = query.count()
    rows = query.order_by(ToolInvocation.id.desc()).offset(offset).limit(size).all()

    tids = {r.tool_id for r in rows if r.tool_id}
    tmap = {t.id: t.name for t in db.query(Tool).filter(Tool.id.in_(tids)).all()} if tids else {}
    # 会话型调用（可「继续」）→ 带上 session_id 与所在子任务序号
    # 多工具会话下，一条调用记录属于会话里的某个子任务，映射从 session_turn 取。
    inv_ids = [r.id for r in rows]
    sess_map: dict = {}
    if inv_ids:
        for t in (
            db.query(SessionTurn)
            .filter(SessionTurn.invocation_id.in_(inv_ids))
            .all()
        ):
            sess_map[t.invocation_id] = (t.session_id, t.seq)
    # 会话状态（前端据此决定该条记录能否「继续」）
    sess_status: dict = {}
    sids = {v[0] for v in sess_map.values() if v and v[0]}
    if sids:
        sess_status = {
            s.id: s.status for s in db.query(ToolSession).filter(ToolSession.id.in_(sids)).all()
        }

    return {
        "items": [
            {
                "invocation_id": r.id,
                "session_id": sess_map.get(r.id, (None, None))[0],
                "turn_seq": sess_map.get(r.id, (None, None))[1],
                "session_status": sess_status.get(sess_map.get(r.id, (None, None))[0], ""),
                "tool_id": r.tool_id,
                "tool_name": tmap.get(r.tool_id) or r.tool_id,
                "status": r.status,
                "quota_charged": int(r.quota_charged or 0),
                "input_text": (r.input_text or "")[:120],
                "created_at": local_iso(r.created_at),
                "archived": bool(r.archived),
            }
            for r in rows
        ],
        "total": int(total),
        "page": page,
        "size": size,
        "has_more": offset + len(rows) < int(total),
    }
