from pathlib import Path

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool


def test_durable_run_schema_and_repeat_migration(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "runs.db")
    try:
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0
    finally:
        pool.close()
