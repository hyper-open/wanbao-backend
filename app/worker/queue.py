import json

import redis

from app.core.config import settings

_pool = None
QUEUE = "wanbao:queue"


def _r():
    global _pool
    if _pool is None:
        _pool = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=10,
            retry_on_timeout=True,
        )
    return _pool


def enqueue(job: dict) -> None:
    _r().rpush(QUEUE, json.dumps(job, ensure_ascii=False))


def dequeue(timeout: int = 5):
    try:
        item = _r().blpop(QUEUE, timeout)
    except Exception:  # noqa: BLE001  连接抖动时让 worker 重试
        return None
    return json.loads(item[1]) if item else None
