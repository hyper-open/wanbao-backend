"""时区工具。

约定：库内所有时间统一以 **naive UTC** 存储（`utcnow()`），
对外序列化（to_dict / API）时统一转成 `APP_TZ` 本地时间展示，
用户在前端输入的时间也按 `APP_TZ` 解释后再转 UTC 存库/比较。

这样「存 UTC、显本地」的口径各处一致，不会再出现少 8 小时的问题。
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

# 中国大陆固定 UTC+8（无夏令时），不依赖系统 tzdata（容器里常常没有）。
APP_TZ = timezone(timedelta(hours=8))


def utcnow() -> datetime:
    """当前 UTC 时间（naive），与库内存储口径一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """naive-UTC -> naive-本地（用于展示/序列化）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(APP_TZ).replace(tzinfo=None)


def local_iso(dt: Optional[datetime]) -> Optional[str]:
    """naive-UTC -> 本地时间 ISO 字符串（如 2026-10-15T16:00:00）。None 原样返回。"""
    d = to_local(dt)
    return d.isoformat() if d else None


def local_str_to_utc(s: Optional[str]) -> Optional[datetime]:
    """解析前端输入的本地时间字符串（'YYYY-MM-DD' 或 ISO），转成 naive-UTC。

    带时区信息的输入按原时区换算；不带则视为 APP_TZ 本地时间。
    """
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).strip().replace("Z", ""))
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(tzinfo=APP_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def local_day_start_utc() -> datetime:
    """本地「今天 00:00」对应的 UTC（naive），用于按自然日统计。"""
    d = to_local(utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.replace(tzinfo=APP_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def local_month_start_utc() -> datetime:
    """本地「本月 1 号 00:00」对应的 UTC（naive），用于按自然月统计。"""
    d = to_local(utcnow()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return d.replace(tzinfo=APP_TZ).astimezone(timezone.utc).replace(tzinfo=None)
