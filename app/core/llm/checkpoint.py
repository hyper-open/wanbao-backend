"""LangGraph 检查点持久化：自建 MySQL saver。

为什么自建（而不是用官方 langgraph-checkpoint-sqlite / -postgres）：
1. 不引入新依赖（那两个包都需要额外安装，且引入构建/升级不确定性）；
2. 业务数据已在 MySQL，图状态同库便于审计、备份与「按会话清理」；
3. 本项目全程使用同步 `graph.invoke`，只需实现同步方法
   （`BaseCheckpointSaver` 并未强制抽象方法）。

语义对齐官方 `langgraph.checkpoint.memory.InMemorySaver`，仅替换存储介质：
- `lg_checkpoint`：检查点本体（去掉 channel_values）+ metadata + 父检查点
- `lg_checkpoint_blob`：各 channel 的值，按 (channel, version) 分片存储
- `lg_checkpoint_write`：待写（pending writes）

每次操作使用独立短连接（`SessionLocal`），不参与请求级事务。
"""
from __future__ import annotations

from typing import Any, Iterator, Optional, Sequence

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
    writes_sort_key,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import delete, select

from app.core.db import SessionLocal
from app.models import LangCheckpoint, LangCheckpointBlob, LangCheckpointWrite


class MySQLCheckpointSaver(BaseCheckpointSaver):
    """把 LangGraph 检查点存进 MySQL。thread_id 约定为 `str(tool_session.id)`。"""

    def __init__(self) -> None:
        super().__init__(serde=JsonPlusSerializer())

    # ---------------- 内部工具 ----------------
    @staticmethod
    def _loc(config: Optional[dict]) -> tuple[str, str]:
        c = (config or {}).get("configurable") or {}
        return str(c.get("thread_id") or ""), str(c.get("checkpoint_ns") or "")

    @staticmethod
    def _cid(config: Optional[dict]) -> Optional[str]:
        v = ((config or {}).get("configurable") or {}).get("checkpoint_id")
        return str(v) if v else None

    @staticmethod
    def _mkconfig(thread_id: str, checkpoint_ns: str, checkpoint_id: Optional[str]) -> dict:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def _load_blobs(self, db, thread_id: str, ns: str, versions: dict) -> dict:
        """按 channel_versions 取回各 channel 的值（与 InMemorySaver 同语义）。

        注意：只跳过显式占位的 `empty`。值为 None 的 channel（如挂起的
        `branch:to:*` 标记）序列化为 ("null", b"")，必须原样返回，
        否则 LangGraph 无法判断图已暂停在某个节点。
        """
        out: dict[str, Any] = {}
        for channel, version in (versions or {}).items():
            row = db.get(LangCheckpointBlob, (thread_id, ns, str(channel), str(version)))
            if row is None or row.type == "empty":
                continue
            out[channel] = self.serde.loads_typed((row.type, row.value))
        return out

    def _load_writes(self, db, thread_id: str, ns: str, checkpoint_id: str) -> list:
        """按 task_path/task_id/idx 排序返回 pending writes：(task_id, channel, value)。"""
        rows = (
            db.execute(
                select(LangCheckpointWrite)
                .where(
                    LangCheckpointWrite.thread_id == thread_id,
                    LangCheckpointWrite.checkpoint_ns == ns,
                    LangCheckpointWrite.checkpoint_id == checkpoint_id,
                )
            )
            .scalars()
            .all()
        )
        ordered = sorted(rows, key=lambda r: writes_sort_key(r.task_path or "", r.task_id, r.idx))
        return [
            (r.task_id, r.channel, self.serde.loads_typed((r.type, r.value)))
            for r in ordered
        ]

    # ---------------- 写 ----------------
    def put(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict:
        thread_id, ns = self._loc(config)
        cp = dict(checkpoint)
        values = cp.pop("channel_values", None) or {}
        parent_id = self._cid(config)
        checkpoint_id = str(cp["id"])
        db = SessionLocal()
        try:
            for channel, version in (new_versions or {}).items():
                key = (thread_id, ns, str(channel), str(version))
                if channel in values:
                    btype, blob = self.serde.dumps_typed(values[channel])
                else:
                    btype, blob = "empty", b""  # 该版本无值，显式占位
                row = db.get(LangCheckpointBlob, key)
                if row is None:
                    db.add(
                        LangCheckpointBlob(
                            thread_id=thread_id, checkpoint_ns=ns,
                            channel=str(channel), version=str(version),
                            type=btype, value=blob,
                        )
                    )
                else:
                    row.type, row.value = btype, blob

            ctype, cblob = self.serde.dumps_typed(cp)
            mtype, mblob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
            pk = (thread_id, ns, checkpoint_id)
            row = db.get(LangCheckpoint, pk)
            if row is None:
                db.add(
                    LangCheckpoint(
                        thread_id=thread_id, checkpoint_ns=ns, checkpoint_id=checkpoint_id,
                        parent_checkpoint_id=parent_id,
                        checkpoint_type=ctype, checkpoint=cblob,
                        metadata_type=mtype, meta_blob=mblob,
                    )
                )
            else:
                row.parent_checkpoint_id = parent_id
                row.checkpoint_type, row.checkpoint = ctype, cblob
                row.metadata_type, row.meta_blob = mtype, mblob
            db.commit()
        finally:
            db.close()
        return self._mkconfig(thread_id, ns, checkpoint_id)

    def put_writes(
        self,
        config: dict,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, ns = self._loc(config)
        checkpoint_id = self._cid(config) or ""
        db = SessionLocal()
        try:
            for idx, (channel, value) in enumerate(writes):
                widx = int(WRITES_IDX_MAP.get(channel, idx))
                pk = (thread_id, ns, checkpoint_id, str(task_id), widx)
                row = db.get(LangCheckpointWrite, pk)
                # 同语义：普通 channel 的写入已存在则跳过（幂等），特殊 channel（负数 idx）允许覆盖
                if row is not None and widx >= 0:
                    continue
                wtype, wblob = self.serde.dumps_typed(value)
                if row is None:
                    db.add(
                        LangCheckpointWrite(
                            thread_id=thread_id, checkpoint_ns=ns, checkpoint_id=checkpoint_id,
                            task_id=str(task_id), idx=widx,
                            channel=str(channel), type=wtype, value=wblob, task_path=task_path or "",
                        )
                    )
                else:
                    row.channel, row.type, row.value = str(channel), wtype, wblob
                    row.task_path = task_path or ""
            db.commit()
        finally:
            db.close()

    # ---------------- 读 ----------------
    def get_tuple(self, config: dict) -> Optional[CheckpointTuple]:
        thread_id, ns = self._loc(config)
        cid = self._cid(config)
        db = SessionLocal()
        try:
            if cid:
                row = db.get(LangCheckpoint, (thread_id, ns, cid))
            else:
                row = (
                    db.execute(
                        select(LangCheckpoint)
                        .where(
                            LangCheckpoint.thread_id == thread_id,
                            LangCheckpoint.checkpoint_ns == ns,
                        )
                        .order_by(LangCheckpoint.checkpoint_id.desc())
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
            if row is None:
                return None
            cp: Checkpoint = self.serde.loads_typed((row.checkpoint_type, row.checkpoint))
            cp["channel_values"] = self._load_blobs(
                db, row.thread_id, row.checkpoint_ns, cp.get("channel_versions") or {}
            )
            return CheckpointTuple(
                config=config if cid else self._mkconfig(thread_id, ns, row.checkpoint_id),
                checkpoint=cp,
                metadata=self.serde.loads_typed((row.metadata_type, row.meta_blob)),
                pending_writes=self._load_writes(db, row.thread_id, row.checkpoint_ns, row.checkpoint_id),
                parent_config=(
                    self._mkconfig(thread_id, ns, row.parent_checkpoint_id)
                    if row.parent_checkpoint_id
                    else None
                ),
            )
        finally:
            db.close()

    def list(
        self,
        config: Optional[dict],
        *,
        filter: Optional[dict] = None,
        before: Optional[dict] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        thread_id, ns = self._loc(config)
        cid = self._cid(config)
        before_id = self._cid(before)
        db = SessionLocal()
        try:
            q = select(LangCheckpoint).order_by(LangCheckpoint.checkpoint_id.desc())
            if config:
                q = q.where(LangCheckpoint.thread_id == thread_id)
                if ((config.get("configurable") or {}).get("checkpoint_ns") is not None):
                    q = q.where(LangCheckpoint.checkpoint_ns == ns)
            if cid:
                q = q.where(LangCheckpoint.checkpoint_id == cid)
            if before_id:
                q = q.where(LangCheckpoint.checkpoint_id < before_id)
            if limit:
                q = q.limit(limit)

            out: list[CheckpointTuple] = []
            for row in db.execute(q).scalars().all():
                md = self.serde.loads_typed((row.metadata_type, row.meta_blob))
                if filter and not all(md.get(k) == v for k, v in filter.items()):
                    continue
                cp: Checkpoint = self.serde.loads_typed((row.checkpoint_type, row.checkpoint))
                cp["channel_values"] = self._load_blobs(
                    db, row.thread_id, row.checkpoint_ns, cp.get("channel_versions") or {}
                )
                out.append(
                    CheckpointTuple(
                        config=self._mkconfig(row.thread_id, row.checkpoint_ns, row.checkpoint_id),
                        checkpoint=cp,
                        metadata=md,
                        pending_writes=self._load_writes(db, row.thread_id, row.checkpoint_ns, row.checkpoint_id),
                        parent_config=(
                            self._mkconfig(row.thread_id, row.checkpoint_ns, row.parent_checkpoint_id)
                            if row.parent_checkpoint_id
                            else None
                        ),
                    )
                )
            return iter(out)
        finally:
            db.close()

    # ---------------- 清理 ----------------
    def delete_thread(self, thread_id: str) -> None:
        """会话结束/过期时调用，避免 checkpoint 表无限增长。"""
        db = SessionLocal()
        try:
            for model in (LangCheckpoint, LangCheckpointBlob, LangCheckpointWrite):
                db.execute(delete(model).where(model.thread_id == thread_id))
            db.commit()
        finally:
            db.close()
