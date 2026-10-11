"""Deleting a conversation must not erase an unresolved external operation."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.runs import RunConflict
from octop.infra.db.services import RepoBundle


@pytest.fixture
def cancelled_write(tmp_path: Path) -> Iterator[RepoBundle]:
    pool = SqlitePool(tmp_path / "retention.db")
    run_migrations(pool)
    repos = RepoBundle.from_pool(pool)
    uid = repos.user_repo.create(username="owner", role="user")
    repos.agent_repo.create(agent_id="agent", user_id=uid, name="Assistant")
    repos.thread_repo.insert(
        thread_id="thread", agent_id="agent", user_id=uid, channel_type="web", session_key="session"
    )
    repo = repos.run_repo
    repo.create("run", "agent", "thread", uid, "v1")
    execution = repo.claim("run", "worker")
    repo.checkpoint_put(
        "run",
        "worker",
        execution.fence,
        "",
        "checkpoint",
        None,
        ("json", "state"),
        ("json", "metadata"),
    )
    repo.configure_tool("submit", read_only=False)
    repo.grant("agent", uid, "submit")
    repo.approval("run", "worker", execution.fence, "call", "submit", "{}", "digest", "approval")
    repo.decide("approval", uid, "digest", approved=True)
    repo.begin_write("run", "worker", execution.fence, "call", "submit", "digest")
    repo.cancel("run", uid)
    try:
        yield repos
    finally:
        pool.close()


def test_cancelled_uncertain_write_prevents_thread_deletion(cancelled_write: RepoBundle) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        cancelled_write.thread_repo.delete("thread")
    assert cancelled_write.thread_repo.get("thread") is not None
    assert cancelled_write.run_repo.get("run").state == "cancelled"
    assert cancelled_write.run_repo.executions("run")[0]["state"] == "uncertain"


def test_cancelled_uncertain_write_blocks_new_turn(cancelled_write: RepoBundle) -> None:
    run = cancelled_write.run_repo.get("run")
    with pytest.raises(RunConflict):
        cancelled_write.run_repo.create("replacement", "agent", "thread", run.user_id, "v1")


def test_cancelled_uncertain_write_prevents_checkpoint_deletion(
    cancelled_write: RepoBundle,
) -> None:
    with pytest.raises(RunConflict):
        cancelled_write.run_repo.checkpoint_delete("agent", "thread")
    checkpoint = cancelled_write.run_repo.checkpoint_get("agent", "thread", "")
    assert checkpoint is not None
    assert checkpoint["checkpoint_id"] == "checkpoint"
    assert cancelled_write.run_repo.executions("run")[0]["state"] == "uncertain"
