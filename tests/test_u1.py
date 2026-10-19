"""§16.2-U1 验证：停售预约 waitlist + Order 主体/通道字段。

两阶段（依赖 STOP_SELL_THRESHOLD env）：
  phase1: 正常阈值下充值，制造本月收款
  phase2: STOP_SELL_THRESHOLD=1 触发停售 -> 验证 sold_out/join/去重/触达/entity 字段
"""
import json
import sys
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


def phase1():
    print("== P1: 正常阈值下充值 ==")
    st, j = req("POST", "/api/auth/login", {"channel": "wechat", "code": "u1"})
    h = {"Authorization": f"Bearer {j['token']}"}
    open("/tmp/u1_token.txt", "w").write(j["token"])
    st, j = req("POST", "/api/billing/recharge/create", {"package_id": 1}, headers=h)
    assert st == 200, j
    req("POST", "/api/payment/notify", {"order_id": j["order_id"]})
    print("funded OK")
    print("PHASE1_OK")


def phase2():
    print("== P2: 停售态（STOP_SELL_THRESHOLD=1）==")
    token = open("/tmp/u1_token.txt").read()
    h = {"Authorization": f"Bearer {token}"}

    print("\n== 1. 充值被停售拦截 ==")
    st, j = req("POST", "/api/billing/recharge/create", {"package_id": 1}, headers=h)
    print(st, j)
    assert st == 400 and j["detail"] == "sold_out"

    print("\n== 2. packages 返回停售标记 ==")
    st, lst = req("GET", "/api/billing/packages")
    assert lst and all(p["sold_out"] for p in lst)
    print("sold_out OK")

    print("\n== 3. 预约 join（幂等）==")
    st, j = req("POST", "/api/billing/restock/join", {"channel": "wechat"}, headers=h)
    print(st, j)
    assert st == 200 and j["joined"] and not j["already"]
    st, j = req("POST", "/api/billing/restock/join", {"channel": "wechat"}, headers=h)
    assert j["already"] is True
    st, s = req("GET", "/api/billing/restock/status", headers=h)
    assert s["joined"] and s["sold_out"]
    print("join/status OK")

    print("\n== 4. 未停售用户 join 被拒（对照：正常阈值下） ==")
    # 当前进程是停售态；该用例在 phase3（恢复正常阈值）覆盖

    print("\n== 5. admin waitlist 列表 + 人工标记 ==")
    st, rows = req("GET", "/api/admin/waitlist", headers=ADMIN)
    print("waitlist:", [(r["user_id"], r["notified"]) for r in rows])
    assert any(r["notified"] is False for r in rows)
    wid = rows[0]["id"]
    st, j = req("PUT", f"/api/admin/waitlist/{wid}", {"notified": True}, headers=ADMIN)
    assert j["notified"] is True and j["notified_at"]

    print("\n== 6. 月度触达任务：上月未通知 -> 置位 ==")
    from app.core.db import SessionLocal
    from datetime import datetime, timedelta, timezone
    from app.models import RestockWaitlist
    from app.services.billing import notify_waitlist_for_new_month

    db = SessionLocal()
    db.commit()
    rows = db.query(RestockWaitlist).filter(RestockWaitlist.notified == False).all()
    if not rows:
        # 全部已被人工标记 -> 造一条"上月"预约
        db.add(RestockWaitlist(user_id=rows and 1 or 1, channel="wechat"))
        db.commit()
        rows = db.query(RestockWaitlist).filter(RestockWaitlist.notified == False).all()
    for r in rows:
        r.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40)
    db.commit()
    n = notify_waitlist_for_new_month(db)
    db.commit()
    print("monthly notified:", n)
    assert n >= 1
    db.close()

    print("\n== 7. order.entity / payment_provider 落库 ==")
    from app.models import Order
    db = SessionLocal()
    db.commit()
    o = db.query(Order).order_by(Order.id.desc()).first()
    print("order:", o.id, o.payment_provider, o.entity, "client_channel:", o.client_channel)
    assert o.payment_provider == "mock" and o.entity == "personal"
    db.close()

    print("\nPHASE2_OK")


def phase3():
    print("== P3: 恢复正常阈值 -> 未停售时 join 被拒 ==")
    token = open("/tmp/u1_token.txt").read()
    h = {"Authorization": f"Bearer {token}"}
    st, s = req("GET", "/api/billing/restock/status", headers=h)
    print("status:", s)
    if not s["sold_out"]:
        st, j = req("POST", "/api/billing/restock/join", {}, headers=h)
        print("join while not sold_out:", st, j)
        assert st == 400 and j["detail"] == "not_sold_out"
        print("PHASE3_OK")
    else:
        print("本月仍停售（历史累计 > 1），跳过 phase3 断言")
        print("PHASE3_SKIPPED")


if __name__ == "__main__":
    {"1": phase1, "2": phase2, "3": phase3}[sys.argv[1]]()
