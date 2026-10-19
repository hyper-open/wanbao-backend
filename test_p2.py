"""P2 验证：单工具防亏告警（§16.4-C）。"""
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


def tool_cost_alerts(m):
    return [a for a in m["alerts"] if a["kind"] == "tool_cost"]


def main():
    print("== 1. 登录+充值 ==")
    st, j = req("POST", "/api/auth/login", {"channel": "wechat", "code": "t"})
    h = {"Authorization": f"Bearer {j['token']}"}
    st, j = req("POST", "/api/billing/recharge/create", {"package_id": 1}, headers=h)
    req("POST", "/api/payment/notify", {"order_id": j["order_id"]})

    print("\n== 2. 设置 copy_rewrite 低阈值 0.001 元 ==")
    st, j = req("PUT", "/api/admin/tools/copy_rewrite", {"cost_alert_threshold": 0.001}, headers=ADMIN)
    assert float(j["cost_alert_threshold"]) == 0.001
    print("threshold:", j["cost_alert_threshold"])

    print("\n== 3. 执行工具 -> 触发告警 ==")
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "copy_rewrite", "input": "防亏告警测试"}, headers=h)
    j = poll_success(h, j["invocation_id"])
    assert j["status"] == "success"
    st, m = req("GET", "/api/admin/monitor", headers=ADMIN)
    alerts = tool_cost_alerts(m)
    print("tool_cost alerts:", [(a["kind"], a["message"][:60]) for a in alerts])
    assert len(alerts) == 1 and "copy_rewrite" in alerts[0]["message"], alerts
    assert alerts[0]["payload"]["tool_id"] == "copy_rewrite"

    print("\n== 4. 再次执行 -> 当月去重（不重复告警）==")
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "copy_rewrite", "input": "防亏告警测试第二次"}, headers=h)
    j = poll_success(h, j["invocation_id"])
    assert j["status"] == "success"
    st, m = req("GET", "/api/admin/monitor", headers=ADMIN)
    assert len(tool_cost_alerts(m)) == 1, "expected dedup"
    print("dedup OK")

    print("\n== 5. 其他工具不受影响（未达阈值不告警）==")
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "summarize", "input": "防亏告警对照测试"}, headers=h)
    j = poll_success(h, j["invocation_id"])
    assert j["status"] == "success"
    st, m = req("GET", "/api/admin/monitor", headers=ADMIN)
    alerts = tool_cost_alerts(m)
    assert len(alerts) == 1 and "summarize" not in alerts[0]["message"]
    print("isolation OK")

    print("\n== 6. 恢复默认阈值 ==")
    st, j = req("PUT", "/api/admin/tools/copy_rewrite", {"cost_alert_threshold": None}, headers=ADMIN)
    assert j["cost_alert_threshold"] is None

    print("\nP2_ALL_OK")


if __name__ == "__main__":
    main()
