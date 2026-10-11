"""Durable control-plane contracts against a dedicated PostgreSQL database."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos.runs import RunConflict
from octop.infra.db.services import RepoBundle
from octop.infra.runs.service import RunService
from tests.integration.test_postgresql_control_plane import _conninfo, _reset_public_schema
from tests.support.postgresql import requires_postgresql
from tests.unit.agents.test_run_graph import graph_protocol, resume, start

pytestmark = [requires_postgresql, pytest.mark.postgresql]


@pytest.fixture
def pg_runs():
    pool = PostgresPool(_conninfo())
    _reset_public_schema(pool)
    run_migrations(pool)
    repos = RepoBundle.from_pool(pool)
    uid = repos.user_repo.create(username="run-owner", role="user")
    repos.agent_repo.create(agent_id="agent", user_id=uid, name="Run assistant")
    repos.thread_repo.insert(
        thread_id="thread",
        agent_id="agent",
        user_id=uid,
        channel_type="web",
        session_key="pg-session",
    )
    try:
        yield repos, uid
    finally:
        pool.close()


def test_postgresql_claim_has_one_winner_and_cancel_fences_it(pg_runs):
    repos, uid = pg_runs
    repo = repos.run_repo
    repo.create("run", "agent", "thread", uid, "v1")
    ready = Barrier(2)

    def claim(node):
        ready.wait()
        try:
            return repo.claim("run", node)
        except RunConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, ["a", "b"]))
    winners = [row for row in results if row is not None]
    assert len(winners) == 1
    winner = winners[0]
    repo.append_event("run", winner.node, winner.fence, {"type": "token", "content": "partial"})
    repo.cancel("run", uid)
    with pytest.raises(RunConflict):
        repo.commit("run", winner.node, winner.fence, {"text": "late"})
    assert repo.terminal_event("run")["result"]["text"] == "partial"


@pytest.mark.asyncio
async def test_postgresql_graph_approval_and_checkpoints_survive_recreation(pg_runs):
    repos, uid = pg_runs
    repos.run_repo.configure_tool("write_business", read_only=False)
    repos.run_repo.grant("agent", uid, "write_business")
    effects = []
    first = RunService(repos)
    events = await start(first, graph_protocol(first, effects, calls=2), uid)
    run_id = events[0]["run_id"]
    assert effects == []
    assert len(repos.run_repo.approvals(run_id)) == 2
    recreated = RunService(repos)
    completed = await resume(
        recreated,
        graph_protocol(recreated, effects, calls=2),
        uid,
        run_id,
        [{"type": "approve"}, {"type": "approve"}],
    )
    assert sorted(effects) == [1, 2]
    assert completed[-1]["state"] == "succeeded"
    replay = repos.run_repo.events(run_id)
    assert [event["seq"] for event in replay] == list(range(1, len(replay) + 1))
    assert repos.run_repo.terminal_event(run_id) == completed[-1]
