"""Thin HTTP adapters for durable execution and business tool policy."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from octop.api.deps import current_user, get_server
from octop.api.routers.chat.sse import format_sse
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.runs.service import RunConflict, RunRow, RunService, conflict
from octop.infra.users.enterprise import db_call, require_company_admin

router = APIRouter()


class RunOut(BaseModel):
    run_id: str
    thread_id: str
    state: str
    version: str
    fence: int
    last_seq: int
    result: dict[str, Any] | None
    approvals: list[dict[str, Any]]
    executions: list[dict[str, Any]]


class EventsOut(BaseModel):
    events: list[dict[str, Any]]
    next_seq: int
    has_more: bool
    state: str


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str
    args_digest: str = Field(description="Digest of the exact parameters displayed for approval")
    approved: bool


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Original HITL decisions; parameter editing is not permitted for business writes",
    )


class ResolutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["succeeded", "not_executed"]
    note: str = Field(
        min_length=1,
        max_length=4000,
        description="Evidence used to reconcile the external operation",
    )
    result: dict[str, Any] | None = Field(
        default=None, description="Exact tool message for a confirmed successful operation"
    )


class ToolPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read_only: bool
    enabled: bool = True


class ToolGrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    user_id: int
    enabled: bool = True


class ToolPolicyOut(ToolPolicyBody):
    name: str
    revision: int


class ToolGrantOut(ToolGrantBody):
    tool_name: str
    revision: int


class Acknowledged(BaseModel):
    ok: bool = True


def service_for(server: Any) -> RunService:
    return RunService(server.services.repos)


async def output(service: RunService, row: RunRow) -> RunOut:
    return RunOut(
        run_id=row.run_id,
        thread_id=row.thread_id,
        state=row.state,
        version=row.version,
        fence=row.fence,
        last_seq=row.next_seq,
        result=json.loads(row.result_json) if row.result_json else None,
        approvals=await db_call(service.repo.approvals, row.run_id),
        executions=await db_call(service.repo.executions, row.run_id),
    )


@router.get("/runs/{run_id}", summary="Get durable execution state", response_model=RunOut)
async def get_run(
    run_id: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> RunOut:
    service = service_for(server)
    return await output(service, await service.owned(run_id, user.id))


@router.get(
    "/admin/runs/{run_id}",
    summary="Audit and inspect an execution for reconciliation",
    response_model=RunOut,
)
async def admin_run(
    run_id: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> RunOut:
    require_company_admin(user)
    service = service_for(server)
    return await output(service, await service.inspect_admin(run_id, user.id))


@router.post(
    "/admin/runs/{run_id}/cancel",
    summary="Audit and cancel an execution administratively",
    response_model=RunOut,
)
async def admin_cancel(
    run_id: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> RunOut:
    require_company_admin(user)
    service = server.app_runtime.agent_registry.run_service
    row = await service.inspect_admin(run_id, user.id)
    row = await service.cancel_id(row.run_id, row.user_id)
    return await output(service, row)


@router.get(
    "/threads/{thread_id}/run",
    summary="Get latest owned thread execution",
    response_model=RunOut | None,
)
async def latest_run(
    thread_id: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> RunOut | None:
    service = service_for(server)
    thread = await db_call(service.repos.thread_repo.get, thread_id)
    if thread is None or thread.user_id != user.id:
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    await service.version(thread.agent_id, user.id)
    await db_call(service.repo.expire)
    row = await db_call(service.repo.latest, thread.agent_id, thread_id)
    return await output(service, row) if row else None


@router.get(
    "/runs/{run_id}/events",
    summary="Replay durable events after a sequence",
    response_model=EventsOut,
    description="Events are saved before delivery. Resume with next_seq until has_more is false; duplicate events are identified by run_id and seq.",
)
async def events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> EventsOut:
    service = service_for(server)
    row = await service.owned(run_id, user.id)
    items = await db_call(service.repo.events, run_id, after_seq, limit)
    next_seq = items[-1]["seq"] if items else after_seq
    return EventsOut(
        events=items, next_seq=next_seq, has_more=next_seq < row.next_seq, state=row.state
    )


@router.post(
    "/runs/{run_id}/cancel", summary="Cancel and fence an owned execution", response_model=RunOut
)
async def cancel(
    run_id: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> RunOut:
    service = service_for(server)
    row = await service.owned(run_id, user.id)
    runtime_service = server.app_runtime.agent_registry.run_service
    row = await runtime_service.cancel_id(row.run_id, user.id)
    return await output(service, row)


@router.post(
    "/runs/{run_id}/approval",
    summary="Decide an exact business tool invocation",
    response_model=Acknowledged,
)
async def approval(
    run_id: str,
    body: ApprovalDecision,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Acknowledged:
    service = service_for(server)
    await service.owned(run_id, user.id)
    rows = await db_call(service.repo.approvals, run_id)
    if not any(r["approval_id"] == body.approval_id for r in rows):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    try:
        await db_call(
            service.repo.decide, body.approval_id, user.id, body.args_digest, body.approved
        )
    except RunConflict as exc:
        raise conflict(exc) from exc
    return Acknowledged()


@router.post(
    "/runs/{run_id}/resume",
    summary="Resume an owned checkpoint safely",
    description="Uses saved graph state without resending user input. Uncertain writes require administrator reconciliation first. Streams persisted events over SSE.",
)
async def resume(
    run_id: str,
    body: ResumeBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> StreamingResponse:
    manager = server.app_runtime.agent_registry
    service = manager.run_service
    row = await service.owned(run_id, user.id)
    if row.state not in {"queued", "waiting_approval", "recovery_blocked", "failed"}:
        raise OctopError.localized(ErrorCode.RUN_CONFLICT, status=409)

    async def generate() -> Any:
        async def publish(chunk: dict[str, Any]) -> None:
            await server.app_runtime.gateway.ws_hub.push_to_thread(row.thread_id, chunk)

        async for chunk in service.resume_events(manager, run_id, user.id, body.decisions, publish):
            yield format_sse("chunk", chunk)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post(
    "/runs/{run_id}/writes/{call_id}/resolve",
    summary="Reconcile an uncertain business write",
    response_model=Acknowledged,
)
async def resolve(
    run_id: str,
    call_id: str,
    body: ResolutionBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Acknowledged:
    require_company_admin(user)
    service = service_for(server)
    # Administrator reconciliation remains available after account revocation.
    try:
        result = body.result if body.outcome == "succeeded" else None
        if body.outcome == "succeeded":
            from langchain_core.messages import ToolMessage

            if result is None:
                raise OctopError.localized(ErrorCode.RUN_CONFLICT, status=409)
            try:
                message = ToolMessage.model_validate(result)
            except ValidationError as exc:
                raise OctopError.localized(ErrorCode.RUN_CONFLICT, status=409) from exc
            if message.tool_call_id != call_id or message.status == "error":
                raise OctopError.localized(ErrorCode.RUN_CONFLICT, status=409)
            result = message.model_dump(mode="json")
        await db_call(service.repo.resolve_write, run_id, call_id, user.id, body.note, result)
    except RunConflict as exc:
        raise conflict(exc) from exc
    return Acknowledged()


@router.get(
    "/business-tools",
    summary="List administrator-governed tool policies",
    response_model=list[ToolPolicyOut],
)
async def tools(
    user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> list[dict[str, Any]]:
    require_company_admin(user)
    return await db_call(service_for(server).repo.tools)


@router.put(
    "/business-tools/{name}",
    summary="Register or revoke a business tool",
    response_model=Acknowledged,
)
async def configure(
    name: str,
    body: ToolPolicyBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Acknowledged:
    require_company_admin(user)
    await db_call(service_for(server).repo.configure_tool, name, body.read_only, body.enabled)
    return Acknowledged()


@router.put(
    "/business-tools/{name}/grant",
    summary="Grant a tool to an agent and acting user",
    response_model=Acknowledged,
)
async def grant(
    name: str,
    body: ToolGrantBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Acknowledged:
    require_company_admin(user)
    service = service_for(server)
    if body.enabled:
        await service.version(body.agent_id, body.user_id)
    if not any(t["name"] == name for t in await db_call(service.repo.tools)):
        raise OctopError.localized(ErrorCode.NOT_FOUND)
    await db_call(service.repo.grant, body.agent_id, body.user_id, name, body.enabled)
    return Acknowledged()


@router.get(
    "/business-tools/{name}/grants",
    summary="Read configured business tool grants",
    response_model=list[ToolGrantOut],
)
async def grants(
    name: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> list[dict[str, Any]]:
    require_company_admin(user)
    return await db_call(service_for(server).repo.grants, name)
