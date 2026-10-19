"""C 端工具发现（§16.1）：类目 / 列表 / 搜索 / 详情。

纯文本检索（name/description/tags LIKE）；工具上百后切 embedding 语义检索（复用 §7.3 embedding 槽位）。
多渠道矩阵：按请求渠道账户过滤可见工具（Tool.channel_ids 空=全渠道）。
渠道来源：优先 X-App-Id 头（小程序 appid），回落登录 token 的用户身份。
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import verify_token
from app.models import CATEGORY_NAMES, ChannelAccount, Tool, ToolCategory
from app.services.auth import account_id_for

router = APIRouter()


def _cat_names(db: Session) -> dict:
    """分类字典 key -> 展示名（后台「工具分类」维护；缺历史值时回落默认枚举）。"""
    out = dict(CATEGORY_NAMES)
    for c in db.query(ToolCategory).all():
        out[c.key] = c.name
    return out


_DISCLAIMER_FLAG = {"v": True, "at": 0.0}


def _disclaimer_enabled() -> bool:
    """免责横幅总开关（后台可配；进程内 30s 缓存，避免列表页逐行查库）。"""
    import time as _t

    if _t.time() - _DISCLAIMER_FLAG["at"] > 30:
        from app.services import appsettings

        _DISCLAIMER_FLAG["v"] = appsettings.get_flag(appsettings.DISCLAIMER_ENABLED, default=True)
        _DISCLAIMER_FLAG["at"] = _t.time()
    return _DISCLAIMER_FLAG["v"]


def channel_account_of(db: Session, x_app_id: str, authorization: Optional[str]) -> ChannelAccount | None:
    """请求归属渠道账户：优先 X-App-Id 头（appid），回落登录 token 里的用户身份。"""
    if x_app_id:
        acc = (
            db.query(ChannelAccount)
            .filter(ChannelAccount.appid == x_app_id, ChannelAccount.enabled == True)  # noqa: E712
            .first()
        )
        if acc:
            return acc
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        uid = (payload or {}).get("openid")
        if uid:
            aid = account_id_for(db, uid)
            if aid:
                return db.get(ChannelAccount, aid)
    return None


def _channel_visible(q, account_id: str | None):
    """渠道可见性过滤：channel_ids 为空/NULL = 全渠道；非空 = 仅列表内账户。"""
    if not account_id:
        return q
    return q.filter(
        or_(
            Tool.channel_ids.is_(None),
            func.json_length(Tool.channel_ids) == 0,
            func.json_contains(Tool.channel_ids, json.dumps(account_id)),
        )
    )


def _tool_visible(t: Tool, account_id: str | None) -> bool:
    if not account_id:
        return True
    ids = t.channel_ids or []
    return len(ids) == 0 or account_id in ids


def _card(t: Tool, cat_names: dict | None = None) -> dict:
    names = cat_names or CATEGORY_NAMES
    generic_disclaimer = (
        "本工具结果由 AI 自动生成，可能存在错误或遗漏，仅供您参考；"
        "请自行核实后再采用，重要事项请咨询相关领域专业人士。"
    )
    if not _disclaimer_enabled():
        generic_disclaimer = None
        special = None
    else:
        special = t.disclaimer
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "disclaimer": special,                           # 专项免责（法律/医疗等，横幅强提示）
        "disclaimer_generic": generic_disclaimer,        # 通用免责兜底（无专项时的常规提示）
        "category": t.category or "other",
        "category_name": names.get(t.category, t.category or "其他"),
        "price_quota": t.price_quota,
        "needs_attachment": bool(t.needs_attachment),
        "is_hot": bool(t.is_hot),
        "usage_count": t.usage_count or 0,
        "tags": t.tags or [],
        # 交互式多轮工具：前端应进入会话页而非一次性 confirm
        "interactive": bool(t.interactive),
    }


def _detail(t: Tool, cat_names: dict | None = None) -> dict:
    d = _card(t, cat_names)
    schema = t.input_schema or {}
    if isinstance(schema, str):  # 防御：历史数据可能双重编码为 JSON 字符串
        try:
            schema = json.loads(schema)
        except ValueError:
            schema = {}
    d["attachment_spec"] = schema.get("attachment") or {}
    d["text_fields"] = schema.get("text_fields") or []
    return d


@router.get("/categories")
def categories(
    db: Session = Depends(get_db),
    x_app_id: str = Header(""),
    authorization: Optional[str] = Header(None),
):
    """类目：后台「工具分类」字典按 sort 升序返回（数量不再展示）。

    只返回当前渠道下有启用工具的分类；字典缺历史值时回落展示。"""
    account = channel_account_of(db, x_app_id, authorization)
    aid = account.id if account else None
    tool_cats = {
        (c or "other"): n
        for c, n in _channel_visible(
            db.query(Tool.category, func.count(Tool.id)).filter(Tool.enabled == True),  # noqa: E712
            aid,
        )
        .group_by(Tool.category)
        .all()
    }
    rows = (
        db.query(ToolCategory)
        .filter(ToolCategory.enabled == True)  # noqa: E712
        .order_by(ToolCategory.sort.asc())
        .all()
    )
    out, known = [], set()
    for c in rows:
        known.add(c.key)
        if tool_cats.get(c.key, 0) > 0:
            out.append({"category": c.key, "name": c.name})
    for c, n in tool_cats.items():
        if n > 0 and c not in known:
            out.append({"category": c, "name": CATEGORY_NAMES.get(c, c or "其他")})
    return out


@router.get("/search")
def search(
    q: str = "",
    db: Session = Depends(get_db),
    x_app_id: str = Header(""),
    authorization: Optional[str] = Header(None),
):
    q = (q or "").strip()
    if not q:
        return []
    account = channel_account_of(db, x_app_id, authorization)
    aid = account.id if account else None
    tools = _channel_visible(
        db.query(Tool).filter(Tool.enabled == True),  # noqa: E712
        aid,
    ).all()
    if not tools:
        return []
    cat_names = _cat_names(db)

    def _text(t: Tool) -> str:
        return f"{t.name} {t.description or ''} {' '.join(t.tags or [])}"

    # 语义排序（§16.1-D3）：query 向量 vs 工具向量缓存余弦 + 关键词命中加分；异常回落 LIKE
    # 精确命中（名称含完整关键词）置顶第一梯队；其余走动态阈值——低于「最高分-0.2」或绝对分
    # 不足 0.3 的丢弃，避免语义近似结果凑满 20 条的尾部噪音
    try:
        from app.services.embedding import cosine, get_tool_vectors, openai_embed

        vecs = get_tool_vectors(db, tools)  # 缓存命中则零 embedding 调用
        qv = openai_embed([q])
        if vecs is None or not qv:
            raise ValueError("embedding unavailable")
        # bge-m3 相似度基线偏高（不相关对约 0.42-0.49），绝对地板 0.48 + 相对窗口 0.12
        # 双重过滤：动态阈值基于最高原始余弦（不含精确命中加成，避免加成压死同簇结果）
        exact, fuzzy = [], []
        top_raw = 0.0
        for t in tools:
            v = vecs.get(t.id)
            if not v:
                continue
            s = cosine(v, qv[0])
            top_raw = max(top_raw, s)
            if q in (t.name or ""):
                exact.append((s + 0.3, t))  # 名称精确命中：必进第一梯队
            elif q in _text(t):
                fuzzy.append((s + 0.1, t))
            elif s >= 0.48:
                fuzzy.append((s, t))
        cutoff = max(0.48, top_raw - 0.12)
        exact.sort(key=lambda x: -x[0])
        fuzzy = [(s, t) for s, t in fuzzy if s >= cutoff]
        fuzzy.sort(key=lambda x: -x[0])
        return [_card(t, cat_names) for _, t in (exact + fuzzy)[:20]]
    except Exception:  # noqa: BLE001
        like = f"%{q}%"
        hits = (
            _channel_visible(
                db.query(Tool)
                .filter(Tool.enabled == True)  # noqa: E712
                .filter(Tool.name.like(like) | Tool.description.like(like) | Tool.tags.like(like)),
                aid,
            )
            .order_by(Tool.is_hot.desc(), Tool.usage_count.desc())
            .limit(20)
            .all()
        )
        return [_card(t, cat_names) for t in hits]


@router.get("")
def list_tools(
    category: str = "",
    sort: str = "hot",
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
    x_app_id: str = Header(""),
    authorization: Optional[str] = Header(None),
):
    account = channel_account_of(db, x_app_id, authorization)
    aid = account.id if account else None
    q = _channel_visible(db.query(Tool).filter(Tool.enabled == True), aid)  # noqa: E712
    if category:
        q = q.filter(Tool.category == category)
    if sort == "new":
        q = q.order_by(Tool.created_at.desc())
    else:  # hot：运营标记优先，其次累计使用量
        q = q.order_by(Tool.is_hot.desc(), Tool.usage_count.desc(), Tool.sort.asc(), Tool.id.asc())
    total = q.count()
    rows = q.offset((max(page, 1) - 1) * size).limit(size).all()
    cat_names = _cat_names(db)
    return {"total": total, "page": page, "size": size, "items": [_card(t, cat_names) for t in rows]}


@router.get("/{tool_id}")
def tool_detail(
    tool_id: str,
    db: Session = Depends(get_db),
    x_app_id: str = Header(""),
    authorization: Optional[str] = Header(None),
):
    account = channel_account_of(db, x_app_id, authorization)
    aid = account.id if account else None
    t = db.query(Tool).filter(Tool.id == tool_id, Tool.enabled == True).first()  # noqa: E712
    if not t or not _tool_visible(t, aid):
        raise HTTPException(404, "tool not found")
    return _detail(t, _cat_names(db))
