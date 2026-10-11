from pathlib import Path

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool


def test_enterprise_schema_is_idempotent(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "enterprise.db")
    try:
        run_migrations(pool)
        run_migrations(pool)
        with pool.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 0
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
            assert {"enterprise_kind", "department_id", "definition_version"} <= columns
            assert conn.execute("SELECT COUNT(*) FROM employee_assistants").fetchone()[0] == 0
    finally:
        pool.close()


def test_interrupted_enterprise_migration_preserves_existing_department(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "interrupted.db")
    try:
        run_migrations(pool)
        with pool.connect() as conn:
            conn.execute(
                "INSERT INTO departments(department_id, name, created_at) VALUES (?, ?, ?)",
                ("sales", "Sales", 1),
            )
            conn.execute("DROP TABLE department_runtimes")
            conn.execute("UPDATE _schema_version SET version = 20")

        run_migrations(pool)
        run_migrations(pool)

        with pool.connect() as conn:
            assert conn.execute("SELECT name FROM departments").fetchone()[0] == "Sales"
            assert conn.execute("SELECT COUNT(*) FROM department_runtimes").fetchone()[0] == 0
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 22
    finally:
        pool.close()


def test_enterprise_schema_repairs_version_ahead_database(tmp_path: Path) -> None:
    pool = SqlitePool(tmp_path / "version-ahead.db")
    try:
        initial = (
            Path(__file__).resolve().parents[3] / "src/octop/infra/db/migrations/001_initial.sql"
        )
        with pool.connect() as conn:
            conn.executescript(initial.read_text(encoding="utf-8"))
            conn.execute("UPDATE _schema_version SET version = 99")

        run_migrations(pool)

        with pool.connect() as conn:
            assert "department_id" in {
                row["name"] for row in conn.execute("PRAGMA table_info(users)")
            }
            assert "enterprise_kind" in {
                row["name"] for row in conn.execute("PRAGMA table_info(agents)")
            }
            assert conn.execute("SELECT version FROM _schema_version").fetchone()[0] == 22
    finally:
        pool.close()
