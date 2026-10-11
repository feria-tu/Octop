import asyncio
from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle
from octop.infra.errors import OctopError
from octop.infra.runs.checkpoints import FencedSaver
from octop.infra.runs.service import Execution, RunService, execution_context


@pytest.mark.asyncio
async def test_cancelled_checkpoint_cannot_replace_authoritative_state(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "cp.db")
    run_migrations(pool)
    repos = RepoBundle.from_pool(pool)
    uid = repos.user_repo.create(username="owner", role="user")
    repos.agent_repo.create(agent_id="agent", user_id=uid, name="Assistant")
    repos.thread_repo.insert(
        thread_id="thread", agent_id="agent", user_id=uid, channel_type="web", session_key="s"
    )
    service = RunService(repos)
    repos.run_repo.create("run", "agent", "thread", uid, await service.version("agent", uid))
    row = repos.run_repo.claim("run", service.node)
    base = InMemorySaver()
    saver = FencedSaver(base, service, "agent")
    token = execution_context.set(Execution(service, row, asyncio.Lock()))
    config = {"configurable": {"thread_id": "thread", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"answer": "accepted"}
    try:
        saved = await saver.aput(config, checkpoint, {}, {})
        repos.run_repo.cancel("run", uid)
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"answer": "stale"}
        with pytest.raises(OctopError):
            await saver.aput(saved, checkpoint, {}, {})
    finally:
        execution_context.reset(token)
    current = await saver.aget_tuple(config)
    assert current.checkpoint["channel_values"]["answer"] == "accepted"
    pool.close()
