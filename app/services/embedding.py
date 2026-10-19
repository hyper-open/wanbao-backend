"""文本向量化（§16.1-D3 语义搜索 / §7.3 embedding 槽位共用）。

配置全部在后台「向量配置」（app_setting，无 env）：
- provider=local（未配置时的默认）：字符 bigram 哈希到 256 维——零依赖、离线可用、确定性；
  对中文具备部分匹配/容错能力，明显优于 LIKE 子串匹配（同义改写仍有限）。
- provider=openai：base_url/api_key/model 走 OpenAI 兼容 /embeddings，获得真语义（同义词/改写）。

`embed_texts` 恒可用：未配置或调用失败自动回落 local（降级策略）。
"""
import hashlib
import json
import math
import re
from typing import Optional
from urllib.request import Request, urlopen


DIM = 256

_EMBED_CFG: dict = {"v": None, "at": 0.0}


def cfg() -> dict:
    """后台向量配置（app_setting，30s 缓存）。provider：local（默认降级）/ openai。"""
    import time

    if time.time() - _EMBED_CFG["at"] > 30:
        from app.services import appsettings

        _EMBED_CFG["v"] = {
            "provider": (appsettings.get_str("EMBEDDING_PROVIDER") or "local").strip().lower(),
            "base_url": (appsettings.get_str("EMBEDDING_BASE_URL") or "").strip(),
            "api_key": appsettings.get_str("EMBEDDING_API_KEY"),
            "model": (appsettings.get_str("EMBEDDING_MODEL") or "").strip(),
        }
        _EMBED_CFG["at"] = time.time()
    return _EMBED_CFG["v"]


def cache_model_name(c: Optional[dict] = None) -> str:
    """向量缓存键：model@md5(base_url)——换模型或换服务都会自动全量重算，防新旧向量混空间。"""
    c = c or cfg()
    h = hashlib.md5(c["base_url"].encode()).hexdigest()[:10] if c["base_url"] else "local"
    return f"{c['model'] or 'local'}@{h}"



def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i: i + 2] for i in range(len(s) - 1)}


def local_embed(text: str) -> list[float]:
    """确定性哈希向量：bigram -> index = hash(bigram) % DIM，词频归一。"""
    vec = [0.0] * DIM
    for bg in _bigrams(text):
        vec[hash(bg) % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1e-9
    return [v / norm for v in vec]


def openai_embed(texts: list[str]) -> Optional[list[list[float]]]:
    c = cfg()
    base, key, model = c["base_url"], c["api_key"], c["model"]
    if not (base and model):
        return None
    try:
        req = Request(
            base.rstrip("/") + "/embeddings",
            data=json.dumps({"input": texts, "model": model}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urlopen(req, timeout=120) as r:  # 批量补算 171 个工具向量在 CPU 上可能超过 10s
            data = json.loads(r.read().decode())
        return [d["embedding"] for d in data["data"]]
    except Exception:  # noqa: BLE001
        return None


def embed_texts(texts: list[str]) -> list[list[float]]:
    """provider 优先，未配置/失败回落 local（local 恒可用，不返回 None）。"""
    if cfg()["provider"] == "openai":
        r = openai_embed(texts)
        if r and len(r) == len(texts):
            return r
    return [local_embed(t) for t in texts]


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1e-9
    db = math.sqrt(sum(x * x for x in b)) or 1e-9
    return num / (da * db)


def tool_content(t) -> str:
    """参与向量计算的文本（name+描述+标签）；工具编辑后内容变化即自动重算。"""
    tags = " ".join(t.tags or []) if getattr(t, "tags", None) else ""
    return f"{t.name}：{t.description or ''}{(' ' + tags) if tags else ''}"


def get_tool_vectors(db, tools) -> Optional[dict]:
    """工具向量缓存：命中直接用，缺失/内容变更的批量补算并落库。

    返回 {tool_id: 向量}；provider 未配置或 embedding 调用失败 → None（调用方回落）。
    查询文本的向量由调用方单独计算（每次仅 1 条请求）。
    """
    c = cfg()
    if c["provider"] != "openai" or not (c["base_url"] and c["model"]):
        return None
    from app.core.timeutil import utcnow
    from app.models import ToolEmbedding

    model_name = cache_model_name(c)
    ids = [t.id for t in tools]
    rows = (
        db.query(ToolEmbedding)
        .filter(ToolEmbedding.model == model_name, ToolEmbedding.tool_id.in_(ids))
        .all()
        if ids
        else []
    )
    cmap = {r.tool_id: r for r in rows}
    vecs, need = {}, []
    for t in tools:
        c = tool_content(t)
        r = cmap.get(t.id)
        if r and r.content == c and r.vector:
            vecs[t.id] = r.vector
        else:
            need.append((t, c))
    if need:
        B = 64  # 分批请求：全量一次请求在 CPU 服务上会超 urlopen 120s 超时
        for i in range(0, len(need), B):
            chunk = need[i:i + B]
            embs = openai_embed([c for _, c in chunk])
            if not embs or len(embs) != len(chunk):
                return None  # 某批失败：本次整体回落；已落库批次下次直接命中（断点续传）
            for (t, c), v in zip(chunk, embs):
                vecs[t.id] = v
                db.merge(
                    ToolEmbedding(tool_id=t.id, model=model_name, content=c, vector=v, updated_at=utcnow())
                )
            db.commit()
    return vecs
