"""Company departments, assistant defaults and centrally managed agents."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.enterprise import db_call, require_company_admin

router = APIRouter()


class DepartmentBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class DepartmentResponse(DepartmentBody):
    department_id: str
    created_at: int


class PersonalDefaults(BaseModel):
    name: str = Field(default="", max_length=100)
    default_model: str | None = Field(
        default=None,
        description="Configured provider/model reference; credentials remain server-side.",
    )
    system_prompt: str | None = None


class DepartmentAgentBody(PersonalDefaults):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    knowledge_base_ids: list[str] = Field(
        default_factory=list,
        description="Knowledge bases granted read-only to department conversations.",
    )


class DepartmentAgentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    default_model: str | None = None
    system_prompt: str | None = None
    knowledge_base_ids: list[str] | None = None
    enabled: bool | None = Field(
        default=None, description="Publish or revoke the agent for department employees."
    )


class DepartmentAgentResponse(DepartmentAgentBody):
    agent_id: str
    department_id: str
    enabled: bool
    definition_version: int


class AssistantStatus(BaseModel):
    agent_id: str | None
    status: str
    error: str | None


def agent_response(row: Any) -> DepartmentAgentResponse:
    from octop.infra.agents.settings.profile import parse_id_list_json

    return DepartmentAgentResponse(
        agent_id=row.agent_id,
        department_id=row.department_id,
        name=row.name,
        description=row.description,
        default_model=row.default_model,
        system_prompt=row.system_prompt,
        knowledge_base_ids=parse_id_list_json(row.knowledge_base_ids) or [],
        enabled=bool(row.enabled),
        definition_version=row.definition_version,
    )


@router.get(
    "/departments", response_model=list[DepartmentResponse], summary="List company departments"
)
async def list_departments(
    user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> Any:
    require_company_admin(user)
    return [asdict(row) for row in await server.enterprise.departments()]


@router.post(
    "/departments",
    response_model=DepartmentResponse,
    status_code=201,
    summary="Create company department",
)
async def create_department(
    body: DepartmentBody, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> Any:
    return asdict(await server.enterprise.create_department(user, body.name))


@router.patch(
    "/departments/{department_id}",
    response_model=DepartmentResponse,
    summary="Rename company department",
)
async def rename_department(
    department_id: str,
    body: DepartmentBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Any:
    return asdict(await server.enterprise.rename_department(user, department_id, body.name))


@router.delete(
    "/departments/{department_id}",
    status_code=204,
    summary="Delete empty company department",
    description="Departments containing employees or agents cannot be deleted.",
)
async def delete_department(
    department_id: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> None:
    await server.enterprise.delete_department(user, department_id)


@router.get(
    "/personal-defaults", response_model=PersonalDefaults, summary="Get employee assistant defaults"
)
async def get_defaults(user: Any = Depends(current_user), server: Any = Depends(get_server)) -> Any:
    require_company_admin(user)
    return await server.enterprise.personal_defaults()


@router.put(
    "/personal-defaults",
    response_model=PersonalDefaults,
    summary="Configure defaults for newly created employee assistants",
)
async def set_defaults(
    body: PersonalDefaults, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> Any:
    return await server.enterprise.set_personal_defaults(user, body.model_dump())


@router.get(
    "/agents",
    response_model=list[DepartmentAgentResponse],
    summary="List department agent definitions",
)
async def list_agents(user: Any = Depends(current_user), server: Any = Depends(get_server)) -> Any:
    require_company_admin(user)
    rows = await db_call(server.services.agent_repo.list_all)
    return [agent_response(row) for row in rows if row.enterprise_kind == "department"]


@router.post(
    "/departments/{department_id}/agents",
    response_model=DepartmentAgentResponse,
    status_code=201,
    summary="Create department agent",
    description="Creates a draft; publish with enabled=true after configuration. Employee runtime data is isolated.",
)
async def create_agent(
    department_id: str,
    body: DepartmentAgentBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Any:
    row = await server.enterprise.create_department_agent(user, department_id, body.model_dump())
    return agent_response(await db_call(server.services.agent_repo.get, row.agent_id))


@router.patch(
    "/agents/{agent_id}",
    response_model=DepartmentAgentResponse,
    summary="Configure or publish department agent",
)
async def update_agent(
    agent_id: str,
    body: DepartmentAgentPatch,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Any:
    values = body.model_dump(exclude_unset=True)
    if ("name" in values and values["name"] is None) or (
        "enabled" in values and values["enabled"] is None
    ):
        raise OctopError.localized(ErrorCode.FORBIDDEN, status=400)
    return agent_response(await server.enterprise.update_department_agent(user, agent_id, values))


@router.delete(
    "/agents/{agent_id}", status_code=204, summary="Delete department agent and private runtimes"
)
async def delete_agent(
    agent_id: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> None:
    await server.enterprise.delete_department_agent(user, agent_id)


@router.post(
    "/users/{user_id}/assistant/retry",
    response_model=AssistantStatus,
    summary="Retry employee assistant initialization",
)
async def retry_assistant(
    user_id: int, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> Any:
    require_company_admin(user)
    await server.enterprise.audit(user, "assistant.retry", str(user_id))
    return await server.enterprise.ensure_personal_assistant(user_id)
