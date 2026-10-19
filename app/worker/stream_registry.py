"""流式输出回调注册表（worker 进程内）。

worker 在跑一个 turn 前若开启流式，会把「增量发布回调」按 invocation_id 注册；
图内 produce 节点通过 ctx.invocation_id 取回回调，把 LLM 增量发布到 Redis。
进程内 dict：produce 与注册发生在同一 worker 进程（初始运行/恢复都走 run_session_step）。
"""
from __future__ import annotations

import json
import threading
from typing import Callable, Optional

import redis

from app.core.config import settings

_lock = threading.Lock()
_cbs: dict[int, Callable[[dict], None]] = {}

CHANNEL = "wanbao:stream:{sid}"


def register(invocation_id: int, sid: int, seq: int) -> None:
    def cb(event: dict) -> None:
        payload = {"seq": seq, "invocation_id": invocation_id, **event}
        try:
            r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
            r.publish(
                CHANNEL.format(sid=sid),
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001  发布失败不阻塞产出
            pass

    with _lock:
        _cbs[invocation_id] = cb


def get(invocation_id: int) -> Optional[Callable[[dict], None]]:
    return _cbs.get(invocation_id)


def pop(invocation_id: int) -> None:
    with _lock:
        _cbs.pop(invocation_id, None)
