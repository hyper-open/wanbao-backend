"""M5 端到端验证：全量异步 / presigned 下载 / 限流 / 识别三分支。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:28082"


def req(method, path, data=None, token=None, headers=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    print("== 1. 登录+充值 ==")
    st, body = req("POST", "/api/auth/login", {"code": "t"})
    tok = json.loads(body)["token"]
    h = {"Authorization": f"Bearer {tok}"}
    _, body = req("GET", "/api/billing/packages", headers=h)
    pkg = json.loads(body)[0]["id"]
    _, body = req("POST", "/api/billing/recharge/create", {"package_id": pkg}, headers=h)
    req("POST", "/api/payment/notify", {"order_id": json.loads(body)["order_id"]})
    print("funded:", req("GET", "/api/billing/account/me", headers=h)[1])

    print("\n== 2. 识别三分支 ==")
    for text in ["帮我写个周报", "帮我翻译一下这段文案", "今天天气真不错"]:
        st, r = req("POST", "/api/agent/recognize", {"text": text}, headers=h)
        j = json.loads(r)
        print(f"  [{text}] {j.get('status')} {j.get('tool_id')} {j.get('confidence')}")

    print("\n== 3. 全量异步：纯文本工具 copy_rewrite ==")
    st, r = req("POST", "/api/agent/confirm", {"tool_id": "copy_rewrite", "input": "帮我改写一句产品文案"}, headers=h)
    j = json.loads(r)
    print(st, j)
    assert j.get("async") is True, "expected async"
    inv_id = j["invocation_id"]
    for i in range(15):
        time.sleep(2)
        st, r = req("GET", f"/api/agent/invocation/{inv_id}", headers=h)
        jj = json.loads(r)
        if jj["status"] in ("success", "failed"):
            break
    print("final:", jj["status"], (jj.get("result_text") or "")[:60])
    assert jj["status"] == "success"

    print("\n== 4. presigned 下载：跑一次 pptx_optimize 验证签名 URL ==")
    import tempfile, os
    from test_m4 import make_pptx
    fd, p = tempfile.mkstemp(suffix=".pptx"); os.close(fd)
    make_pptx(p)
    pptx_bytes = open(p, "rb").read()
    boundary = "----wbtest5"
    parts = [f"--{boundary}".encode(),
             b'Content-Disposition: form-data; name="files"; filename="t.pptx"',
             b"Content-Type: application/vnd.openxmlformats-officedocument.presentationml.presentation",
             b"", pptx_bytes, f"--{boundary}--".encode(), b""]
    r = urllib.request.Request(BASE + "/api/attachment/upload",
                               data=b"\r\n".join(parts), method="POST",
                               headers=dict(h, **{"Content-Type": f"multipart/form-data; boundary={boundary}"}))
    with urllib.request.urlopen(r, timeout=30) as resp:
        att_ids = json.loads(resp.read().decode())["attachment_ids"]
    st, r = req("POST", "/api/agent/confirm",
                {"tool_id": "pptx_optimize", "input": "精炼一点", "attachment_ids": att_ids}, headers=h)
    inv_id = json.loads(r)["invocation_id"]
    for i in range(20):
        time.sleep(2)
        st, r = req("GET", f"/api/agent/invocation/{inv_id}", headers=h)
        jj = json.loads(r)
        if jj["status"] in ("success", "failed"):
            break
    assert jj["status"] == "success", jj
    du = jj.get("download_url")
    print("download_url:", (du or "")[:100])
    assert du and ("Signature=" in du or "X-Amz-Signature" in du), "expected presigned url"
    # 容器内 S3 地址是 host.docker.internal，宿主机测试时改写为 127.0.0.1（生产为公网域名，无需处理）
    du = du.replace("host.docker.internal", "127.0.0.1")
    with urllib.request.urlopen(du, timeout=15) as resp:
        magic = resp.read(4)
    print("magic:", magic)
    assert magic == b"PK\x03\x04", "not a valid pptx"

    print("\n== 5. 限流：连续 35 次 recognize ==")
    codes = []
    for i in range(35):
        st, _ = req("POST", "/api/agent/recognize", {"text": f"测试限流{i}"})
        codes.append(st)
    from collections import Counter
    print(Counter(codes))
    assert 429 in codes, "expected 429"
    # 确认接口同限流代码路径（recognize 已证）；HTTP 突发受 60s 窗口跨越影响不稳定，
    # 这里直接对 allow() 做确定性单测：limit=5 -> 前 5 次放行，第 6 次拒绝
    from app.services.ratelimit import allow
    key = f"confirm:unittest:{int(time.time())}"
    results = [allow(key, limit=5) for _ in range(6)]
    print("allow x6 (limit=5):", results)
    assert results[:5] == [True] * 5 and results[5] is False, "ratelimit allow() broken"

    print("\nM5_ALL_OK")


if __name__ == "__main__":
    main()
