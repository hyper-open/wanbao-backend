"""内容安全（M4 全链路）。

设计要点：
- 可插拔 provider：默认 `mock`（演示用关键词拦截），预留微信 msgSecCheck 真实路径。
- 覆盖三层：用户输入(input) / 工具输出(output) / 附件文本(attachment)。
- 每次筛查都写入 AuditLog（通过/命中均留痕）。
- fail-closed：审核服务异常时拒绝而非放行。

调用方约定：
- `screen(text, stage, db, user_id) -> bool`，命中返回 False（不抛异常）。
- 便捷函数 `check_input/check_output/check_attachment` 在命中时抛 `ContentRejected`。
"""
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog
from app.services.auth import _wxa_access_token

# 演示用最小拦截词表；生产应替换为微信/第三方审核服务
_BLOCKLIST = ["违禁词示例A", "违禁词示例B", "违规内容demo"]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _audit(db: Optional[Session], user_id, stage: str, text: str, passed: bool, reason: str):
    if db is None:
        return
    try:
        db.add(
            AuditLog(
                user_id=user_id,
                stage=stage,
                passed=passed,
                reason=reason,
                sample=(text or "")[:200],
                created_at=_now(),
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001  审计写入失败不应阻断主流程（仅告警）
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


class ContentRejected(Exception):
    """内容命中安全策略。调用方应转换为 400 并在异步场景退次。"""

    def __init__(self, stage: str, reason: str = ""):
        self.stage = stage
        self.reason = reason
        super().__init__(f"content rejected at {stage}: {reason}")


def _mock_sec_check(text: str) -> tuple[bool, str]:
    for w in _BLOCKLIST:
        if w and w in text:
            return False, f"blocklist:{w}"
    return True, ""


def _infra_fallback(text: str, reason: str) -> tuple[bool, str]:
    """基础设施故障（配额耗尽/token/网络）≠ 内容风险 → 降级本地词表，避免全站输入被拒。

    内容侧仍宁拒勿放：真实判定为 risky（87014 / v2 suggest）不走这里。
    """
    try:
        from app.services.ratelimit import _alert

        _alert("sec_check_infra", f"内容安全接口故障，已降级本地词表筛查：{reason[:120]}")
    except Exception:  # noqa: BLE001
        pass
    ok, mock_reason = _mock_sec_check(text)
    return ok, ("" if ok else f"mock:{mock_reason}（degraded:{reason}）")


def _wechat_sec_check(text: str, openid: Optional[str] = None, appid: str = None, secret: str = None, mock: bool = False) -> tuple[bool, str]:
    """微信 msgSecCheck 真实审核。

    优先 v2（`version=2` + `scene`，需要该用户的真实 openid，判定更细：
    result.suggest = pass/review/risky）；拿不到真实 openid 时回落 v1（无需 openid，
    命中返回 errcode 87014）。v2 若因 openid 无效等原因失败，也回落 v1。

    异常一律 fail-closed（拒绝而非放行）：安全侧宁拒勿放，reason 里保留原因便于排查。
    """
    if mock or not (appid and secret):
        return _mock_sec_check(text)  # 未配置微信 → 退回本地词表
    try:
        token = _wxa_access_token(appid, secret)
    except Exception as e:  # noqa: BLE001
        return _infra_fallback(text, f"sec_check_token_error:{e}")

    url = "https://api.weixin.qq.com/wxa/msg_sec_check"
    try:
        # --- v2：有真实 openid 时优先，判定更细 ---
        if openid and not openid.startswith("mock_"):
            r = httpx.post(
                url,
                params={"access_token": token},
                json={"content": text, "version": 2, "scene": 2, "openid": openid},
                timeout=8,
            )
            d = r.json()
            if d.get("errcode") == 0:
                res = d.get("result") or {}
                if res.get("suggest") == "pass":
                    return True, ""
                return False, f"wechat_v2:{res.get('suggest')}:label={res.get('label')}"
            # v2 异常（如 openid 无效）→ 继续尝试 v1，避免把用户全拒掉

        # --- v1 回退：无需 openid ---
        r = httpx.post(url, params={"access_token": token}, json={"content": text}, timeout=8)
        d = r.json()
        ec = d.get("errcode")
        if ec == 0:
            return True, ""
        if ec == 87014:
            return False, "wechat_v1:risky"
        return _infra_fallback(text, f"sec_check_error:{ec}:{d.get('errmsg')}")
    except Exception as e:  # noqa: BLE001
        return _infra_fallback(text, f"sec_check_error:{e}")


def _openid_of(db: Optional[Session], user_id) -> Optional[str]:
    """取用户的微信 openid（msgSecCheck v2 需要）。

    以 user_identity（channel='wechat' 的 channel_uid）为准 —— 与 auth.bind_phone
    的口径一致，多渠道下也正确；兼容回退到 User.openid（历史数据）。
    非微信身份返回 None，上层会回落到无需 openid 的 v1。
    """
    if db is None or not user_id:
        return None
    try:
        from app.models import User, UserIdentity

        row = (
            db.query(UserIdentity.channel_uid)
            .filter(UserIdentity.user_id == user_id, UserIdentity.channel == "wechat")
            .first()
        )
        if row and row[0]:
            return row[0]
        u = db.get(User, user_id)
        return (u.openid or None) if u else None
    except Exception:  # noqa: BLE001
        return None


_CS_PROVIDER: dict = {"v": None, "at": 0.0}


def _provider_name() -> str:
    """内容安全 provider（后台可配 mock/wechat；app_setting 优先，30s 缓存）。"""
    import time

    if time.time() - _CS_PROVIDER["at"] > 30:
        from app.services import appsettings

        _CS_PROVIDER["v"] = (
            appsettings.get_str("CONTENT_SECURITY_PROVIDER")
            or settings.CONTENT_SECURITY_PROVIDER
            or "mock"
        ).lower()
        _CS_PROVIDER["at"] = time.time()
    return _CS_PROVIDER["v"]


def screen(
    text: str,
    *,
    stage: str = "input",
    db: Optional[Session] = None,
    user_id=None,
) -> bool:
    """对一段文本做安全筛查，返回是否通过。命中写入审计并返回 False。"""
    if not text:
        return True
    provider = _provider_name()
    if provider == "wechat":
        oid = _openid_of(db, user_id)
        appid = secret = None
        mock = False  # 匿名请求（无用户身份）时回落本地词表，避免 UnboundLocalError
        if db is not None and oid:
            from app.services.auth import account_creds_for

            appid, secret, mock = account_creds_for(db, oid)
        passed, reason = _wechat_sec_check(text, openid=oid, appid=appid, secret=secret, mock=mock)
    else:
        passed, reason = _mock_sec_check(text)
    _audit(db, user_id, stage, text, passed, reason)
    return passed


def check_text(text: str) -> bool:
    """向后兼容：仅做输入层筛查，不写审计、不抛异常。"""
    return screen(text, stage="input")


def check_input(text: str, *, db: Optional[Session] = None, user_id=None) -> None:
    if not screen(text, stage="input", db=db, user_id=user_id):
        raise ContentRejected("input")


def check_output(text: str, *, db: Optional[Session] = None, user_id=None) -> None:
    if not screen(text, stage="output", db=db, user_id=user_id):
        raise ContentRejected("output")


def check_attachment(text: str, *, db: Optional[Session] = None, user_id=None) -> None:
    if not screen(text, stage="attachment", db=db, user_id=user_id):
        raise ContentRejected("attachment")
