"""Managed models cannot be changed through legacy chat entry points."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from octop_harness.slash import SlashCommand

from octop.api.routers.chat.history import patch_thread
from octop.api.routers.chat.models import RenameThreadBody
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.slash.ctx import SlashCtx
from octop.infra.gateway.slash.dispatcher import build_default_dispatcher
from octop.infra.users.enterprise import runtime_access


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["personal", "runtime"])
@pytest.mark.parametrize("command", ["model", "models"])
async def test_managed_model_slash_does_not_change_thread_override(kind: str, command: str) -> None:
    row = SimpleNamespace(enterprise_kind=kind, default_model="provider/approved")
    manager = MagicMock(get_row=MagicMock(return_value=row))
    threads = MagicMock(get_bound_thread_id=MagicMock(return_value="thread"))
    ctx = SlashCtx(
        agent_id="managed",
        user_id=7,
        channel_type="dashboard",
        session_key="session",
        thread_registry=threads,
        agent_manager=manager,
    )
    sink = SimpleNamespace(text=AsyncMock(), complete=AsyncMock(), action=AsyncMock())

    await build_default_dispatcher().handle(
        SlashCommand(name=command, args="provider/other"), ctx, sink
    )

    manager.set_thread_model.assert_not_called()
    sink.action.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["personal", "runtime"])
async def test_managed_thread_patch_rejects_model_override(kind: str) -> None:
    row = SimpleNamespace(agent_id="managed", enterprise_kind=kind, user_id=7, is_shared=0)
    user = SimpleNamespace(id=7, is_admin=False, disabled=False)
    thread = SimpleNamespace(
        agent_id="managed",
        user_id=7,
        title="Thread",
        pinned=False,
        model_ref=None,
        reasoning_mode=None,
        reasoning_effort=None,
        conversation_mode="craft",
        pending_plan_path=None,
        hitl_policy=None,
    )
    agents = MagicMock(get_row=MagicMock(return_value=row))
    agents.providers.is_model_ref_usable.return_value = True
    threads = MagicMock(get_thread=MagicMock(return_value=thread))
    server = SimpleNamespace(
        app_runtime=SimpleNamespace(
            agent_registry=agents, gateway=SimpleNamespace(thread_registry=threads)
        )
    )
    token = runtime_access.set(("managed", 7))
    try:
        with pytest.raises(OctopError) as exc:
            await patch_thread(
                "managed",
                "thread",
                RenameThreadBody(model_ref="provider/other"),
                as_user=None,
                user=user,
                server=server,
            )
        assert exc.value.code is ErrorCode.FORBIDDEN
    finally:
        runtime_access.reset(token)
    threads.update_composer.assert_not_called()
