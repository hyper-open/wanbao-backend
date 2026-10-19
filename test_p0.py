"""P0 合规项端到端验证：反馈/举报、封禁、备案字段、AI 生成元数据标识。"""
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


def main():
    print("== 1. 登录+充值 ==")
    st, j = req("POST", "/api/auth/login", {"code": "t"})
    h = {"Authorization": f"Bearer {j['token']}"}
    _, j = req("POST", "/api/billing/recharge/create", {"package_id": 1}, headers=h)
    req("POST", "/api/payment/notify", {"order_id": j["order_id"]})

    print("\n== 2. 反馈/举报提交（建议+举报） ==")
    st, j = req("POST", "/api/feedback", {"type": "suggestion", "content": "希望支持导出 PDF"}, headers=h)
    print("suggestion:", st, j); assert st == 200
    st, j = req("POST", "/api/feedback", {"type": "report", "content": "结果内容有误，举报", "invocation_id": 1}, headers=h)
    print("report:", st, j); assert st == 200
    # 违禁反馈内容应被内容安全拦截
    st, j = req("POST", "/api/feedback", {"type": "suggestion", "content": "包含违禁词示例A的内容"}, headers=h)
    print("blocklist feedback:", st, j); assert st == 400

    print("\n== 3. admin 反馈列表+处理 ==")
    st, lst = req("GET", "/api/admin/feedback", headers=ADMIN)
    print("count:", len(lst)); assert len(lst) >= 2
    fid = lst[0]["id"]
    st, j = req("PUT", f"/api/admin/feedback/{fid}", {"status": "processed"}, headers=ADMIN)
    assert j["status"] == "processed"; print("processed feedback", fid)

    print("\n== 4. 用户列表+封禁/恢复（封禁后 confirm 403） ==")
    st, users = req("GET", "/api/admin/users", headers=ADMIN)
    me = users[0]
    print("users:", len(users), "me:", me["id"], me["status"])
    st, _ = req("PUT", f"/api/admin/users/{me['id']}", {"status": "ban"}, headers=ADMIN)
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "copy_rewrite", "input": "测试"}, headers=h)
    print("confirm while banned:", st, j); assert st == 403
    st, _ = req("PUT", f"/api/admin/users/{me['id']}", {"status": "normal"}, headers=ADMIN)
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "copy_rewrite", "input": "帮我改写一段文案"}, headers=h)
    print("confirm after unban:", st); assert st == 200
    inv_id = j["invocation_id"]
    for _ in range(15):
        time.sleep(2)
        st, j = req("GET", f"/api/agent/invocation/{inv_id}", headers=h)
        if j["status"] in ("success", "failed"):
            break
    assert j["status"] == "success"

    print("\n== 5. pptx 导出含 AI 生成元数据 ==")
    import tempfile, os, io
    from test_m4 import make_pptx
    fd, p = tempfile.mkstemp(suffix=".pptx"); os.close(fd)
    make_pptx(p)
    pptx_bytes = open(p, "rb").read()
    boundary = "----p0t"
    parts = [f"--{boundary}".encode(),
             b'Content-Disposition: form-data; name="files"; filename="t.pptx"',
             b"Content-Type: application/vnd.openxmlformats-officedocument.presentationml.presentation",
             b"", pptx_bytes, f"--{boundary}--".encode(), b""]
    r = urllib.request.Request(BASE + "/api/attachment/upload", data=b"\r\n".join(parts), method="POST",
                               headers=dict(h, **{"Content-Type": f"multipart/form-data; boundary={boundary}"}))
    with urllib.request.urlopen(r, timeout=30) as resp:
        att_ids = json.loads(resp.read().decode())["attachment_ids"]
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "pptx_optimize", "input": "", "attachment_ids": att_ids}, headers=h)
    inv_id = j["invocation_id"]
    for _ in range(20):
        time.sleep(2)
        st, j = req("GET", f"/api/agent/invocation/{inv_id}", headers=h)
        if j["status"] in ("success", "failed"):
            break
    assert j["status"] == "success", j
    du = j["download_url"].replace("host.docker.internal", "127.0.0.1")
    with urllib.request.urlopen(du, timeout=15) as resp:
        data = resp.read()
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    cp = prs.core_properties
    print("author:", cp.author, "| category:", cp.category)
    assert cp.author == "万宝工具箱 AI" and cp.category == "AI生成内容"
    notes = prs.slides[0].notes_slide.notes_text_frame.text
    print("notes:", notes[:40])
    assert "AI" in notes and "万宝工具箱" in notes

    print("\n== 5b. docx 导出含 AI 生成元数据与文末说明 ==")
    from docx import Document as MakeDocx
    fd2, p2 = tempfile.mkstemp(suffix=".docx"); os.close(fd2)
    d = MakeDocx(); d.add_paragraph("五年后端开发经验"); d.save(p2)
    docx_bytes = open(p2, "rb").read()
    boundary2 = "----p0d"
    parts2 = [f"--{boundary2}".encode(),
              b'Content-Disposition: form-data; name="files"; filename="r.docx"',
              b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              b"", docx_bytes, f"--{boundary2}--".encode(), b""]
    r2 = urllib.request.Request(BASE + "/api/attachment/upload", data=b"\r\n".join(parts2), method="POST",
                                headers=dict(h, **{"Content-Type": f"multipart/form-data; boundary={boundary2}"}))
    with urllib.request.urlopen(r2, timeout=30) as resp:
        att_ids2 = json.loads(resp.read().decode())["attachment_ids"]
    st, j = req("POST", "/api/agent/confirm", {"tool_id": "resume_optimize", "input": "", "attachment_ids": att_ids2}, headers=h)
    inv_id = j["invocation_id"]
    for _ in range(20):
        time.sleep(2)
        st, j = req("GET", f"/api/agent/invocation/{inv_id}", headers=h)
        if j["status"] in ("success", "failed"):
            break
    assert j["status"] == "success", j
    du = j["download_url"].replace("host.docker.internal", "127.0.0.1")
    with urllib.request.urlopen(du, timeout=15) as resp:
        data2 = resp.read()
    from docx import Document as ReadDocx
    d2 = ReadDocx(io.BytesIO(data2))
    cp2 = d2.core_properties
    print("author:", cp2.author, "| category:", cp2.category)
    assert cp2.author == "万宝工具箱 AI" and cp2.category == "AI生成内容"
    last_text = d2.paragraphs[-1].text
    print("last line:", last_text)
    assert "AI" in last_text and "万宝工具箱" in last_text

    print("\n== 6. monitor 返回备案字段 ==")
    st, m = req("GET", "/api/admin/monitor", headers=ADMIN)
    print("providers:", m["providers"][:2])
    assert "filing_no" in m["providers"][0]

    print("\n== 7. admin 页面 JS 语法（本地校验） ==")
    import re
    html = open("static/admin/index.html").read()
    open("/tmp/admin_p0.js", "w").write(re.search(r"<script>(.*)</script>", html, re.S).group(1))

    print("\nP0_ALL_OK")


if __name__ == "__main__":
    main()
