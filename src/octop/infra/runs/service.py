"""Durable execution at the AgentManager boundary."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from octop.infra.db.repos.runs import RunConflict as RunConflict
from octop.infra.db.repos.runs import RunRow as RunRow
from octop.infra.db.repos.runs import encode
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.enterprise import db_call
from octop.infra.users.enterprise_access import agent_access_policy
from octop.infra.users.identity import User
from octop.infra.utils.ulid import new_ulid


def wire(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return wire(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): wire(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [wire(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return wire(asdict(value))
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    raise TypeError(f"Unsupported durable payload: {type(value).__name__}")


def conflict(exc: RunConflict) -> OctopError:
    return OctopError.localized(ErrorCode.RUN_CONFLICT, status=409)


def definition_version(row: Any) -> str:
    data = {
        key: getattr(row, key)
        for key in (
            "config_json",
            "system_prompt",
            "default_model",
            "definition_version",
            "knowledge_base_ids",
            "mcp_servers",
            "skill_package_ids",
            "enterprise_kind",
            "department_id",
            "user_id",
            "is_shared",
        )
    }
    return hashlib.sha256(encode(data).encode()).hexdigest()


@dataclass
class Execution:
    service: RunService
    row: RunRow
    write_lock: asyncio.Lock
    resuming_approval: bool = False

    async def validate(self) -> None:
        await self.service.validate(self.row)


execution_context: ContextVar[Execution | None] = ContextVar("octop_run", default=None)


class RunService:
    def __init__(self, repos: RepoBundle) -> None:
        self.repos = repos
        self.repo = repos.run_repo
        self.node = new_ulid()
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.background: set[asyncio.Task[None]] = set()

    async def version(self, agent_id: str, user_id: int) -> str:
        row = await db_call(self.repos.agent_repo.get, agent_id)
        user = await db_call(self.repos.user_repo.get, user_id)
        if row is None or user is None or user.disabled or not row.enabled:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if row.enterprise_kind == "runtime":
            bindings = await db_call(self.repos.enterprise_repo.runtimes, user_id=user_id)
            binding = next((r for r in bindings if r.runtime_id == agent_id), None)
            definition = (
                await db_call(self.repos.agent_repo.get, binding.definition_id) if binding else None
            )
            if (
                binding is None
                or definition is None
                or not definition.enabled
                or user.department_id != definition.department_id
                or binding.definition_version != definition.definition_version
            ):
                raise OctopError.localized(ErrorCode.FORBIDDEN)
        elif (
            row.user_id is not None
            and not agent_access_policy(
                row,
                User(
                    id=user.id,
                    username=user.username,
                    role=user.role,
                    display_name=user.display_name,
                    department_id=user.department_id,
                ),
            ).can_use
        ):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        data = {"definition": definition_version(row), "user_department": user.department_id}
        return hashlib.sha256(encode(data).encode()).hexdigest()

    async def validate(self, row: RunRow) -> None:
        version = await self.version(row.agent_id, row.user_id)
        try:
            await db_call(self.repo.assert_live, row.run_id, self.node, row.fence, version)
        except RunConflict as exc:
            raise conflict(exc) from exc

    async def owned(self, run_id: str, user_id: int, admin: bool = False) -> RunRow:
        await db_call(self.repo.expire)
        row = await db_call(self.repo.get, run_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if row.user_id != user_id and not admin:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if not admin:
            await self.version(row.agent_id, row.user_id)
        return row

    async def inspect_admin(self, run_id: str, user_id: int) -> RunRow:
        actor = await db_call(self.repos.user_repo.get, user_id)
        if actor is None or actor.disabled or actor.role != "admin":
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        row = await self.owned(run_id, user_id, admin=True)
        await db_call(
            self.repos.audit_repo.write,
            actor=actor.username,
            action="run.admin_view",
            target=run_id,
        )
        return row

    async def cancel(self, agent_id: str, thread_id: str) -> None:
        row = await db_call(self.repo.active, agent_id, thread_id)
        if row is not None:
            await self.cancel_id(row.run_id, row.user_id)

    async def cancel_id(self, run_id: str, user_id: int) -> RunRow:
        row = await db_call(self.repo.cancel, run_id, user_id)
        task = self.tasks.get(run_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        return row

    async def _heartbeat(self, row: RunRow, task: asyncio.Task[Any] | None) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.validate(row)
                alive = await db_call(self.repo.heartbeat, row.run_id, self.node, row.fence)
                if not alive:
                    raise RunConflict("lease_lost")
            except Exception:
                if task is not None:
                    task.cancel()
                return

    async def stream(
        self,
        agent_id: str,
        request: dict[str, Any],
        source: Callable[[], AsyncIterator[Any]],
        *,
        resume_run_id: str | None = None,
        expose_done: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        await db_call(self.repo.expire)
        thread_id = str(request.get("thread_id") or "")
        raw_user = request.get("user") or (request.get("configurable") or {}).get("user")
        thread = await db_call(self.repos.thread_repo.get, thread_id)
        if thread is None:
            # Internal harness sessions also need a durable, owned thread.
            agent = await db_call(self.repos.agent_repo.get, agent_id)
            uid = int(raw_user) if raw_user is not None else (agent.user_id if agent else None)
            if uid is None:
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            if not thread_id:
                thread_id = new_ulid()
                request["thread_id"] = thread_id
            await db_call(
                self.repos.thread_repo.insert,
                thread_id=thread_id,
                agent_id=agent_id,
                user_id=uid,
                channel_type=str(request.get("source") or "internal"),
                session_key=f"run:{agent_id}:{thread_id}",
            )
        else:
            uid = int(raw_user) if raw_user is not None else thread.user_id
        version = await self.version(agent_id, uid)
        try:
            if resume_run_id is None:
                row = await db_call(
                    self.repo.create,
                    new_ulid(),
                    agent_id,
                    thread_id,
                    uid,
                    version,
                    encode(wire(request)),
                )
            else:
                previous = await db_call(self.repo.get, resume_run_id)
                if (
                    previous is None
                    or previous.agent_id != agent_id
                    or previous.thread_id != thread_id
                    or previous.user_id != uid
                    or previous.version != version
                ):
                    raise RunConflict("resume_identity")
                row = previous
            row = await db_call(self.repo.claim, row.run_id, self.node)
        except RunConflict as exc:
            raise conflict(exc) from exc
        ctx = Execution(
            self,
            row,
            asyncio.Lock(),
            resume_run_id is not None
            and previous is not None
            and previous.state == "waiting_approval",
        )
        token = execution_context.set(ctx)
        task = asyncio.current_task()
        if task is not None:
            self.tasks[row.run_id] = task
        heartbeat = asyncio.create_task(self._heartbeat(row, asyncio.current_task()))
        paused = False
        committed = False
        result: dict[str, Any] = json.loads(row.result_json) if row.result_json else {"text": ""}
        result.pop("partial", None)
        result.pop("error", None)
        pending_requests: list[dict[str, Any]] = []
        try:
            yield await db_call(
                self.repo.append_event,
                row.run_id,
                self.node,
                row.fence,
                {"type": "run_started", "version": version, "fence": row.fence},
            )
            async for raw in source():
                await ctx.validate()
                chunk = wire(raw)
                if not isinstance(chunk, dict):
                    raise TypeError("Harness stream chunk must be a mapping")
                if chunk.get("type") == "done":
                    continue
                if chunk.get("type") in {"token", "text"}:
                    result["text"] += str(chunk.get("content") or chunk.get("text") or "")
                if chunk.get("type") == "state_snapshot":
                    result["state"] = chunk.get("data")
                if chunk.get("type") == "error":
                    result["error"] = chunk
                if chunk.get("type") == "hitl_required":
                    from octop.infra.gateway.hitl.format import normalize_hitl_request

                    pending_requests.append(normalize_hitl_request(chunk.get("request")))
                    paused = True
                    continue
                persisted = await db_call(
                    self.repo.append_event, row.run_id, self.node, row.fence, chunk
                )
                yield {
                    **raw,
                    "run_id": row.run_id,
                    "seq": persisted["seq"],
                    "thread_id": row.thread_id,
                }
            await ctx.validate()
            if paused:
                combined: dict[str, list[Any]] = {"action_requests": [], "review_configs": []}
                for pending in pending_requests:
                    combined["action_requests"].extend(pending.get("action_requests") or [])
                    combined["review_configs"].extend(pending.get("review_configs") or [])
                event = await db_call(
                    self.repo.append_event,
                    row.run_id,
                    self.node,
                    row.fence,
                    {
                        "type": "hitl_required",
                        "request": {
                            **combined,
                            "run_id": row.run_id,
                            "pending_id": f"run:{row.run_id}",
                        },
                    },
                )
                await db_call(
                    self.repo.pause,
                    row.run_id,
                    self.node,
                    row.fence,
                    result={**result, "partial": True},
                )
                yield event
            else:
                done = await db_call(
                    self.repo.commit,
                    row.run_id,
                    self.node,
                    row.fence,
                    result,
                    "failed" if "error" in result else "succeeded",
                )
                committed = True
                if expose_done:
                    yield done
        except BaseException:
            await db_call(
                self.repo.record_partial,
                row.run_id,
                self.node,
                row.fence,
                {**result, "partial": True},
            )
            current = await db_call(self.repo.get, row.run_id)
            if current is not None and current.state == "running":
                with contextlib.suppress(RunConflict):
                    await db_call(
                        self.repo.commit,
                        row.run_id,
                        self.node,
                        row.fence,
                        {**result, "partial": True},
                        "failed",
                    )
            raise
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            execution_context.reset(token)
            if self.tasks.get(row.run_id) is task:
                self.tasks.pop(row.run_id, None)
            if not paused and not committed:
                await db_call(self.repo.expire)

    async def close(self) -> None:
        tasks = list(self.background)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def resume_events(
        self,
        manager: Any,
        run_id: str,
        user_id: int,
        decisions: list[dict[str, Any]],
        publish: Callable[[dict[str, Any]], Any],
    ) -> AsyncIterator[dict[str, Any]]:
        row = await self.owned(run_id, user_id)
        after = row.next_seq
        errors: list[BaseException] = []

        async def execute() -> None:
            try:
                async for event in self.resume(manager, run_id, user_id, decisions):
                    await publish(event)
            except BaseException as exc:
                errors.append(exc)

        task = asyncio.create_task(execute())
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        # The observer can disconnect without cancelling the execution task.
        while True:
            await self.owned(run_id, user_id)
            events = await db_call(self.repo.events, run_id, after, 500)
            for event in events:
                after = event["seq"]
                yield event
            if task.done():
                # Re-read after completion to include a commit racing the preceding query.
                tail = await db_call(self.repo.events, run_id, after, 500)
                if tail:
                    for event in tail:
                        after = event["seq"]
                        yield event
                    continue
                if errors:
                    exc = errors[0]
                    if isinstance(exc, OctopError):
                        yield {"type": "error", "error_code": exc.code.value, "message": str(exc)}
                    else:
                        yield {"type": "error", "error_code": "RUN_CONFLICT"}
                return
            await asyncio.sleep(0.1)

    async def checkpoint_edit(
        self, agent_id: str, thread_id: str, operation: Callable[[], Any]
    ) -> Any:
        context = execution_context.get()
        if context is not None:
            if context.row.agent_id != agent_id or context.row.thread_id != thread_id:
                raise conflict(RunConflict("checkpoint_scope"))
            await context.validate()
            return await operation()
        thread = await db_call(self.repos.thread_repo.get, thread_id)
        if thread is None or thread.agent_id != agent_id:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        result: Any = None

        async def source() -> AsyncIterator[Any]:
            nonlocal result
            result = await operation()
            yield {"type": "state_snapshot", "data": {"checkpoint_edit": True}}

        async for _event in self.stream(
            agent_id,
            {"thread_id": thread_id, "user": str(thread.user_id), "source": "checkpoint_edit"},
            source,
        ):
            pass
        return result

    async def resume(
        self, manager: Any, run_id: str, user_id: int, decisions: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        row = await self.owned(run_id, user_id)
        latest = await db_call(self.repo.latest, row.agent_id, row.thread_id)
        if latest is None or latest.run_id != row.run_id:
            raise OctopError.localized(ErrorCode.RUN_CONFLICT, status=409)
        if row.state not in {"queued", "waiting_approval", "recovery_blocked", "failed"}:
            raise OctopError.localized(ErrorCode.RUN_CONFLICT, status=409)
        if row.state == "waiting_approval":
            async for chunk in manager.resume_hitl(row.agent_id, row.thread_id, decisions or []):
                yield chunk
            terminal = await db_call(self.repo.terminal_event, row.run_id)
            if terminal is not None:
                yield terminal
            return
        async with (
            manager._thread_execution_lock(row.agent_id, row.thread_id),
            manager._track_invocation(row.agent_id),
        ):
            manager._bind_run_checkpointer(row.agent_id)
            agent = manager.get_agent(row.agent_id)
            graph = agent.graph
            request = json.loads(row.request_json)
            if row.state == "queued":
                async for chunk in self.stream(
                    row.agent_id,
                    request,
                    lambda: manager._harness_manager.stream(row.agent_id, request),
                    resume_run_id=row.run_id,
                    expose_done=True,
                ):
                    yield chunk
                return
            if not agent.checkpointer:
                raise OctopError.localized(ErrorCode.RUN_CONFLICT, status=409)
            config = self.resume_config(request, row)
            snapshot = await graph.aget_state(config)
            if not snapshot or not snapshot.next:
                raise OctopError.localized(ErrorCode.RUN_CONFLICT, status=409)
            from octop_harness.protocols.langgraph import LangGraphProtocol
            from octop_harness.protocols.think_splitter import ThinkSplitter

            protocol = LangGraphProtocol(graph=graph)

            async def source() -> AsyncIterator[Any]:
                splitter = ThinkSplitter()
                async for raw in graph.astream(
                    None,
                    config=config,
                    stream_mode=["values", "updates", "messages", "custom"],
                    version="v2",
                ):
                    for chunk in protocol._project_chunk(raw, splitter):
                        yield chunk
                for chunk in protocol._drain_splitter(splitter):
                    yield chunk

            async for chunk in self.stream(
                row.agent_id,
                request,
                source,
                resume_run_id=row.run_id,
                expose_done=True,
            ):
                yield chunk

    @staticmethod
    def resume_config(request: dict[str, Any], row: RunRow) -> dict[str, Any]:
        from octop_harness.request import ChatRequest

        config: dict[str, Any] = ChatRequest.coerce(request).to_runnable_config()
        config["configurable"] = {
            **config.get("configurable", {}),
            "thread_id": row.thread_id,
            "user": str(row.user_id),
        }
        return config

    async def resume_source(
        self, protocol: Any, thread_id: str, decisions: list[dict[str, Any]], config: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        from langgraph.types import Command
        from octop_harness.protocols.think_splitter import ThinkSplitter

        from octop.infra.gateway.hitl.format import normalize_hitl_request

        ctx = execution_context.get()
        if ctx is None or ctx.row.thread_id != thread_id:
            raise conflict(RunConflict("resume_scope"))
        snapshot = await protocol._graph.aget_state(config)
        interrupts = [item for task in snapshot.tasks for item in task.interrupts]
        if not interrupts:
            raise conflict(RunConflict("interrupt_missing"))
        stored = {a["approval_id"]: a for a in await db_call(self.repo.approvals, ctx.row.run_id)}
        if (
            decisions
            and len(decisions) != len(interrupts)
            and not (
                len(interrupts) == 1
                and not normalize_hitl_request(interrupts[0].value).get("approval_id")
            )
        ):
            raise conflict(RunConflict("decision_count"))
        mapped = {}
        for index, item in enumerate(interrupts):
            value = normalize_hitl_request(item.value)
            approval = stored.get(value.get("approval_id"))
            if len(interrupts) == 1 and approval is None:
                mapped[item.id] = {"decisions": decisions}
                continue
            if decisions:
                bound = next(
                    (
                        d
                        for d in decisions
                        if approval and d.get("approval_id") == approval["approval_id"]
                    ),
                    None,
                )
                decision = dict(bound if bound is not None else decisions[index])
            elif approval and approval["state"] in {"approved", "rejected"}:
                decision = {"type": "approve" if approval["state"] == "approved" else "reject"}
            else:
                raise conflict(RunConflict("approval_required"))
            if approval:
                if decision.get("type") not in {"approve", "reject"}:
                    raise conflict(RunConflict("approval_edit_forbidden"))
                if (
                    decision.get("approval_id", approval["approval_id"]) != approval["approval_id"]
                    or decision.get("args_digest", approval["args_digest"])
                    != approval["args_digest"]
                ):
                    raise conflict(RunConflict("approval_changed"))
                decision.update(
                    approval_id=approval["approval_id"], args_digest=approval["args_digest"]
                )
            mapped[item.id] = {"decisions": [decision]}
        splitter = ThinkSplitter()
        async for raw in protocol._graph.astream(
            Command(resume=mapped),
            config=config,
            stream_mode=["values", "updates", "messages", "custom"],
            version="v2",
        ):
            for event in protocol._project_chunk(raw, splitter):
                yield event
        for event in protocol._drain_splitter(splitter):
            yield event
