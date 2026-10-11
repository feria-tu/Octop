"""Company administration and isolated, centrally managed assistants."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from functools import partial
from typing import Any, cast

from octop.infra.db.repos.enterprise import DepartmentRow
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.enterprise_access import agent_access_policy
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)
runtime_access: ContextVar[tuple[str, int] | None] = ContextVar(
    "enterprise_runtime_access", default=None
)
PERSONAL_PROFILE_FIELDS = frozenset(
    {"name", "description", "icon", "icon_name", "color", "system_prompt"}
)
SAFE_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "write_file",
        "edit_file",
        "write_todos",
        "current_time",
        "send_file_to_user",
        "ask_user_question",
        "memory_search",
        "memory_get",
        "search_knowledge",
    }
)


async def db_call[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, partial(fn, *args, **kwargs))


def require_company_admin(actor: Any) -> None:
    if not actor.is_admin:
        raise OctopError.localized(ErrorCode.FORBIDDEN)


class EnterpriseService:
    def __init__(self, server: Any) -> None:
        self.server = server
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def services(self) -> Any:
        return self.server.services

    @property
    def repo(self) -> Any:
        return self.services.enterprise_repo

    @property
    def registry(self) -> Any:
        if self.server.app_runtime is None:
            raise OctopError.localized(ErrorCode.AGENT_NOT_RUNNING)
        return self.server.app_runtime.agent_registry

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def audit(self, actor: Any, action: str, target: str, payload: str | None = None) -> None:
        await db_call(
            self.services.audit_repo.write,
            actor=actor.username,
            action=action,
            target=target,
            payload=payload,
        )

    async def departments(self) -> list[DepartmentRow]:
        return await db_call(self.repo.departments)

    async def require_department(self, department_id: str) -> DepartmentRow:
        row = await db_call(self.repo.department, department_id)
        if row is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        return cast(DepartmentRow, row)

    async def create_department(self, actor: Any, name: str) -> DepartmentRow:
        require_company_admin(actor)
        name = name.strip()
        if not name or len(name) > 100:
            raise OctopError.localized(ErrorCode.FORBIDDEN, status=400)
        async with self.lock("departments"):
            if any(d.name == name for d in await self.departments()):
                raise OctopError.localized(ErrorCode.FORBIDDEN, status=409)
            department_id = new_ulid()
            await db_call(self.repo.create_department, department_id, name)
        await self.audit(actor, "department.create", department_id)
        return await self.require_department(department_id)

    async def rename_department(self, actor: Any, department_id: str, name: str) -> DepartmentRow:
        require_company_admin(actor)
        await self.require_department(department_id)
        name = name.strip()
        if not name or len(name) > 100:
            raise OctopError.localized(ErrorCode.FORBIDDEN, status=400)
        async with self.lock("departments"):
            if any(
                d.name == name and d.department_id != department_id
                for d in await self.departments()
            ):
                raise OctopError.localized(ErrorCode.FORBIDDEN, status=409)
            await db_call(self.repo.rename_department, department_id, name)
        await self.audit(actor, "department.rename", department_id)
        return await self.require_department(department_id)

    async def delete_department(self, actor: Any, department_id: str) -> None:
        require_company_admin(actor)
        await self.require_department(department_id)
        if not await db_call(self.repo.delete_department, department_id):
            raise OctopError.localized(ErrorCode.FORBIDDEN, status=409)
        await self.audit(actor, "department.delete", department_id)

    async def assign_department(
        self, actor: Any, user_id: int, department_id: str | None
    ) -> dict[str, str | None]:
        require_company_admin(actor)
        user = await db_call(self.services.user_repo.get, user_id)
        if user is None:
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        if department_id is None and user.department_id is not None and user.role != "admin":
            raise OctopError.localized(ErrorCode.FORBIDDEN, status=400)
        if department_id is not None:
            await self.require_department(department_id)
        await db_call(self.repo.assign_department, user_id, department_id)
        cached = self.server.user_manager.get_by_id(user_id)
        if cached is not None:
            cached.department_id = department_id
        await self.revoke_user_runtimes(user_id)
        await self.audit(actor, "user.department", str(user_id), department_id)
        if department_id is None or user.role == "admin":
            return await db_call(self.repo.assistant_status, user_id)
        return await self.ensure_personal_assistant(user_id)

    async def personal_defaults(self) -> dict[str, Any]:
        raw = await db_call(self.services.settings_repo.get, "enterprise.personal_defaults")
        return (
            json.loads(raw) if raw else {"name": "", "default_model": None, "system_prompt": None}
        )

    async def set_personal_defaults(self, actor: Any, values: dict[str, Any]) -> dict[str, Any]:
        require_company_admin(actor)
        if set(values) - {"name", "default_model", "system_prompt"}:
            raise OctopError.localized(ErrorCode.FORBIDDEN, status=400)
        await db_call(
            self.services.settings_repo.set, "enterprise.personal_defaults", json.dumps(values)
        )
        await self.audit(actor, "assistant.defaults", "company")
        return values

    def safe_config(self, agent_id: str) -> dict[str, Any]:
        from octop.infra.agents.settings.tool_catalog import BUILTIN_TOOL_CATALOG

        root = str(self.services.paths.agent_workspace(agent_id))
        return {
            "workspace_dir": root,
            "backend": {"type": "filesystem", "root_dir": root, "virtual_mode": True},
            "tools_disabled": sorted({t.name for t in BUILTIN_TOOL_CATALOG} - SAFE_TOOLS),
            "mcp_servers": {},
            "skills": [],
            "subagents": [],
            "acp_runners": {},
        }

    async def ensure_personal_assistant(self, user_id: int) -> dict[str, str | None]:
        from octop.infra.agents.manager import AgentCreateSpec

        async with self.lock(f"personal:{user_id}"):
            user = await db_call(self.services.user_repo.get, user_id)
            if user is None or user.disabled or not user.department_id:
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            status = await db_call(self.repo.assistant_status, user_id)
            existing = status["agent_id"]
            if existing and await db_call(self.services.agent_repo.get, existing):
                row = await db_call(self.services.agent_repo.get, existing)
                try:
                    if row.last_state != "running":
                        await self.registry.start(existing)
                    running = await db_call(self.services.agent_repo.get, existing)
                    if running is None or running.last_state != "running":
                        raise OctopError.localized(ErrorCode.AGENT_FAILED)
                    await db_call(self.repo.set_assistant_status, user_id, existing, "ready")
                except Exception:
                    await db_call(
                        self.repo.set_assistant_status,
                        user_id,
                        existing,
                        "failed",
                        ErrorCode.AGENT_FAILED.value,
                    )
                return await db_call(self.repo.assistant_status, user_id)
            agent_id = new_ulid().lower()
            await db_call(self.repo.set_assistant_status, user_id, None, "pending")
            try:
                defaults = await self.personal_defaults()
                await self.registry.create(
                    AgentCreateSpec(
                        agent_id=agent_id,
                        user_id=user_id,
                        name=defaults.get("name") or user.display_name or user.username,
                        default_model=defaults.get("default_model"),
                        system_prompt=defaults.get("system_prompt"),
                        config=self.safe_config(agent_id),
                        mcp_servers=[],
                        skill_package_ids=[],
                        enterprise_kind="personal",
                    )
                )
                row = await db_call(self.services.agent_repo.get, agent_id)
                state = "ready" if row and row.last_state == "running" else "failed"
                await db_call(
                    self.repo.set_assistant_status,
                    user_id,
                    agent_id if row else None,
                    state,
                    None if state == "ready" else ErrorCode.AGENT_FAILED.value,
                )
            except Exception:
                logger.exception("employee assistant initialization failed user=%s", user_id)
                row = await db_call(self.services.agent_repo.get, agent_id)
                await db_call(
                    self.repo.set_assistant_status,
                    user_id,
                    agent_id if row else None,
                    "failed",
                    ErrorCode.AGENT_FAILED.value,
                )
            return await db_call(self.repo.assistant_status, user_id)

    async def create_department_agent(
        self, actor: Any, department_id: str, values: dict[str, Any]
    ) -> Any:
        from octop.infra.agents.manager import AgentCreateSpec

        require_company_admin(actor)
        await self.require_department(department_id)
        knowledge_ids = values.get("knowledge_base_ids") or []
        for kb_id in knowledge_ids:
            if await db_call(self.services.knowledge_repo.get_base, kb_id) is None:
                raise OctopError.localized(ErrorCode.KNOWLEDGE_NOT_FOUND)
        agent_id = new_ulid().lower()
        row = await self.registry.create(
            AgentCreateSpec(
                agent_id=agent_id,
                user_id=None,
                name=values["name"],
                description=values.get("description"),
                default_model=values.get("default_model"),
                system_prompt=values.get("system_prompt"),
                knowledge_base_ids=knowledge_ids,
                config=self.safe_config(agent_id),
                skill_package_ids=[],
                mcp_servers=[],
                enterprise_kind="department",
                enabled=False,
                department_id=department_id,
            )
        )
        await self.audit(actor, "department.agent.create", agent_id, department_id)
        return row

    async def runtime_for(
        self, definition_id: str, user_id: int, *, audit_actor: Any | None = None
    ) -> str:
        from octop.infra.agents.manager import AgentCreateSpec

        async with (
            self.lock(f"definition:{definition_id}"),
            self.lock(f"runtime:{definition_id}:{user_id}"),
        ):
            await self.validate_use(definition_id, user_id, audit_actor=audit_actor)
            definition = await db_call(self.services.agent_repo.get, definition_id)
            records = await db_call(
                self.repo.runtimes, definition_id=definition_id, user_id=user_id
            )
            if audit_actor is not None:
                if not records:
                    raise OctopError.localized(ErrorCode.NOT_FOUND)
                return str(records[0].runtime_id)
            if not records:
                runtime_id = new_ulid().lower()
                await self.registry.create(
                    AgentCreateSpec(
                        agent_id=runtime_id,
                        user_id=None,
                        name=f"{definition.name} / {user_id}",
                        default_model=definition.default_model,
                        system_prompt=definition.system_prompt,
                        config=self.safe_config(runtime_id),
                        skill_package_ids=[],
                        mcp_servers=[],
                        enterprise_kind="runtime",
                    )
                )
                await db_call(self.repo.bind_runtime, definition_id, user_id, runtime_id)
                records = await db_call(
                    self.repo.runtimes, definition_id=definition_id, user_id=user_id
                )
            record = records[0]
            if record.definition_version != definition.definition_version:
                await self.stop_runtime(record.runtime_id)
                await db_call(
                    self.services.agent_repo.update_config,
                    record.runtime_id,
                    default_model=definition.default_model,
                    system_prompt=definition.system_prompt,
                    description=definition.description,
                    knowledge_base_ids=definition.knowledge_base_ids,
                )
                await self.registry.start(record.runtime_id)
                running = await db_call(self.services.agent_repo.get, record.runtime_id)
                if running is None or running.last_state != "running":
                    raise OctopError.localized(ErrorCode.AGENT_FAILED)
                await db_call(
                    self.repo.sync_runtime, record.runtime_id, definition.definition_version
                )
            else:
                row = await db_call(self.services.agent_repo.get, record.runtime_id)
                if row.last_state != "running":
                    await self.registry.start(record.runtime_id)
                    running = await db_call(self.services.agent_repo.get, record.runtime_id)
                    if running is None or running.last_state != "running":
                        raise OctopError.localized(ErrorCode.AGENT_FAILED)
            await self.validate_use(definition_id, user_id, audit_actor=audit_actor)
            return str(record.runtime_id)

    async def validate_use(
        self,
        definition_id: str,
        user_id: int,
        *,
        audit_actor: Any | None = None,
        runtime_id: str | None = None,
    ) -> None:
        from types import SimpleNamespace

        user = await db_call(self.services.user_repo.get, user_id)
        definition = await db_call(self.services.agent_repo.get, definition_id)
        if user is None or definition is None:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if audit_actor is not None:
            require_company_admin(audit_actor)
            current_actor = await db_call(self.services.user_repo.get, audit_actor.id)
            if current_actor is None or current_actor.disabled or current_actor.role != "admin":
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            return
        if user.disabled or not definition.enabled:
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        if runtime_id is not None:
            records = await db_call(
                self.repo.runtimes, definition_id=definition_id, user_id=user_id
            )
            if not any(
                record.runtime_id == runtime_id
                and record.definition_version == definition.definition_version
                for record in records
            ):
                raise OctopError.localized(ErrorCode.FORBIDDEN)
        identity = SimpleNamespace(
            id=user.id, is_admin=user.role == "admin", department_id=user.department_id
        )
        if not agent_access_policy(definition, identity).can_use:
            raise OctopError.localized(ErrorCode.FORBIDDEN)

    async def stop_runtime(self, runtime_id: str) -> None:
        for thread in await db_call(
            self.services.thread_repo.list_by_agent, agent_id=runtime_id, limit=10000
        ):
            self.registry.cancel_stream(runtime_id, thread.thread_id)
        row = await db_call(self.services.agent_repo.get, runtime_id)
        if row is not None and row.last_state == "running":
            await self.registry.stop(runtime_id)

    async def revoke_user_runtimes(self, user_id: int) -> None:
        for row in await db_call(self.repo.runtimes, user_id=user_id):
            await self.stop_runtime(row.runtime_id)

    async def remove_employee_assistants(self, user_id: int) -> None:
        for runtime in await db_call(self.repo.runtimes, user_id=user_id):
            await self.stop_runtime(runtime.runtime_id)
            await self.registry.delete(runtime.runtime_id)
        personal = (await db_call(self.repo.assistant_status, user_id))["agent_id"]
        if personal:
            await self.stop_runtime(personal)
            await self.registry.delete(personal)

    async def definition_changed(self, actor: Any, definition_id: str) -> None:
        require_company_admin(actor)
        await db_call(self.repo.bump_definition, definition_id)
        for row in await db_call(self.repo.runtimes, definition_id=definition_id):
            await self.stop_runtime(row.runtime_id)
        await self.audit(actor, "department.agent.update", definition_id)

    async def update_department_agent(
        self, actor: Any, agent_id: str, values: dict[str, Any]
    ) -> Any:
        require_company_admin(actor)
        row = await db_call(self.services.agent_repo.get, agent_id)
        if row is None or row.enterprise_kind != "department":
            raise OctopError.localized(ErrorCode.NOT_FOUND)
        allowed = {
            "name",
            "description",
            "default_model",
            "system_prompt",
            "knowledge_base_ids",
            "enabled",
        }
        if set(values) - allowed:
            raise OctopError.localized(ErrorCode.FORBIDDEN, status=400)
        async with self.lock(f"definition:{agent_id}"):
            for kb_id in values.get("knowledge_base_ids") or []:
                if await db_call(self.services.knowledge_repo.get_base, kb_id) is None:
                    raise OctopError.localized(ErrorCode.KNOWLEDGE_NOT_FOUND)
            updates = {
                k: v for k, v in values.items() if k not in {"enabled", "knowledge_base_ids"}
            }
            if "knowledge_base_ids" in values:
                updates["knowledge_base_ids"] = json.dumps(values["knowledge_base_ids"] or [])
            # Invalidate running copies before modifying the published definition.
            await self.definition_changed(actor, agent_id)
            if updates:
                await self.registry.update(agent_id, **updates)
            if "enabled" in values:
                await db_call(self.services.agent_repo.set_enabled, agent_id, values["enabled"])
        return await db_call(self.services.agent_repo.get, agent_id)

    async def delete_department_agent(self, actor: Any, agent_id: str) -> None:
        require_company_admin(actor)
        async with self.lock(f"definition:{agent_id}"):
            row = await db_call(self.services.agent_repo.get, agent_id)
            if row is None or row.enterprise_kind != "department":
                raise OctopError.localized(ErrorCode.NOT_FOUND)
            await db_call(self.services.agent_repo.set_enabled, agent_id, False)
            for runtime in await db_call(self.repo.runtimes, definition_id=agent_id):
                await self.stop_runtime(runtime.runtime_id)
                await self.registry.delete(runtime.runtime_id)
            await self.registry.delete(agent_id)
            await self.audit(actor, "department.agent.delete", agent_id)
