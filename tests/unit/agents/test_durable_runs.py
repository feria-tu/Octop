from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle
from octop.infra.runs.service import RunService


@pytest.mark.asyncio
async def test_stream_persists_events_before_delivery_and_commits_result(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "run.db")
    run_migrations(pool)
    repos = RepoBundle.from_pool(pool)
    uid = repos.user_repo.create(username="owner", role="user")
    repos.agent_repo.create(agent_id="agent", user_id=uid, name="Assistant")
    repos.thread_repo.insert(
        thread_id="thread", agent_id="agent", user_id=uid, channel_type="web", session_key="session"
    )

    async def source():
        yield {"type": "text", "text": "hello"}
        yield {"type": "state_snapshot", "data": {"messages": []}}

    service = RunService(repos)
    delivered = []
    async for event in service.stream("agent", {"thread_id": "thread", "user": str(uid)}, source):
        delivered.append(event)
        saved = repos.run_repo.events(event["run_id"], after=event["seq"] - 1)
        assert saved[0]["seq"] == event["seq"]
    run = repos.run_repo.get(delivered[0]["run_id"])
    assert run.state == "succeeded"
    assert run.result_json is not None
    assert repos.run_repo.events(run.run_id)[-1]["type"] == "done"
    pool.close()
