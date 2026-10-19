"""简单限流（Redis 固定窗口计数，跨进程共享）。

- Redis 不可用时放行（不阻塞主链路），仅记录告警（webhook 冷却防刷屏）。
- 每次拒绝按类别写监控指标：m:deny:{类别}:{分钟}（近 2h）/ m:deny:{类别}:today（当日）。
"""
import time

from app.core.config import settings


def _alert(kind: str, message: str) -> None:
    """告警：POST 到 ALERT_WEBHOOK_URL（钉钉/飞书/企微自定义机器人通用 text 结构）。

    同类告警 10 分钟冷却，防刷屏；未配置 webhook 或发送失败静默。
    """
    try:
        import json as _json
        import urllib.request

        import redis

        from app.services import appsettings

        url = appsettings.get_str("ALERT_WEBHOOK_URL") or settings.ALERT_WEBHOOK_URL
        if not url:
            return
        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=1)
        cool = f"alert:cool:{kind}"
        if not r.set(cool, "1", nx=True, ex=600):
            return  # 冷却期内
        body = _json.dumps({"text": f"[万宝] {message}"}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
    except Exception as e:  # noqa: BLE001
        print(f"[alert] {kind}: {e}")


def _deny_metric(category: str, r, now: int) -> None:
    """拒绝指标：按类别记「当前分钟」与「当日累计」两个计数；近 10 分钟超 100 次告警。"""
    try:
        minute = now // 60
        p = r.pipeline()
        p.incr(f"m:deny:{category}:{minute}")
        p.expire(f"m:deny:{category}:{minute}", 7200)
        p.incr(f"m:deny:{category}:today")
        p.expire(f"m:deny:{category}:today", 172800)
        p.execute()
        # 突增告警：近 10 分钟拒绝次数超阈值（默认 100）
        last10 = sum(int(r.get(f"m:deny:{category}:{minute - i}") or 0) for i in range(10))
        if last10 >= 100:
            _alert("rl_spike", f"限流突增：{category} 近 10 分钟触发 {last10} 次（可能被攻击或阈值过低）")
    except Exception:  # noqa: BLE001
        pass


def allow(key: str, limit: int | None = None, window_seconds: int = 60, category: str | None = None) -> bool:
    try:
        import redis

        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=1)
        now = int(time.time())
        bucket = f"rl:{key}:{now // window_seconds}"
        cnt = r.incr(bucket)
        if cnt == 1:
            r.expire(bucket, window_seconds * 2)
        if cnt <= (limit or settings.RATE_LIMIT_PER_MIN):
            return True
        if category:
            _deny_metric(category, r, now)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[ratelimit] degraded, allow: {e}")
        _alert("redis_down", "Redis 不可用，限流已降级放行（所有阈值失效）")
        return True


def global_daily_cost_add(amount_yuan: float, limit_yuan: float = 0.0) -> None:
    """全站当日 LLM 成本累计（Redis INCRBYFLOAT，按自然日键，2 天过期）。

    供 worker 在产出落库后调用；达到阈值 80% 时触发告警（10 分钟冷却）。
    """
    if amount_yuan <= 0:
        return
    try:
        import redis

        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=1)
        day = time.strftime("%Y%m%d")
        key = f"cost:global:{day}"
        total = float(r.incrbyfloat(key, amount_yuan))
        r.expire(key, 172800)  # 2 天
        if limit_yuan > 0 and total >= limit_yuan * 0.8:
            _alert("global_cost", f"全站今日 LLM 成本已达 {total:.2f}/{limit_yuan} 元（≥80%）")
    except Exception as e:  # noqa: BLE001
        print(f"[ratelimit] global cost add failed: {e}")


def global_daily_cost_get() -> float:
    try:
        import redis

        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=1)
        day = time.strftime("%Y%m%d")
        return float(r.get(f"cost:global:{day}") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0
