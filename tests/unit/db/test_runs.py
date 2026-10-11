"""Durable execution acceptance tests through the public repository API."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.runs import RunConflict, RunRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo


@pytest.fixture
def run_db(tmp_path: Path) -> tuple[SqlitePool, int]:
    pool = SqlitePool(tmp_path / "runs.db")
    run_migrations(pool)
    user_id = UserRepo(pool).create(username="owner", role="user")
    AgentRepo(pool).create(agent_id="agent", user_id=user_id, name="Assistant")
    ThreadRepo(pool).insert(
        thread_id="thread",
        agent_id="agent",
        user_id=user_id,
        channel_type="web",
        session_key="session",
    )
    return pool, user_id


def test_cancellation_rejects_old_execution_commit(run_db: tuple[SqlitePool, int]) -> None:
    pool, user_id = run_db
    repo = RunRepo(pool)
    repo.create("run", "agent", "thread", user_id, "version-1")
    execution = repo.claim("run", "worker")

    cancelled = repo.cancel("run", user_id)

    assert cancelled.state == "cancelled"
    with pytest.raises(RunConflict):
        repo.commit("run", "worker", execution.fence, {"text": "late result"})
    assert repo.get("run").state == "cancelled"
    assert repo.active("agent", "thread") is None


def test_resume_invalidates_old_execution(run_db: tuple[SqlitePool, int]) -> None:
    pool, user_id = run_db
    repo = RunRepo(pool)
    repo.create("run", "agent", "thread", user_id, "v1")
    old = repo.claim("run", "worker-a")
    repo.pause("run", "worker-a", old.fence)
    resumed = repo.claim("run", "worker-b")
    assert resumed.fence > old.fence
    with pytest.raises(RunConflict):
        repo.append_event("run", "worker-a", old.fence, {"type": "text"})
    with pytest.raises(RunConflict):
        repo.assert_live("run", "worker-b", resumed.fence, "v2")
    assert not repo.heartbeat("run", "worker-a", old.fence)
    repo.assert_live("run", "worker-b", resumed.fence, "v1")


def test_two_database_connections_cannot_claim_same_run(
    run_db: tuple[SqlitePool, int], tmp_path: Path
) -> None:
    pool, user_id = run_db
    first = RunRepo(pool)
    second = RunRepo(SqlitePool(tmp_path / "runs.db"))
    first.create("run", "agent", "thread", user_id, "v1")
    first.claim("run", "worker-a")
    with pytest.raises(RunConflict):
        second.claim("run", "worker-b")
    with pytest.raises(RunConflict):
        second.create("other-run", "agent", "thread", user_id, "v1")


def test_simultaneous_claim_has_only_one_winner(
    run_db: tuple[SqlitePool, int], tmp_path: Path
) -> None:
    pool, user_id = run_db
    RunRepo(pool).create("run", "agent", "thread", user_id, "v1")
    repos = [RunRepo(pool), RunRepo(SqlitePool(tmp_path / "runs.db"))]
    barrier = Barrier(2)

    def claim(index: int) -> str:
        barrier.wait(timeout=10)
        try:
            repos[index].claim("run", f"worker-{index}")
        except RunConflict:
            return "rejected"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(claim, range(2))) == ["claimed", "rejected"]


def test_committed_result_and_done_are_persistent_and_idempotent(
    run_db: tuple[SqlitePool, int], tmp_path: Path
) -> None:
    pool, user_id = run_db
    repo = RunRepo(pool)
    repo.create("run", "agent", "thread", user_id, "v1")
    execution = repo.claim("run", "worker")
    first = repo.append_event("run", "worker", execution.fence, {"type": "text", "text": "a"})
    done = repo.commit("run", "worker", execution.fence, {"text": "answer"})
    reopened = RunRepo(SqlitePool(tmp_path / "runs.db"))
    assert reopened.get("run").state == "succeeded"
    assert reopened.commit("run", "worker", execution.fence, {"text": "answer"}) == done
    with pytest.raises(RunConflict):
        reopened.commit("run", "worker", execution.fence, {"text": "different"})
    events = reopened.events("run")
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert reopened.events("run", after=first["seq"], limit=1) == [done]
    assert done["run_id"] == "run"
    assert sum(event["type"] == "done" for event in events) == 1


def test_failed_final_transaction_leaves_no_result_or_done(
    run_db: tuple[SqlitePool, int], tmp_path: Path
) -> None:
    class FailingCommitPool(SqlitePool):
        @contextmanager
        def transaction(self) -> Iterator[sqlite3.Connection]:
            with super().transaction() as conn:
                yield conn
                raise sqlite3.OperationalError("simulated commit failure")

    pool, user_id = run_db
    repo = RunRepo(pool)
    repo.create("run", "agent", "thread", user_id, "v1")
    execution = repo.claim("run", "worker")
    events_before = repo.events("run")
    failing = RunRepo(FailingCommitPool(tmp_path / "runs.db"))
    with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
        failing.commit("run", "worker", execution.fence, {"text": "answer"})
    assert repo.get("run").state == "running"
    assert repo.get("run").result_json is None
    assert repo.events("run") == events_before
    repo.commit("run", "worker", execution.fence, {"text": "answer"})
    assert repo.get("run").state == "succeeded"


def test_expired_execution_cannot_commit_and_requires_explicit_recovery(
    run_db: tuple[SqlitePool, int],
) -> None:
    pool, user_id = run_db
    repo = RunRepo(pool)
    repo.create("run", "agent", "thread", user_id, "v1")
    execution = repo.claim("run", "worker", now=1)
    assert repo.expire(now=100000) == 1
    with pytest.raises(RunConflict):
        repo.commit("run", "worker", execution.fence, {"text": "late"})
    assert repo.get("run").state == "recovery_blocked"
    assert repo.expire(now=100000) == 0


def test_other_user_cannot_cancel_run(run_db: tuple[SqlitePool, int]) -> None:
    pool, user_id = run_db
    repo = RunRepo(pool)
    repo.create("run", "agent", "thread", user_id, "v1")
    with pytest.raises(RunConflict):
        repo.cancel("run", user_id + 1)
    assert repo.get("run").state == "queued"


def test_failed_run_cannot_resume_after_a_new_run_completed_same_thread(
    run_db: tuple[SqlitePool, int],
) -> None:
    pool, user_id = run_db
    repo = RunRepo(pool)
    repo.create("old", "agent", "thread", user_id, "v1")
    old = repo.claim("old", "worker")
    repo.commit("old", "worker", old.fence, {"text": "partial"}, state="failed")
    repo.create("new", "agent", "thread", user_id, "v1")
    new = repo.claim("new", "worker")
    repo.commit("new", "worker", new.fence, {"text": "new answer"})
    with pytest.raises(RunConflict):
        repo.claim("old", "worker")
    assert repo.latest("agent", "thread").run_id == "new"
    assert repo.active("agent", "thread") is None


@pytest.fixture
def write_run(run_db: tuple[SqlitePool, int]) -> tuple[RunRepo, int, int]:
    pool, user_id = run_db
    repo = RunRepo(pool)
    repo.create("run", "agent", "thread", user_id, "v1")
    execution = repo.claim("run", "worker")
    repo.configure_tool("submit", read_only=False)
    repo.grant("agent", user_id, "submit")
    repo.approval(
        "run", "worker", execution.fence, "call", "submit", '{"amount":1}', "digest", "approval"
    )
    return repo, user_id, execution.fence


@pytest.mark.parametrize("changed", ["user", "digest", "grant", "tool", "cancel"])
def test_approval_rejects_changed_authority_or_parameters(
    write_run: tuple[RunRepo, int, int], changed: str
) -> None:
    repo, user_id, _ = write_run
    decision_user = user_id
    digest = "digest"
    if changed == "user":
        decision_user += 1
    elif changed == "digest":
        digest = "different"
    elif changed == "grant":
        repo.grant("agent", user_id, "submit", enabled=False)
    elif changed == "tool":
        repo.configure_tool("submit", read_only=False)
    else:
        repo.cancel("run", user_id)
    with pytest.raises(RunConflict):
        repo.decide("approval", decision_user, digest, approved=True)
    assert repo.approvals("run")[0]["state"] != "approved"


def test_successful_write_replays_and_rejects_changed_call(
    write_run: tuple[RunRepo, int, int],
) -> None:
    repo, user_id, fence = write_run
    with pytest.raises(RunConflict):
        repo.begin_write("run", "worker", fence, "call", "submit", "digest")
    repo.decide("approval", user_id, "digest", approved=True)
    assert repo.begin_write("run", "worker", fence, "call", "submit", "digest") == {
        "action": "execute"
    }
    with pytest.raises(RunConflict):
        repo.begin_write("run", "worker", fence, "call", "submit", "digest")
    repo.complete_write("run", "worker", fence, "call", {"business_id": "record"})
    assert repo.begin_write("run", "worker", fence, "call", "submit", "digest") == {
        "action": "replay",
        "result": {"business_id": "record"},
    }
    with pytest.raises(RunConflict):
        repo.begin_write("run", "worker", fence, "call", "submit", "different")
    with pytest.raises(RunConflict):
        repo.complete_write("run", "worker", fence, "call", {"business_id": "other"})


def test_uncertain_write_requires_admin_resolution_before_resume(
    write_run: tuple[RunRepo, int, int], run_db: tuple[SqlitePool, int]
) -> None:
    repo, user_id, fence = write_run
    pool, _ = run_db
    admin_id = UserRepo(pool).create(username="admin", role="admin")
    repo.decide("approval", user_id, "digest", approved=True)
    repo.begin_write("run", "worker", fence, "call", "submit", "digest")
    repo.uncertain("run", "worker", fence, "call")
    with pytest.raises(RunConflict):
        repo.claim("run", "new-worker")
    with pytest.raises(RunConflict):
        repo.resolve_write("run", "call", user_id, "checked system", None)
    with pytest.raises(RunConflict):
        repo.resolve_write("run", "call", admin_id, "", None)
    repo.resolve_write("run", "call", admin_id, "confirmed no business record", None)
    resumed = repo.claim("run", "new-worker")
    assert resumed.fence > fence
    assert repo.begin_write("run", "new-worker", resumed.fence, "call", "submit", "digest") == {
        "action": "execute"
    }
    with pytest.raises(RunConflict):
        repo.complete_write("run", "worker", fence, "call", {"business_id": "late"})


def test_reconciled_success_replays_without_reexecuting(
    write_run: tuple[RunRepo, int, int], run_db: tuple[SqlitePool, int]
) -> None:
    repo, user_id, fence = write_run
    pool, _ = run_db
    admin_id = UserRepo(pool).create(username="admin", role="admin")
    repo.decide("approval", user_id, "digest", approved=True)
    repo.begin_write("run", "worker", fence, "call", "submit", "digest")
    repo.uncertain("run", "worker", fence, "call")
    repo.resolve_write(
        "run", "call", admin_id, "verified business record", {"business_id": "record"}
    )
    resumed = repo.claim("run", "new-worker")
    assert repo.begin_write("run", "new-worker", resumed.fence, "call", "submit", "digest") == {
        "action": "replay",
        "result": {"business_id": "record"},
    }
