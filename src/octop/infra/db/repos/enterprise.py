"""SQL persistence for company departments and managed runtimes."""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts


@dataclass(frozen=True)
class DepartmentRow:
    department_id: str
    name: str
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> DepartmentRow:
        return cls(str(row["department_id"]), str(row["name"]), int(row["created_at"]))


@dataclass(frozen=True)
class ManagedRuntimeRow:
    definition_id: str
    user_id: int
    runtime_id: str
    definition_version: int


class EnterpriseRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def departments(self) -> list[DepartmentRow]:
        with self._db.connect() as conn:
            return [
                DepartmentRow.from_row(r)
                for r in conn.execute("SELECT * FROM departments ORDER BY name").fetchall()
            ]

    def department(self, department_id: str) -> DepartmentRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM departments WHERE department_id = ?", (department_id,)
            ).fetchone()
        return DepartmentRow.from_row(row) if row else None

    def create_department(self, department_id: str, name: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO departments(department_id, name, created_at) VALUES (?, ?, ?)",
                (department_id, name, now_ts()),
            )

    def rename_department(self, department_id: str, name: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE departments SET name = ? WHERE department_id = ?", (name, department_id)
            )

    def delete_department(self, department_id: str) -> bool:
        with self._db.transaction() as conn:
            users = conn.execute(
                "SELECT COUNT(*) FROM users WHERE department_id = ?", (department_id,)
            ).fetchone()
            agents = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE department_id = ?", (department_id,)
            ).fetchone()
            if users is not None and agents is not None and (users[0] or agents[0]):
                return False
            conn.execute("DELETE FROM departments WHERE department_id = ?", (department_id,))
        return True

    def assign_department(self, user_id: int, department_id: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET department_id = ? WHERE id = ?", (department_id, user_id)
            )

    def mark_agent(self, agent_id: str, kind: str, department_id: str | None = None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE agents SET enterprise_kind = ?, department_id = ?, is_shared = 0 WHERE agent_id = ?",
                (kind, department_id, agent_id),
            )

    def bump_definition(self, agent_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE agents SET definition_version = definition_version + 1 WHERE agent_id = ?",
                (agent_id,),
            )

    def assistant_status(self, user_id: int) -> dict[str, str | None]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT agent_id, status, error FROM employee_assistants WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return (
            {"agent_id": row["agent_id"], "status": row["status"], "error": row["error"]}
            if row
            else {"agent_id": None, "status": "pending", "error": None}
        )

    def set_assistant_status(
        self, user_id: int, agent_id: str | None, status: str, error: str | None = None
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO employee_assistants(user_id, agent_id, status, error) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET agent_id = excluded.agent_id, status = excluded.status, error = excluded.error",
                (user_id, agent_id, status, error),
            )

    def runtimes(
        self, *, definition_id: str | None = None, user_id: int | None = None
    ) -> list[ManagedRuntimeRow]:
        sql = "SELECT * FROM department_runtimes WHERE 1 = 1"
        args: list[object] = []
        if definition_id is not None:
            sql += " AND definition_id = ?"
            args.append(definition_id)
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        with self._db.connect() as conn:
            return [
                ManagedRuntimeRow(
                    str(r["definition_id"]),
                    int(r["user_id"]),
                    str(r["runtime_id"]),
                    int(r["definition_version"]),
                )
                for r in conn.execute(sql, args).fetchall()
            ]

    def bind_runtime(self, definition_id: str, user_id: int, runtime_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO department_runtimes(definition_id, user_id, runtime_id) VALUES (?, ?, ?)",
                (definition_id, user_id, runtime_id),
            )

    def sync_runtime(self, runtime_id: str, version: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE department_runtimes SET definition_version = ? WHERE runtime_id = ?",
                (version, runtime_id),
            )
