"""运行时键值开关（app_setting 表）：后台切换即时生效，无需重启容器。

- `get_flag` / `set_flag`：布尔开关（如 stream_enabled 流式输出）
- 读取失败（表不存在/DB 抖动）按默认值处理，不阻塞主链路
- worker 每个任务开始时读一次；API 每次请求读一次（单行主键查询，开销可忽略）
"""
from __future__ import annotations

from app.core.db import SessionLocal
from app.models import AppSetting

STREAM_ENABLED = "stream_enabled"
DISCLAIMER_ENABLED = "disclaimer_enabled"


_FLAG_CACHE: dict = {}


def get_flag_cached(key: str, default: bool = False, ttl: int = 30) -> bool:
    """带进程内 TTL 缓存的开关读取（高频调用路径避免逐次查库；后台切换 ttl 秒内生效）。"""
    import time

    c = _FLAG_CACHE.get(key)
    now = time.time()
    if c and now - c[1] < ttl:
        return c[0]
    v = get_flag(key, default)
    _FLAG_CACHE[key] = (v, now)
    return v


def get_flag(key: str, default: bool = False) -> bool:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        if not row or row.value is None:
            return default
        return row.value.strip().lower() in ("1", "true", "on", "yes")
    except Exception:  # noqa: BLE001
        return default
    finally:
        db.close()


def get_float(key: str, default: float) -> float:
    """动态浮点参数（如成本闸元数）；未设置/非法值回落默认。"""
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        if not row or row.value is None:
            return default
        return float(row.value)
    except Exception:  # noqa: BLE001
        return default
    finally:
        db.close()


def get_num(key: str, default: int) -> int:
    """动态数值参数（如限流阈值）；未设置/非法值回落默认。"""
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        if not row or row.value is None:
            return default
        return int(float(row.value))
    except Exception:  # noqa: BLE001
        return default
    finally:
        db.close()


def set_num(key: str, value: int) -> None:
    import sqlalchemy.exc

    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        if row:
            row.value = str(int(value))
        else:
            db.add(AppSetting(key=key, value=str(int(value))))
        db.commit()
    except sqlalchemy.exc.OperationalError:
        db.rollback()
    finally:
        db.close()


def get_str(key: str, default: str = "") -> str:
    """字符串配置（如虚拟支付 Token/AESKey）；未设置回落 default。"""
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        if not row or row.value is None:
            return default
        return row.value
    except Exception:  # noqa: BLE001
        return default
    finally:
        db.close()


def set_str(key: str, value: str) -> None:
    import sqlalchemy.exc

    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        db.commit()
    except sqlalchemy.exc.OperationalError:
        db.rollback()
    finally:
        db.close()


def del_str(key: str) -> None:
    """删除配置项（后台清空 = 回落 .env 默认）。"""
    import sqlalchemy.exc

    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        if row:
            db.delete(row)
            db.commit()
    except sqlalchemy.exc.OperationalError:
        db.rollback()
    finally:
        db.close()


def set_flag(key: str, value: bool) -> None:
    import sqlalchemy.exc

    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        if row:
            row.value = "true" if value else "false"
        else:
            db.add(AppSetting(key=key, value="true" if value else "false"))
        db.commit()
    except sqlalchemy.exc.OperationalError:
        # 表还没建（老容器首次启动竞态）：下一轮迁移会建
        db.rollback()
    finally:
        db.close()
