"""交互式多轮会话端到端验证（§6.5 工具图内人工介入）。

覆盖：建会话（分析）→ 追问/回答 → 跳过 → 确认扣次 → 产出文档 → 幂等与防白嫖。

用法：
    WB_TOKEN=<用户 token> python3 test_s1.py

用户 token 获取（开发环境，小程序真实登录不可脚本化）：
    docker exec wanbao python3 -c "
    from app.core.db import SessionLocal
    from app.models import UserIdentity
    from app.core.security import create_token
    db=SessionLocal(); i=db.query(UserIdentity).filter_by(user_id=56).first()
    print(create_token(i.channel_uid, channel=i.channel, user_id=56))"
"""
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile

BASE = "http://127.0.0.1:28082"
TOKEN = os.environ.get("WB_TOKEN", "")
TOOL_ID = os.environ.get("WB_TOOL", "resume_optimize")


def _eval(resp):
    return json.loads(resp) if resp else None


def req(method, path, data=None, headers=None, raw=None, ctype=None):
    h = dict(headers or {})
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    if raw is not None:
        body = raw
        if ctype:
            h["Content-Type"] = ctype
    elif data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    else:
        body = None
    r = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, _eval(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, _eval(e.read().decode())


def make_resume_docx() -> bytes:
    """用标准库拼一个最小可用 docx（含可检测的缺失/可疑点）。"""
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>张三 | 后端开发工程师</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>工作经历</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>2021.03—2024.06 某科技公司 负责相关工作</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>参与系统开发，熟悉各种框架，有一定经验</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def upload(name: str, data: bytes) -> list:
    boundary = "----wbtests1boundary"
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(data)
    parts.append(f"\r\n--{boundary}\r\n".encode())
    parts.append('Content-Disposition: form-data; name="filename"\r\n\r\n'.encode())
    parts.append(name.encode())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    st, r = req("POST", "/api/attachment/upload", raw=body, ctype=f"multipart/form-data; boundary={boundary}")
    assert st == 200, f"上传失败 {st} {r}"
    return r["attachment_ids"]


def poll(sid: int, want: tuple, timeout=300):
    """轮询直到会话进入期望状态之一。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        st, r = req("GET", f"/api/agent/session/{sid}")
        assert st == 200, f"查询会话失败 {st} {r}"
        last = r
        if r["status"] in want:
            return r
        time.sleep(3)
    raise AssertionError(f"轮询超时，最后状态={last and last['status']}")


def main():
    assert TOKEN, "请设置 WB_TOKEN 环境变量（见文件头注释）"

    print("== 1. 上传简历 ==")
    ids = upload("简历.docx", make_resume_docx())
    print("   attachment_ids =", ids)

    print("== 2. 建会话（分析）==")
    st, s = req(
        "POST",
        "/api/agent/session",
        {"tool_id": TOOL_ID, "input": "想投后端岗位，突出项目亮点", "attachment_ids": ids},
    )
    assert st == 200, f"建会话失败 {st} {s}"
    sid = s["session_id"]
    print("   session_id =", sid, "| price_quota =", s["price_quota"], "| max_rounds =", s["max_rounds"])

    print("== 3. 等待分析结果（应停在 追问 或 确认）==")
    r = poll(sid, ("awaiting_answer", "awaiting_confirm", "failed"))
    assert r["status"] != "failed", f"分析失败: {r['err']}"
    assert r["analysis"], "缺少分析结果"
    miss, susp = r["analysis"].get("missing") or [], r["analysis"].get("suspicious") or []
    print(f"   状态={r['status']} 缺失={len(miss)} 可疑={len(susp)} 轮次={r['round']} 剩余={r['quota_remain']}")
    for it in (miss + susp)[:4]:
        print(f"     · [{it['priority']}] {it['label']}：{it['detail'][:40]}")
    base_remain = r["quota_remain"]

    if r["status"] == "awaiting_answer":
        print("== 4. 回答一轮追问 ==")
        st, a = req("POST", f"/api/agent/session/{sid}/answer", {"text": "手机 13800000000，邮箱 zhangsan@example.com；主导订单系统重构，QPS 提升 3 倍"})
        assert st == 200, f"回答失败 {st} {a}"
        r = poll(sid, ("awaiting_answer", "awaiting_confirm", "failed"))
        assert r["status"] != "failed", f"追问失败: {r['err']}"
        print("   状态=", r["status"], "轮次=", r["round"])

    if r["status"] == "awaiting_answer":
        print("== 5. 跳过剩余追问 ==")
        st, _ = req("POST", f"/api/agent/session/{sid}/skip")
        assert st == 200, "跳过失败"
        r = poll(sid, ("awaiting_confirm", "failed"))
        assert r["status"] == "awaiting_confirm", f"未进入确认: {r}"

    print("== 6. 确认（扣次）==")
    assert r["interrupt"] and r["interrupt"]["type"] == "confirm_charge", f"确认中断异常: {r['interrupt']}"
    st, c = req("POST", f"/api/agent/session/{sid}/confirm")
    assert st == 200, f"确认失败 {st} {c}"
    inv_id = c["invocation_id"]
    print("   invocation_id =", inv_id)

    print("== 7. 等待产出 ==")
    t0 = time.time()
    while time.time() - t0 < 300:
        st, inv = req("GET", f"/api/agent/invocation/{inv_id}")
        if inv.get("status") in ("success", "failed"):
            break
        time.sleep(3)
    assert inv["status"] == "success", f"产出失败: {inv.get('err')}"
    print("   状态=success model=", inv.get("model_used"), "cost=", inv.get("cost_amount"))

    print("== 8. 校验扣次（恰好一次）与文档可下载 ==")
    st, r2 = req("GET", f"/api/agent/session/{sid}")
    after = r2["quota_remain"]
    print(f"   剩余：前 {base_remain} → 后 {after}")
    assert after == base_remain - s["price_quota"], "扣次数量与 tool.price_quota 不一致"

    st, c2 = req("POST", f"/api/agent/session/{sid}/confirm")
    assert st == 200 and c2.get("idempotent"), f"重复确认未幂等: {c2}"
    st, r3 = req("GET", f"/api/agent/session/{sid}")
    assert r3["quota_remain"] == after, "幂等确认后余额被重复扣减"

    st, doc_bytes = _download(f"/api/agent/invocation/{inv_id}/file")
    assert doc_bytes[:2] == b"PK", "下载产物不是合法的 docx(zip)"
    print(f"   ⬇ 文档 {len(doc_bytes)} bytes, zip 魔数 OK")

    print("\nS1_ALL_OK")


def _download(path: str) -> tuple:
    r = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.status, resp.read()


if __name__ == "__main__":
    main()
