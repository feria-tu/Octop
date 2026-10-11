"""Authoritative business tool policy with call-bound durable approval."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt

from octop.infra.agents.settings.tool_catalog import BUILTIN_TOOL_CATALOG
from octop.infra.db.repos.runs import RunConflict, encode
from octop.infra.runs.service import conflict, definition_version, execution_context, wire
from octop.infra.users.enterprise import SAFE_TOOLS, db_call
from octop.infra.utils.ulid import new_ulid

_LOCAL_TOOLS = {t.name for t in BUILTIN_TOOL_CATALOG} | SAFE_TOOLS | {"read_document"}


def external_tool_names(tools: list[Any]) -> set[str]:
    names = set()
    for tool in tools:
        function = getattr(tool, "coroutine", None) or getattr(tool, "func", None)
        module = str(getattr(function, "__module__", ""))
        if not module.startswith(
            (
                "octop.infra.",
                "octop_harness.tools.",
                "octop_harness.middleware.",
                "deepagents.middleware.",
            )
        ):
            names.add(
                str(tool.get("name", "") if isinstance(tool, dict) else getattr(tool, "name", ""))
            )
    return names


class DurableRunMiddleware(AgentMiddleware[Any, Any]):
    def __init__(
        self,
        agent_id: str | None = None,
        loaded_version: str | None = None,
        external_names: Callable[[], set[str]] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.loaded_version = loaded_version
        self.external_names = external_names

    def local_tool(self, name: str) -> bool:
        return name in _LOCAL_TOOLS and (
            self.external_names is None or name not in self.external_names()
        )

    async def validate(self) -> Any:
        ctx = execution_context.get()
        if ctx is None or (self.agent_id is not None and self.agent_id != ctx.row.agent_id):
            raise conflict(RunConflict("execution_context_missing"))
        await ctx.validate()
        if self.loaded_version is not None:
            row = await db_call(ctx.service.repos.agent_repo.get, ctx.row.agent_id)
            if row is None or definition_version(row) != self.loaded_version:
                raise conflict(RunConflict("loaded_version_stale"))
        return ctx

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        ctx = await self.validate()
        visible = []
        for tool in request.tools:
            name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "")
            try:
                policy = await db_call(ctx.service.repo.policy, ctx.row.run_id, str(name))
                if policy is None and not self.local_tool(str(name)):
                    continue
            except RunConflict:
                continue
            visible.append(tool)
        response = await handler(request.override(tools=visible))
        await ctx.validate()
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        ctx = await self.validate()
        call = request.tool_call
        name = str(call.get("name") or "")
        repo = ctx.service.repo
        try:
            policy = await db_call(repo.policy, ctx.row.run_id, name)
            if policy is None and not self.local_tool(str(name)):
                raise RunConflict("tool_unregistered")
            if policy is None or policy["read_only"]:
                result = await handler(request)
                await ctx.validate()
                if policy is not None:
                    current = await db_call(repo.policy, ctx.row.run_id, name)
                    if current != policy:
                        raise RunConflict("tool_policy_changed")
                return result
            call_id = str(call.get("id") or "")
            if not call_id:
                raise RunConflict("call_id_required")
            args_json = encode(wire(call.get("args") or {}))
            digest = hashlib.sha256(args_json.encode()).hexdigest()
            approval = await db_call(
                repo.approval,
                ctx.row.run_id,
                ctx.service.node,
                ctx.row.fence,
                call_id,
                name,
                args_json,
                digest,
                new_ulid(),
            )
            if approval["state"] == "pending" or ctx.resuming_approval:
                # This interrupt is checkpointed. A recreated process can rebuild its UI
                # from run_approvals; an in-memory approval card is never authoritative.
                answer = interrupt(
                    {
                        "action_requests": [
                            {
                                "name": name,
                                "args": json.loads(args_json),
                                "approval_id": approval["approval_id"],
                                "args_digest": digest,
                            }
                        ],
                        "review_configs": [
                            {"action_name": name, "allowed_decisions": ["approve", "reject"]}
                        ],
                        "approval_id": approval["approval_id"],
                        "run_id": ctx.row.run_id,
                    }
                )
                decisions = answer.get("decisions") if isinstance(answer, dict) else None
                if (
                    not isinstance(decisions, list)
                    or len(decisions) != 1
                    or decisions[0].get("type") not in {"approve", "reject"}
                ):
                    raise RunConflict("approval_decision_invalid")
                if (
                    decisions[0].get("approval_id") != approval["approval_id"]
                    or decisions[0].get("args_digest") != digest
                ):
                    raise RunConflict("approval_changed")
                await db_call(
                    repo.decide,
                    approval["approval_id"],
                    ctx.row.user_id,
                    digest,
                    decisions[0]["type"] == "approve",
                )
                approval = await db_call(
                    repo.approval,
                    ctx.row.run_id,
                    ctx.service.node,
                    ctx.row.fence,
                    call_id,
                    name,
                    args_json,
                    digest,
                    approval["approval_id"],
                )
            if approval["state"] != "approved":
                raise RunConflict("approval_rejected")
            async with ctx.write_lock:
                await ctx.validate()
                decision = await db_call(
                    repo.begin_write,
                    ctx.row.run_id,
                    ctx.service.node,
                    ctx.row.fence,
                    call_id,
                    name,
                    digest,
                )
                if decision["action"] == "replay":
                    return ToolMessage.model_validate(decision["result"])
                try:
                    # Authorization/cancellation is checked again after the durable begin.
                    await ctx.validate()
                    current = await db_call(repo.policy, ctx.row.run_id, name)
                    if current != policy:
                        raise RunConflict("tool_policy_changed")
                    result = await handler(request)
                    if not isinstance(result, ToolMessage) or result.status == "error":
                        raise RunConflict("write_result_unconfirmed")
                    await db_call(
                        repo.complete_write,
                        ctx.row.run_id,
                        ctx.service.node,
                        ctx.row.fence,
                        call_id,
                        wire(result),
                    )
                    return result
                except BaseException:
                    await db_call(
                        repo.uncertain, ctx.row.run_id, ctx.service.node, ctx.row.fence, call_id
                    )
                    raise
        except RunConflict as exc:
            raise conflict(exc) from exc
