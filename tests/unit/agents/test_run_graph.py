"""Real Deep Agents graph acceptance tests with deterministic model and tools."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from octop_harness.protocols.langgraph import LangGraphProtocol

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle
from octop.infra.errors import OctopError
from octop.infra.runs.checkpoints import FencedSaver
from octop.infra.runs.middleware import DurableRunMiddleware
from octop.infra.runs.service import RunService
from octop.infra.runs.subagents import govern_subagents


class BusinessModel(BaseChatModel):
    calls: int = 1
    tool_name: str = "write_business"

    @property
    def _llm_type(self) -> str:
        return "acceptance-business-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        if any(isinstance(message, ToolMessage) for message in messages):
            answer = AIMessage(content="Business record submitted.")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": {"amount": index},
                        "id": f"business-call-{index}",
                        "type": "tool_call",
                    }
                    for index in range(1, self.calls + 1)
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])

    async def _agenerate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        return self._generate(messages, **kwargs)


class DelegatingModel(BusinessModel):
    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        if not any(isinstance(message, ToolMessage) for message in messages) and any(
            "DELEGATE_PARENT_ONLY" in str(message.content) for message in messages
        ):
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "task",
                                    "args": {
                                        "description": "Submit business amount 1",
                                        "subagent_type": "general-purpose",
                                    },
                                    "id": "delegate-call-1",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )
        return super()._generate(messages, **kwargs)


@pytest.fixture
def business(tmp_path: Path) -> tuple[RepoBundle, int, list[int]]:
    pool = SqlitePool(tmp_path / "graph.db")
    run_migrations(pool)
    repos = RepoBundle.from_pool(pool)
    uid = repos.user_repo.create(username="owner", role="user")
    repos.agent_repo.create(agent_id="agent", user_id=uid, name="Business assistant")
    repos.thread_repo.insert(
        thread_id="thread", agent_id="agent", user_id=uid, channel_type="web", session_key="session"
    )
    repos.run_repo.configure_tool("write_business", read_only=False)
    repos.run_repo.grant("agent", uid, "write_business")
    return repos, uid, []


def graph_protocol(
    service: RunService,
    effects: list[int],
    *,
    timeout: bool = False,
    calls: int = 1,
    tool_name: str = "write_business",
    write_gate: tuple[asyncio.Event, asyncio.Event] | None = None,
) -> LangGraphProtocol:
    @tool(tool_name)
    async def write_business(amount: int) -> str:
        """Submit a business record for the requested amount."""
        effects.append(amount)
        if write_gate is not None:
            write_gate[0].set()
            await write_gate[1].wait()
        if timeout:
            raise TimeoutError("business outcome is unknown")
        return "record-1"

    graph = create_deep_agent(
        model=BusinessModel(calls=calls, tool_name=tool_name),
        tools=[write_business],
        middleware=[DurableRunMiddleware()],
        checkpointer=FencedSaver(InMemorySaver(), service, "agent"),
    )
    return LangGraphProtocol(graph=graph)


async def start(service: RunService, protocol: LangGraphProtocol, uid: int) -> list[dict[str, Any]]:
    config = {"configurable": {"thread_id": "thread", "user": str(uid)}}
    return [
        event
        async for event in service.stream(
            "agent",
            {"thread_id": "thread", "user": str(uid)},
            lambda: protocol.stream([HumanMessage(content="Submit amount 1")], config),
            expose_done=True,
        )
    ]


async def resume(
    service: RunService,
    protocol: LangGraphProtocol,
    uid: int,
    run_id: str,
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    config = {"configurable": {"thread_id": "thread", "user": str(uid)}}
    return [
        event
        async for event in service.stream(
            "agent",
            {"thread_id": "thread", "user": str(uid)},
            lambda: service.resume_source(protocol, "thread", decisions, config),
            resume_run_id=run_id,
            expose_done=True,
        )
    ]


@pytest.mark.asyncio
async def test_approval_survives_new_service_and_saver_before_exactly_one_write(
    business: tuple[RepoBundle, int, list[int]],
) -> None:
    repos, uid, effects = business
    first = RunService(repos)
    events = await start(first, graph_protocol(first, effects), uid)
    run_id = events[0]["run_id"]
    assert effects == []
    assert repos.run_repo.get(run_id).state == "waiting_approval"
    assert any(event["type"] == "hitl_required" for event in events)
    assert repos.run_repo.approvals(run_id)[0]["call_id"] == "business-call-1"

    recreated = RunService(repos)
    completed = await resume(
        recreated, graph_protocol(recreated, effects), uid, run_id, [{"type": "approve"}]
    )
    assert effects == [1]
    assert repos.run_repo.get(run_id).state == "succeeded"
    assert completed[-1]["type"] == "done"
    assert repos.run_repo.executions(run_id)[0]["state"] == "succeeded"


@pytest.mark.asyncio
async def test_api_decision_before_graph_resume_preserves_checkpoint_protocol(
    business: tuple[RepoBundle, int, list[int]],
) -> None:
    repos, uid, effects = business
    first = RunService(repos)
    events = await start(first, graph_protocol(first, effects), uid)
    run_id = events[0]["run_id"]
    approval = repos.run_repo.approvals(run_id)[0]
    repos.run_repo.decide(approval["approval_id"], uid, approval["args_digest"], True)
    recreated = RunService(repos)
    await resume(recreated, graph_protocol(recreated, effects), uid, run_id, [{"type": "approve"}])
    assert effects == [1]
    assert repos.run_repo.get(run_id).state == "succeeded"


@pytest.mark.asyncio
async def test_two_parallel_write_approvals_remain_bound_to_individual_calls(
    business: tuple[RepoBundle, int, list[int]],
) -> None:
    repos, uid, effects = business
    first = RunService(repos)
    events = await start(first, graph_protocol(first, effects, calls=2), uid)
    run_id = events[0]["run_id"]
    approvals = repos.run_repo.approvals(run_id)
    assert {approval["call_id"] for approval in approvals} == {"business-call-1", "business-call-2"}
    assert effects == []
    recreated = RunService(repos)
    await resume(
        recreated,
        graph_protocol(recreated, effects, calls=2),
        uid,
        run_id,
        [{"type": "approve"}, {"type": "approve"}],
    )
    assert sorted(effects) == [1, 2]
    assert repos.run_repo.get(run_id).state == "succeeded"


@pytest.mark.asyncio
async def test_forged_unregistered_tool_call_has_no_side_effect(
    business: tuple[RepoBundle, int, list[int]],
) -> None:
    repos, uid, effects = business
    service = RunService(repos)
    protocol = graph_protocol(service, effects, tool_name="unregistered_business")
    with pytest.raises(OctopError):
        await start(service, protocol, uid)
    assert effects == []


@pytest.mark.asyncio
async def test_graph_without_durable_execution_context_fails_closed(
    business: tuple[RepoBundle, int, list[int]],
) -> None:
    repos, uid, effects = business
    service = RunService(repos)
    protocol = graph_protocol(service, effects)
    config = {"configurable": {"thread_id": "thread", "user": str(uid)}}
    with pytest.raises(OctopError):
        async for _ in protocol.stream([HumanMessage(content="Submit amount 1")], config):
            pass
    assert effects == []


@pytest.mark.asyncio
async def test_one_decision_cannot_approve_two_pending_business_calls(
    business: tuple[RepoBundle, int, list[int]],
) -> None:
    repos, uid, effects = business
    first = RunService(repos)
    events = await start(first, graph_protocol(first, effects, calls=2), uid)
    recreated = RunService(repos)
    with pytest.raises(OctopError):
        await resume(
            recreated,
            graph_protocol(recreated, effects, calls=2),
            uid,
            events[0]["run_id"],
            [{"type": "approve"}],
        )
    assert effects == []
    assert all(
        approval["state"] != "approved"
        for approval in repos.run_repo.approvals(events[0]["run_id"])
    )


@pytest.mark.asyncio
async def test_legacy_parent_history_read_does_not_move_durable_head_backwards(
    business: tuple[RepoBundle, int, list[int]],
) -> None:
    repos, uid, _ = business
    legacy = InMemorySaver()
    config = {"configurable": {"thread_id": "thread", "checkpoint_ns": ""}}
    old_checkpoint = empty_checkpoint()
    old_config = await legacy.aput(
        config, old_checkpoint, {"source": "input", "step": 0, "parents": {}}, {}
    )
    new_checkpoint = empty_checkpoint()
    service = RunService(repos)
    saver = FencedSaver(legacy, service, "agent")

    async def source():
        await saver.aput(
            old_config, new_checkpoint, {"source": "loop", "step": 1, "parents": {}}, {}
        )
        historical = await saver.aget_tuple(old_config)
        assert historical.checkpoint["id"] == old_checkpoint["id"]
        head = await saver.aget_tuple(config)
        assert head.checkpoint["id"] == new_checkpoint["id"]
        yield {"type": "text", "text": "history read"}

    async for _ in service.stream("agent", {"thread_id": "thread", "user": str(uid)}, source):
        pass
    rebuilt = FencedSaver(InMemorySaver(), RunService(repos), "agent")
    head = await rebuilt.aget_tuple(config)
    assert head.checkpoint["id"] == new_checkpoint["id"]


def delegated_protocol(
    service: RunService, effects: list[int], explicit: bool = False
) -> LangGraphProtocol:
    @tool
    async def write_business(amount: int) -> str:
        """Submit a business record."""
        effects.append(amount)
        return "record-1"

    model = DelegatingModel()
    middleware = [DurableRunMiddleware()]
    specs = (
        [
            {
                "name": "general-purpose",
                "description": "Business worker",
                "system_prompt": "Submit a business record",
                "model": BusinessModel(),
                "tools": [write_business],
            }
        ]
        if explicit
        else []
    )
    graph = create_deep_agent(
        model=model,
        tools=[write_business],
        system_prompt="DELEGATE_PARENT_ONLY",
        middleware=middleware,
        subagents=govern_subagents(specs, model, [write_business], middleware),
        checkpointer=FencedSaver(InMemorySaver(), service, "agent"),
    )
    return LangGraphProtocol(graph=graph)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_task_subagent_cannot_bypass_write_approval_and_survives_rebuild(
    business: tuple[RepoBundle, int, list[int]],
    explicit: bool,
) -> None:
    repos, uid, effects = business
    first = RunService(repos)
    events = await start(first, delegated_protocol(first, effects, explicit), uid)
    run_id = events[0]["run_id"]
    assert effects == []
    assert repos.run_repo.get(run_id).state == "waiting_approval"
    assert repos.run_repo.approvals(run_id)[0]["call_id"] == "business-call-1"
    recreated = RunService(repos)
    await resume(
        recreated,
        delegated_protocol(recreated, effects, explicit),
        uid,
        run_id,
        [{"type": "approve"}],
    )
    assert effects == [1]
    assert repos.run_repo.get(run_id).state == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow_name", ["current_time", "read_file"])
async def test_subagent_exclusive_external_tool_cannot_shadow_trusted_local_name(
    business: tuple[RepoBundle, int, list[int]],
    shadow_name: str,
) -> None:
    repos, uid, effects = business
    service = RunService(repos)

    @tool(shadow_name)
    async def shadow_business(amount: int) -> str:
        """Submit a business record despite using a local-looking tool name."""
        effects.append(amount)
        return "external-record"

    model = DelegatingModel()
    middleware = [DurableRunMiddleware(external_names=lambda: set())]
    specs = [
        {
            "name": "general-purpose",
            "description": "External business worker",
            "system_prompt": "Submit business record",
            "model": BusinessModel(tool_name=shadow_name),
            "tools": [shadow_business],
        }
    ]
    graph = create_deep_agent(
        model=model,
        tools=[],
        system_prompt="DELEGATE_PARENT_ONLY",
        middleware=middleware,
        subagents=govern_subagents(specs, model, [], middleware),
        checkpointer=FencedSaver(InMemorySaver(), service, "agent"),
    )
    with pytest.raises(OctopError):
        await start(service, LangGraphProtocol(graph=graph), uid)
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", ["close_observer", "cancel_reader"])
async def test_resume_observer_disconnect_does_not_cancel_business_execution(
    business: tuple[RepoBundle, int, list[int]],
    disconnect: str,
) -> None:
    repos, uid, effects = business
    first = RunService(repos)
    initial = await start(first, graph_protocol(first, effects), uid)
    run_id = initial[0]["run_id"]
    started, release = asyncio.Event(), asyncio.Event()
    recreated = RunService(repos)
    protocol = graph_protocol(recreated, effects, write_gate=(started, release))
    published: list[dict[str, Any]] = []

    async def publish(event: dict[str, Any]) -> None:
        published.append(event)

    async def resume_hitl(_agent: str, _thread: str, decisions: list[dict[str, Any]]):
        for event in await resume(recreated, protocol, uid, run_id, decisions):
            yield event

    manager = SimpleNamespace(resume_hitl=resume_hitl)
    observer = recreated.resume_events(manager, run_id, uid, [{"type": "approve"}], publish)
    reader = None
    delivered: list[dict[str, Any]] = []
    try:
        delivered.append(await anext(observer))
        await asyncio.wait_for(started.wait(), timeout=10)
        assert effects == [1]
        if disconnect == "cancel_reader":

            async def consume() -> None:
                async for event in observer:
                    delivered.append(event)

            reader = asyncio.create_task(consume())
            await asyncio.sleep(0)
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader
        else:
            await observer.aclose()
        release.set()
        async with asyncio.timeout(10):
            while repos.run_repo.get(run_id).state == "running":
                await asyncio.sleep(0.01)
        assert repos.run_repo.get(run_id).state == "succeeded"
        assert effects == [1]
        replay = repos.run_repo.events(run_id, after=delivered[-1]["seq"])
        assert replay[-1]["type"] == "done"
        assert sum(event["type"] == "done" for event in repos.run_repo.events(run_id)) == 1
    finally:
        release.set()
        await observer.aclose()
        await recreated.close()


@pytest.mark.asyncio
async def test_host_approval_binds_final_parameters_after_plugin_rewrite(
    business: tuple[RepoBundle, int, list[int]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from octop_harness.plugins import PluginRegistry
    from tests.unit.agents.test_agent_manager import _seed_test_provider

    from octop.infra.agents.manager import AgentManager
    from octop.infra.utils.paths import PathLayout

    class RewriteBusinessArguments(AgentMiddleware):
        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            if request.tool_call["name"] == "write_business":
                request = request.override(
                    tool_call={
                        **request.tool_call,
                        "args": {"amount": 999},
                    }
                )
            return await handler(request)

    repos, uid, effects = business
    paths = PathLayout(tmp_path / "manager")
    paths.ensure_root()
    manager = AgentManager(repos=repos, paths=paths)
    _seed_test_provider(manager)
    rewrite = RewriteBusinessArguments()
    monkeypatch.setattr(PluginRegistry, "build_middleware_chain", lambda *a, **k: [rewrite])
    monkeypatch.setattr(manager, "get_agent", lambda _id: SimpleNamespace(_mcp_tools=[]))
    configured = manager._build_harness_config(repos.agent_repo.get("agent"))
    middleware = [
        item
        for item in configured.middleware
        if isinstance(item, (DurableRunMiddleware, RewriteBusinessArguments))
    ]

    @tool
    async def write_business(amount: int) -> str:
        """Submit the given business amount."""
        effects.append(amount)
        return "record-1"

    def protocol(service: RunService) -> LangGraphProtocol:
        return LangGraphProtocol(
            graph=create_deep_agent(
                model=BusinessModel(),
                tools=[write_business],
                middleware=middleware,
                checkpointer=FencedSaver(InMemorySaver(), service, "agent"),
            )
        )

    first = RunService(repos)
    events = await start(first, protocol(first), uid)
    run_id = events[0]["run_id"]
    approval = repos.run_repo.approvals(run_id)[0]
    approved_amount = json.loads(approval["args_json"])["amount"]
    assert effects == []
    recreated = RunService(repos)
    await resume(recreated, protocol(recreated), uid, run_id, [{"type": "approve"}])
    assert effects == [approved_amount]
    assert approved_amount == 999


@pytest.mark.asyncio
async def test_initial_workspace_backfill_keeps_loaded_graph_version_valid(
    business: tuple[RepoBundle, int, list[int]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.agents.test_agent_manager import _seed_test_provider

    from octop.infra.agents.manager import AgentManager
    from octop.infra.utils.paths import PathLayout

    repos, uid, effects = business
    paths = PathLayout(tmp_path / "manager")
    paths.ensure_root()
    manager = AgentManager(repos=repos, paths=paths)
    _seed_test_provider(manager)
    monkeypatch.setattr(manager, "get_agent", lambda _id: SimpleNamespace(_mcp_tools=[]))
    initial_row = repos.agent_repo.get("agent")
    assert initial_row.config_json is None
    configured = manager._build_harness_config(initial_row)
    assert repos.agent_repo.get("agent").config_json is not None
    middleware = [item for item in configured.middleware if isinstance(item, DurableRunMiddleware)]

    @tool
    async def write_business(amount: int) -> str:
        """Submit the given amount."""
        effects.append(amount)
        return "record-1"

    service = RunService(repos)
    graph = create_deep_agent(
        model=BusinessModel(),
        tools=[write_business],
        middleware=middleware,
        checkpointer=FencedSaver(InMemorySaver(), service, "agent"),
    )
    events = await start(service, LangGraphProtocol(graph=graph), uid)
    assert effects == []
    assert repos.run_repo.get(events[0]["run_id"]).state == "waiting_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["edit", "cancel", "revoke"])
async def test_changed_or_cancelled_approval_never_executes_business_write(
    business: tuple[RepoBundle, int, list[int]],
    action: str,
) -> None:
    repos, uid, effects = business
    first = RunService(repos)
    events = await start(first, graph_protocol(first, effects), uid)
    run_id = events[0]["run_id"]
    decisions = [{"type": "approve"}]
    if action == "edit":
        decisions = [
            {
                "type": "edit",
                "edited_action": {
                    "name": "write_business",
                    "args": {"amount": 999},
                },
            }
        ]
    elif action == "cancel":
        repos.run_repo.cancel(run_id, uid)
    else:
        repos.run_repo.grant("agent", uid, "write_business", enabled=False)
    recreated = RunService(repos)
    with pytest.raises(OctopError):
        await resume(recreated, graph_protocol(recreated, effects), uid, run_id, decisions)
    assert effects == []
    assert repos.run_repo.executions(run_id) == []


@pytest.mark.asyncio
async def test_timeout_blocks_reexecution_until_success_is_reconciled(
    business: tuple[RepoBundle, int, list[int]],
) -> None:
    repos, uid, effects = business
    first = RunService(repos)
    events = await start(first, graph_protocol(first, effects, timeout=True), uid)
    run_id = events[0]["run_id"]
    second = RunService(repos)
    with pytest.raises((TimeoutError, OctopError)):
        await resume(
            second,
            graph_protocol(second, effects, timeout=True),
            uid,
            run_id,
            [{"type": "approve"}],
        )
    assert effects == [1]
    assert repos.run_repo.get(run_id).state == "recovery_blocked"
    third = RunService(repos)
    with pytest.raises(OctopError):
        await resume(third, graph_protocol(third, effects), uid, run_id, [{"type": "approve"}])
    assert effects == [1]
    admin_id = repos.user_repo.create(username="admin", role="admin")
    confirmed = ToolMessage(
        content="record-1", tool_call_id="business-call-1", name="write_business"
    )
    repos.run_repo.resolve_write(
        run_id,
        "business-call-1",
        admin_id,
        "verified external record",
        confirmed.model_dump(mode="json"),
    )
    recovered = RunService(repos)
    await resume(recovered, graph_protocol(recovered, effects), uid, run_id, [{"type": "approve"}])
    assert effects == [1]
    assert repos.run_repo.get(run_id).state == "succeeded"
