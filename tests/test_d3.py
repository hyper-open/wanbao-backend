"""D3 语义搜索验证（§16.1-D3）：embedding 服务 + 混合排序 + LIKE 回落。"""
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:28082"
def _admin_headers():
    """后台鉴权：账号登录换 Bearer 令牌（旧共享令牌已移除）。"""
    body = json.dumps({"username": "admin", "password": __import__("os").environ.get("ADMIN_PASSWORD", "change-me-in-prod")}).encode()
    r = urllib.request.Request(
        BASE + "/api/admin/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(r, timeout=30) as resp:
        return {"Authorization": "Bearer " + json.loads(resp.read().decode())["token"]}


ADMIN = _admin_headers()


def req(method, path, data=None, headers=None):
    h = dict(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def main():
    print("== 1. embedding 服务单测（local 模式） ==")
    from app.services.embedding import DIM, cosine, embed_texts, local_embed

    v1 = local_embed("帮我写周报")
    v2 = local_embed("帮我写周报")
    assert v1 == v2 and len(v1) == DIM  # 确定性 + 维度
    assert abs(cosine(v1, v2) - 1.0) < 1e-6
    vecs = embed_texts(["a", "b"])
    assert len(vecs) == 2 and len(vecs[0]) == DIM
    print(f"local embed OK (dim={DIM})")

    print("\n== 2. 语义查询：LIKE 必空的部分查询 ==")
    q = "述职用的幻灯演示"  # 无任何工具名/描述包含该子串
    st, tools = req("GET", "/api/admin/tools", headers=ADMIN)
    assert not any(q in (t["name"] or "") + (t["description"] or "") for t in tools), "LIKE 应为空，用例失效"
    st, r = req("GET", f"/api/tools/search?q={urllib.parse.quote(q)}")
    ids = [t["id"] for t in r]
    print("semantic q ->", ids)
    assert ids, "语义搜索应为部分匹配返回结果"
    assert "annual_review" in ids or "pptx_optimize" in ids, ids

    print("\n== 3. 精确关键词仍优先 ==")
    st, r = req("GET", "/api/tools/search?q=%E7%AE%80%E5%8E%86")  # 简历
    print("q=简历 ->", [t["id"] for t in r])
    assert r[0]["id"] == "resume_optimize"

    print("\n== 4. recall 不回归 ==")
    st, r = req("POST", "/api/agent/recognize", {"text": "帮我写个周报"})
    assert r["status"] == "confirm" and r["tool_id"] == "weekly_report", r
    print("recognize OK:", r["tool_id"], r["confidence"])

    print("\nD3_ALL_OK")


if __name__ == "__main__":
    main()
