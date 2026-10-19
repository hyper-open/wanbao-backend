"""P1 验证：成本闭环（§16.4）+ 多渠道身份/定价（§9.5）。"""
import json
import time
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
    print("== 1. 双渠道登录（wechat / alipay）==")
    st, a = req("POST", "/api/auth/login", {"channel": "wechat", "code": "t"})
    ha = {"Authorization": f"Bearer {a['token']}"}
    st, b = req("POST", "/api/auth/login", {"channel": "alipay", "code": "t"})
    hb = {"Authorization": f"Bearer {b['token']}"}
    print(f"A(wechat): user_id={a['user_id']}  B(alipay): user_id={b['user_id']}")
    assert a["user_id"] != b["user_id"] and b["channel"] == "alipay"
    # 同渠道重复登录映射同一账户
    st, a2 = req("POST", "/api/auth/login", {"channel": "wechat", "code": "t2"})
    print("wechat re-login user_id:", a2["user_id"], "(mock 每次新 uid -> 新户，属预期)")

    print("\n== 2. 渠道档位：alipay 专属档 + 渠道价 ==")
    st, pkg = req("POST", "/api/admin/packages", {
        "name": "支付宝尝鲜包", "quota": 10, "price": 9.9, "price_ios": 9.9,
        "sort": 9, "enabled": True, "channels": ["alipay"], "channel_prices": {"alipay": 5.5},
    }, headers=ADMIN)
    print("created pkg:", pkg["id"], pkg["channels"], pkg["channel_prices"])
    st, lst = req("GET", "/api/billing/packages?channel=alipay")
    assert any(p["id"] == pkg["id"] and p["price"] == 5.5 for p in lst), lst
    st, lst = req("GET", "/api/billing/packages?channel=wechat")
    assert not any(p["id"] == pkg["id"] for p in lst)
    print("channel filter + channel price OK")

    print("\n== 3. 渠道充值（支付宝户充 5.5 元）==")
    st, j = req("POST", "/api/billing/recharge/create", {"package_id": pkg["id"], "channel": "alipay"}, headers=hb)
    assert st == 200, j
    req("POST", "/api/payment/notify", {"order_id": j["order_id"]})
    st, acct = req("GET", "/api/billing/account/me", headers=hb)
    print("alipay account:", acct)
    assert acct["balance"] == 5.5 and acct["quota_total"] == 10

    print("\n== 4. 工具渠道定价：alipay 下 copy_rewrite=2 次 ==")
    st, _ = req("PUT", "/api/admin/tools/copy_rewrite", {"channel_pricing": {"alipay": 2}}, headers=ADMIN)
    st, r = req("POST", "/api/agent/recognize", {"text": "帮我改写一句文案", "channel": "alipay"}, headers=hb)
    print("recognize(alipay):", r.get("tool_id"), "price_quota =", r.get("price_quota"))
    assert r.get("price_quota") == 2

    print("\n== 5. 扣次与成本落库 ==")
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "copy_rewrite", "input": "帮我改写一句文案", "channel": "alipay"}, headers=hb)
    inv_b = j["invocation_id"]
    j = poll_success(hb, inv_b)
    assert j["status"] == "success", j
    print(f"B cost_amount={j['cost_amount']}")
    assert 0 < j["cost_amount"] < 0.02  # mock: 300 tokens × 0.01/1k = 0.006
    st, acct = req("GET", "/api/billing/account/me", headers=hb)
    assert acct["quota_used"] == 2, acct  # 渠道定价生效

    st, j = req("POST", "/api/billing/recharge/create", {"package_id": 1}, headers=ha)
    req("POST", "/api/payment/notify", {"order_id": j["order_id"]})
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "copy_rewrite", "input": "帮我改写一句文案"}, headers=ha)
    inv_a = j["invocation_id"]
    j = poll_success(ha, inv_a)
    assert j["status"] == "success"
    print(f"A cost_amount={j['cost_amount']}")
    st, acct = req("GET", "/api/billing/account/me", headers=ha)
    assert acct["quota_used"] == 1  # wechat 默认 1 次

    print("\n== 6. 经营视图（monitor.biz）==")
    st, m = req("GET", "/api/admin/monitor", headers=ADMIN)
    biz = m["biz"]
    print({k: biz[k] for k in ("month", "paid_total", "calls_success", "quota_consumed", "cost_total", "margin", "margin_pct")})
    assert biz["cost_total"] > 0 and biz["calls_success"] >= 2 and biz["quota_consumed"] >= 3
    tool_ids = [t["tool_id"] for t in biz["per_tool"]]
    assert "copy_rewrite" in tool_ids
    print("per_tool:", biz["per_tool"])
    print("per_provider:", biz["per_provider"])
    assert biz["per_provider"].get("mock-executor", 0) >= 2

    print("\n== 7. 渠道不可售拦截 ==")
    st, j = req("POST", "/api/billing/recharge/create", {"package_id": pkg["id"]}, headers=ha)  # wechat 户买 alipay 档
    print("blocked:", st, j)
    assert st == 400

    print("\nP1_ALL_OK")


if __name__ == "__main__":
    main()
