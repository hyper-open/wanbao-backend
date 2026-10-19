"""工具与渠道组合（多渠道矩阵）集成测试。

覆盖：channel_ids 渠道可见性过滤（类目/列表/搜索/详情）、跨渠道 tool_id 直指防护、
意图识别候选池按渠道收缩。

运行前置：
  1. 后端已部署（默认 http://127.0.0.1:28082，可用 TEST_BASE 覆盖）；
  2. 本机可直连 .env 里的 MySQL（造测试数据 / 清理）；
  3. WX_MOCK=true（mock 登录，不依赖真实微信凭证）。

运行：python3 test_channel_tools.py
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("TEST_BASE", "http://127.0.0.1:28082")
ACC_ID = "ch_it_vp"
ACC_APPID = "wx-it-chan-test"
TOOL_A, TOOL_B = "it_chan_a", "it_chan_b"
NAME_A, NAME_B = "渠道组合测试甲", "渠道组合测试乙"
# 万宝阁小程序（真实渠道账户，用于验证「不在工具集内」的过滤）
DEFAULT_APPID = "wx558f45e1d210e661"


def _db_conn():
    """直连 .env 的 MySQL 造/清测试数据（管理员密码可能已改，绕开后台 API）。"""
    import pymysql

    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    url = ""
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip()
    m = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^:/]+):(\d+)/([^?]+)", url)
    assert m, f"无法解析 DATABASE_URL: {url}"
    user, pwd, host, port, dbname = m.group(1), m.group(2), m.group(3), int(m.group(4)), m.group(5)
    return pymysql.connect(
        host=host, user=user, password=pwd, database=dbname, port=port,
        charset="utf8mb4", autocommit=True,
    )


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
        except Exception:  # noqa: BLE001
            return e.code, {}


def setup():
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT IGNORE INTO channel_account (id, type, name, appid, enabled) "
        "VALUES (%s, 'wechat_mp', '渠道组合集成测试号', %s, 1)",
        (ACC_ID, ACC_APPID),
    )
    # 甲：仅测试渠道可见，且用独占类目（验证类目条按渠道收缩）；乙：全渠道（NULL）
    for tid, name, cat, chan in ((TOOL_A, NAME_A, "it_chan_only", json.dumps([ACC_ID])), (TOOL_B, NAME_B, "office", None)):
        cur.execute(
            "INSERT INTO tool (id, name, category, description, enabled, price_quota, channel_ids) "
            "VALUES (%s, %s, %s, %s, 1, 1, %s) "
            "ON DUPLICATE KEY UPDATE category = VALUES(category), channel_ids = VALUES(channel_ids), enabled = 1",
            (tid, name, cat, f"{name}：集成测试专用工具（关键词 qichanzuhe）", chan),
        )
    conn.close()


def cleanup():
    conn = _db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM tool_embedding WHERE tool_id IN (%s, %s)", (TOOL_A, TOOL_B))
    cur.execute("DELETE FROM tool WHERE id IN (%s, %s)", (TOOL_A, TOOL_B))
    cur.execute("DELETE FROM channel_account WHERE id = %s", (ACC_ID,))
    conn.close()


def login_token(appid):
    """mock 登录换 token；WX_MOCK=false（真实环境）时返回 None，由调用方跳过。"""
    st, j = req("POST", "/api/auth/login", {"code": "it-chan-" + appid, "appid": appid})
    if st != 200 or not j.get("token"):
        return None
    return {"Authorization": "Bearer " + j["token"]}


def main():
    setup()
    failures = []

    def check(name, cond, detail=""):
        print(("PASS" if cond else "FAIL"), name, detail)
        if not cond:
            failures.append(name)

    try:
        print("== 1. 无渠道头：A/B 均可见（向后兼容） ==")
        st, lst = req("GET", "/api/tools?category=it_chan_only&size=100")
        check("列表（独占类目）含 A", TOOL_A in [t["id"] for t in lst["items"]])
        st, lst = req("GET", "/api/tools?category=office&size=100")
        check("列表（office 全页）含 B", TOOL_B in [t["id"] for t in lst["items"]])
        st, d = req("GET", f"/api/tools/{TOOL_A}")
        check("详情 A 可见", st == 200)

        print("== 2. default 渠道（万宝阁）：A 不可见，B 可见 ==")
        h = {"X-App-Id": DEFAULT_APPID}
        st, lst = req("GET", "/api/tools?category=office&size=100", headers=h)
        ids = [t["id"] for t in lst["items"]]
        check("列表不含 A", TOOL_A not in ids)
        check("列表含 B", TOOL_B in ids)
        st, lst = req("GET", "/api/tools?category=it_chan_only", headers=h)
        check("独占类目列表为空", lst["total"] == 0)
        st, d = req("GET", f"/api/tools/{TOOL_A}", headers=h)
        check("详情 A 404", st == 404)
        st, r = req("GET", "/api/tools/search?q=" + urllib.parse.quote(NAME_A), headers=h)
        ids = [t["id"] for t in r] if isinstance(r, list) else []
        check("搜索不含 A", TOOL_A not in ids, f"ids={ids}")

        print("== 3. 测试渠道：A/B 均可见 ==")
        h2 = {"X-App-Id": ACC_APPID}
        st, lst = req("GET", "/api/tools?category=it_chan_only&size=100", headers=h2)
        check("列表（独占类目）含 A", TOOL_A in [t["id"] for t in lst["items"]])
        st, lst = req("GET", "/api/tools?category=office&size=100", headers=h2)
        check("列表（office 全页）含 B", TOOL_B in [t["id"] for t in lst["items"]])
        st, d = req("GET", f"/api/tools/{TOOL_A}", headers=h2)
        check("详情 A 200", st == 200 and d.get("id") == TOOL_A)

        print("== 4. 类目条按渠道收缩（A 用独占类目 it_chan_only） ==")
        st, c1 = req("GET", "/api/tools/categories", headers={"X-App-Id": DEFAULT_APPID})
        st, c2 = req("GET", "/api/tools/categories", headers=h2)
        cats1 = {c["category"] for c in c1}
        cats2 = {c["category"] for c in c2}
        check("default 渠道类目不含 A 的独占类目", "it_chan_only" not in cats1)
        check("测试渠道类目含 A 的独占类目", "it_chan_only" in cats2)

        print("== 5. 意图识别候选池按渠道收缩 ==")
        q = f"我要用{NAME_A}处理内容"
        st, r = req("POST", "/api/agent/recognize", {"text": q, "appid": DEFAULT_APPID})
        menu_ids = [m["id"] for m in (r.get("menu") or [])]
        check("default 渠道不选中 A", r.get("tool_id") != TOOL_A, f"status={r.get('status')}")
        check("default 渠道菜单不含 A", TOOL_A not in menu_ids, f"menu={menu_ids}")
        st, r = req("POST", "/api/agent/recognize", {"text": q, "appid": ACC_APPID})
        menu_ids = [m["id"] for m in (r.get("menu") or [])]
        in_menu_or_chosen = r.get("tool_id") == TOOL_A or TOOL_A in menu_ids
        # LLM/召回有随机性，这里只验证「候选池里有 A」的间接信号：菜单兜底时必须包含 A
        ok = (r.get("status") != "menu") or in_menu_or_chosen
        check("测试渠道候选池含 A（菜单兜底时）", ok, f"status={r.get('status')} menu={menu_ids}")

        print("== 6. 跨渠道 tool_id 直指防护（会话；需 WX_MOCK） ==")
        h_def = login_token(DEFAULT_APPID)
        h_test = login_token(ACC_APPID)
        if not h_def or not h_test:
            print("SKIP：WX_MOCK=false（真实登录），跳过会话直指防护用例（防护逻辑见 _pick_tool 的渠道校验）")
        else:
            st, r = req("POST", "/api/session/chat", {"tool_id": TOOL_A, "input": "你好"}, headers=h_def)
            check("default 渠道直指 A 被拒", st == 404 and "工具不存在" in str(r.get("detail", "")), f"{st} {r}")
            st, r = req("POST", "/api/session/chat", {"tool_id": TOOL_A, "input": "你好"}, headers=h_test)
            check("测试渠道直指 A 放行（非 404）", not (st == 404 and "工具不存在" in str(r.get("detail", ""))), f"{st} {r}")
    finally:
        cleanup()
        print("\n清理完成（测试工具/渠道账户已删除）")

    print("\n" + ("全部通过 ✓" if not failures else f"失败 {len(failures)} 项: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
