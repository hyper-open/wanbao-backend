"""工具发现 D1/D2 验证（§16.1）：类目/列表/搜索/详情/热度计数/menu 分组。"""
import json
import time
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


def poll_success(h, inv_id, n=15):
    for _ in range(n):
        time.sleep(2)
        st, j = req("GET", f"/api/agent/invocation/{inv_id}", headers=h)
        if j["status"] in ("success", "failed"):
            return j
    return j


def main():
    print("== 1. 运营数据：tags/is_hot/sort 写入 ==")
    st, j = req("PUT", "/api/admin/tools/resume_optimize",
                {"tags": ["简历", "求职"], "is_hot": True, "sort": 1}, headers=ADMIN)
    assert j["is_hot"] and j["tags"] == ["简历", "求职"]

    print("\n== 2. 类目接口 ==")
    st, cats = req("GET", "/api/tools/categories")
    print(cats)
    cats_map = {c["category"]: c["count"] for c in cats}
    assert cats_map.get("writing", 0) >= 4 and cats_map.get("doc", 0) >= 2

    print("\n== 3. 列表：hot 排序（运营标记优先）+ 类目过滤 + 分页 ==")
    st, lst = req("GET", "/api/tools?sort=hot")
    assert lst["total"] >= 11 and len(lst["items"]) <= 20
    assert lst["items"][0]["is_hot"], lst["items"][0]
    st, lst = req("GET", "/api/tools?category=writing")
    assert all(i["category"] == "writing" for i in lst["items"]) and lst["total"] >= 4
    st, lst = req("GET", "/api/tools?page=2&size=5")
    assert len(lst["items"]) == 5 and lst["page"] == 2
    print("list OK, first hot:", lst["items"][0]["name"] if False else [i["name"] for i in req("GET", "/api/tools?sort=hot")[1]["items"][:2]])

    print("\n== 4. 搜索（name/description/tags） ==")
    st, r = req("GET", "/api/tools/search?q=" + urllib.parse.quote("简历"))
    ids = [t["id"] for t in r]
    print("q=简历 ->", ids)
    assert "resume_optimize" in ids
    st, r = req("GET", "/api/tools/search?q=" + urllib.parse.quote("求职"))  # 命中 tags
    assert any(t["id"] == "resume_optimize" for t in r)

    print("\n== 5. 详情 ==")
    st, d = req("GET", "/api/tools/resume_optimize")
    assert d["needs_attachment"] and d["attachment_spec"].get("formats") == ["docx", "pdf"]
    print("detail OK:", d["name"], d["attachment_spec"])

    print("\n== 6. 热度计数：执行一次后 usage_count+1 ==")
    st, j = req("POST", "/api/auth/login", {"channel": "wechat", "code": "t"})
    h = {"Authorization": f"Bearer {j['token']}"}
    st, j = req("POST", "/api/billing/recharge/create", {"package_id": 1}, headers=h)
    req("POST", "/api/payment/notify", {"order_id": j["order_id"]})
    st, before = req("GET", "/api/tools/copy_rewrite")
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "copy_rewrite", "input": "热度计数测试"}, headers=h)
    j = poll_success(h, j["invocation_id"])
    assert j["status"] == "success"
    st, after = req("GET", "/api/tools/copy_rewrite")
    print(f"usage_count: {before['usage_count']} -> {after['usage_count']}")
    assert after["usage_count"] == before["usage_count"] + 1

    print("\n== 7. menu 兜底带类目（小程序分组用） ==")
    st, r = req("POST", "/api/agent/recognize", {"text": "今天天气真不错啊随便说说"})
    assert r["status"] == "menu"
    assert all("category" in m for m in r["menu"])
    print("menu items:", [(m["id"], m["category"]) for m in r["menu"][:3]], "...")

    print("\nD1D2_ALL_OK")


if __name__ == "__main__":
    main()
