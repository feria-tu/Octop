"""Checkpoint writes and execution fences share the control-plane transaction."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, CheckpointTuple

from octop.infra.db.repos.runs import RunConflict
from octop.infra.runs.service import RunService, conflict, execution_context
from octop.infra.users.enterprise import db_call


class FencedSaver(BaseCheckpointSaver[Any]):
    def __init__(self, legacy: Any, service: RunService, agent_id: str) -> None:
        super().__init__(serde=legacy.serde)
        self.legacy = legacy
        self.service = service
        self.agent_id = agent_id

    @property
    def conn(self) -> Any:
        return self.legacy.conn

    async def setup(self) -> None:
        setup = getattr(self.legacy, "setup", None)
        if setup is not None:
            await setup()

    def get_next_version(self, current: Any, channel: None) -> Any:
        return self.legacy.get_next_version(current, channel)

    def _dump(self, value: Any) -> tuple[str, str]:
        kind, data = self.serde.dumps_typed(value)
        return kind, base64.b64encode(data).decode("ascii")

    def _load(self, kind: str, data: str) -> Any:
        return self.serde.loads_typed((kind, base64.b64decode(data)))

    def _identity(self, config: Any) -> tuple[str, str, str | None]:
        cfg = config.get("configurable") or {}
        return str(cfg["thread_id"]), str(cfg.get("checkpoint_ns") or ""), cfg.get("checkpoint_id")

    async def _execution(self, config: Any) -> Any:
        ctx = execution_context.get()
        if (
            ctx is None
            or ctx.row.agent_id != self.agent_id
            or self._identity(config)[0] != ctx.row.thread_id
        ):
            raise conflict(RunConflict("checkpoint_scope"))
        await ctx.validate()
        return ctx

    async def aget_tuple(self, config: Any) -> CheckpointTuple | None:
        thread, namespace, checkpoint_id = self._identity(config)
        data = await db_call(
            self.service.repo.checkpoint_get, self.agent_id, thread, namespace, checkpoint_id
        )
        if data is None:
            # Existing workspaces retain their pre-v22 conversation history.
            original = await self.legacy.aget_tuple(config)
            ctx = execution_context.get()
            if original is not None and ctx is not None:
                await self._execution(config)
                legacy_parent = (
                    self._identity(original.parent_config)[2] if original.parent_config else None
                )
                await db_call(
                    self.service.repo.checkpoint_put,
                    ctx.row.run_id,
                    self.service.node,
                    ctx.row.fence,
                    namespace,
                    original.checkpoint["id"],
                    legacy_parent,
                    self._dump(original.checkpoint),
                    self._dump(original.metadata),
                    checkpoint_id is None,
                )
                grouped: dict[str, list[tuple[str, Any]]] = {}
                for task, channel, value in original.pending_writes or []:
                    grouped.setdefault(task, []).append((channel, value))
                for task, writes in grouped.items():
                    await self.aput_writes(original.config, writes, task)
            return cast(CheckpointTuple | None, original)
        saved: Any = {
            "configurable": {
                "thread_id": thread,
                "checkpoint_ns": namespace,
                "checkpoint_id": data["checkpoint_id"],
            }
        }
        parent: Any = (
            {"configurable": {**saved["configurable"], "checkpoint_id": data["parent_id"]}}
            if data["parent_id"]
            else None
        )
        pending = [
            (w["task_id"], w["channel"], self._load(w["value_type"], w["value_data"]))
            for w in data["writes"]
        ]
        return CheckpointTuple(
            saved,
            self._load(data["checkpoint_type"], data["checkpoint_data"]),
            self._load(data["metadata_type"], data["metadata_data"]),
            parent,
            pending,
        )

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        ctx = await self._execution(config)
        thread, namespace, parent = self._identity(config)
        try:
            await db_call(
                self.service.repo.checkpoint_put,
                ctx.row.run_id,
                self.service.node,
                ctx.row.fence,
                namespace,
                checkpoint["id"],
                parent,
                self._dump(checkpoint),
                self._dump(metadata),
            )
        except RunConflict as exc:
            raise conflict(exc) from exc
        return {
            "configurable": {
                "thread_id": thread,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        ctx = await self._execution(config)
        _, namespace, checkpoint_id = self._identity(config)
        if checkpoint_id is None:
            raise conflict(RunConflict("checkpoint_id_missing"))
        data = [
            (WRITES_IDX_MAP.get(channel, index), channel, *self._dump(value))
            for index, (channel, value) in enumerate(writes)
        ]
        try:
            await db_call(
                self.service.repo.checkpoint_writes,
                ctx.row.run_id,
                self.service.node,
                ctx.row.fence,
                namespace,
                checkpoint_id,
                task_id,
                task_path,
                data,
            )
        except RunConflict as exc:
            raise conflict(exc) from exc

    async def alist(
        self,
        config: Any,
        *,
        filter: dict[str, Any] | None = None,
        before: Any = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        thread, namespace, _ = self._identity(config)
        ids = await db_call(
            self.service.repo.checkpoint_list,
            self.agent_id,
            thread,
            namespace,
            self._identity(before)[2] if before else None,
            limit or 100,
        )
        for checkpoint_id in ids:
            item = await self.aget_tuple(
                {
                    "configurable": {
                        "thread_id": thread,
                        "checkpoint_ns": namespace,
                        "checkpoint_id": checkpoint_id,
                    }
                }
            )
            if item is not None and all(
                item.metadata.get(k) == v for k, v in (filter or {}).items()
            ):
                yield item
        if not ids:
            async for item in self.legacy.alist(config, filter=filter, before=before, limit=limit):
                yield item

    async def adelete_thread(self, thread_id: str) -> None:
        await db_call(self.service.repo.checkpoint_delete, self.agent_id, thread_id)
        await self.legacy.adelete_thread(thread_id)
