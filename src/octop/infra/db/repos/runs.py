"""Transactional durable runs; no runtime or transport dependencies."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool


class RunConflict(Exception):
    """Execution is stale, unauthorized, or requires reconciliation."""


def encode(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


@dataclass(frozen=True)
class RunRow:
    run_id: str
    agent_id: str
    thread_id: str
    user_id: int
    version: str
    state: str
    node: str | None
    fence: int
    lease_until: int
    request_json: str
    result_json: str | None
    next_seq: int

    @classmethod
    def from_row(cls, row: Any) -> RunRow:
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


class RunRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def _lock(self, conn: Any, run_id: str) -> RunRow:
        suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
        row = conn.execute(
            "SELECT * FROM agent_runs WHERE run_id = ?" + suffix, (run_id,)
        ).fetchone()
        if row is None:
            raise RunConflict("run_missing")
        return RunRow.from_row(row)

    def _live(self, row: RunRow, node: str, fence: int, now: int | None = None) -> None:
        if (
            row.state != "running"
            or row.node != node
            or row.fence != fence
            or row.lease_until <= (now if now is not None else int(time.time()))
        ):
            raise RunConflict("execution_stale")

    def _event(self, conn: Any, row: RunRow, payload: dict[str, Any]) -> dict[str, Any]:
        seq = row.next_seq + 1
        if payload.get("type") in {"token", "text", "state_snapshot"}:
            partial = json.loads(row.result_json) if row.result_json else {"text": ""}
            partial["partial"] = True
            if payload["type"] == "state_snapshot":
                partial["state"] = payload.get("data")
            else:
                partial["text"] = str(partial.get("text") or "") + str(
                    payload.get("content") or payload.get("text") or ""
                )
            conn.execute(
                "UPDATE agent_runs SET result_json=? WHERE run_id=?", (encode(partial), row.run_id)
            )
        event = {**payload, "run_id": row.run_id, "seq": seq, "thread_id": row.thread_id}
        conn.execute(
            "UPDATE agent_runs SET next_seq = ?, updated_at = ? WHERE run_id = ?",
            (seq, int(time.time()), row.run_id),
        )
        conn.execute(
            "INSERT INTO run_events(run_id,seq,payload_json,created_at) VALUES (?,?,?,?)",
            (row.run_id, seq, encode(event), int(time.time())),
        )
        return event

    def terminal_event(self, run_id: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM run_events WHERE run_id=? AND seq=(SELECT terminal_seq FROM agent_runs WHERE run_id=?)",
                (run_id, run_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, run_id: str) -> RunRow | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return RunRow.from_row(row) if row is not None else None

    def checkpoint_get(
        self, agent_id: str, thread_id: str, namespace: str, checkpoint_id: str | None = None
    ) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            if checkpoint_id is None:
                anchor = conn.execute(
                    "SELECT config_json FROM run_checkpoints WHERE agent_id=? AND thread_id=? AND namespace=?",
                    (agent_id, thread_id, namespace),
                ).fetchone()
                if anchor is None:
                    return None
                checkpoint_id = json.loads(anchor[0])["configurable"]["checkpoint_id"]
            row = conn.execute(
                "SELECT * FROM run_checkpoint_entries WHERE agent_id=? AND thread_id=? AND namespace=? AND checkpoint_id=?",
                (agent_id, thread_id, namespace, checkpoint_id),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["writes"] = [
                dict(w)
                for w in conn.execute(
                    "SELECT * FROM run_checkpoint_writes WHERE agent_id=? AND thread_id=? AND namespace=? AND checkpoint_id=? ORDER BY task_id,idx",
                    (agent_id, thread_id, namespace, checkpoint_id),
                ).fetchall()
            ]
            return result

    def checkpoint_put(
        self,
        run_id: str,
        node: str,
        fence: int,
        namespace: str,
        checkpoint_id: str,
        parent_id: str | None,
        checkpoint: tuple[str, str],
        metadata: tuple[str, str],
        promote: bool = True,
    ) -> None:
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            self._live(row, node, fence)
            conn.execute(
                "INSERT INTO run_checkpoint_entries(agent_id,thread_id,namespace,checkpoint_id,parent_id,checkpoint_type,checkpoint_data,metadata_type,metadata_data,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_id,thread_id,namespace,checkpoint_id) DO NOTHING",
                (
                    row.agent_id,
                    row.thread_id,
                    namespace,
                    checkpoint_id,
                    parent_id,
                    *checkpoint,
                    *metadata,
                    int(time.time()),
                ),
            )
            config = {
                "configurable": {
                    "thread_id": row.thread_id,
                    "checkpoint_ns": namespace,
                    "checkpoint_id": checkpoint_id,
                }
            }
            if promote:
                conn.execute(
                    "INSERT INTO run_checkpoints(agent_id,thread_id,namespace,config_json) VALUES (?,?,?,?) ON CONFLICT(agent_id,thread_id,namespace) DO UPDATE SET config_json=excluded.config_json",
                    (row.agent_id, row.thread_id, namespace, encode(config)),
                )

    def checkpoint_writes(
        self,
        run_id: str,
        node: str,
        fence: int,
        namespace: str,
        checkpoint_id: str,
        task_id: str,
        task_path: str,
        writes: list[tuple[int, str, str, str]],
    ) -> None:
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            self._live(row, node, fence)
            for idx, channel, kind, data in writes:
                suffix = (
                    "DO UPDATE SET channel=excluded.channel,value_type=excluded.value_type,value_data=excluded.value_data,task_path=excluded.task_path"
                    if idx < 0
                    else "DO NOTHING"
                )
                conn.execute(
                    "INSERT INTO run_checkpoint_writes(agent_id,thread_id,namespace,checkpoint_id,task_id,idx,channel,value_type,value_data,task_path) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_id,thread_id,namespace,checkpoint_id,task_id,idx) "
                    + suffix,
                    (
                        row.agent_id,
                        row.thread_id,
                        namespace,
                        checkpoint_id,
                        task_id,
                        idx,
                        channel,
                        kind,
                        data,
                        task_path,
                    ),
                )

    def checkpoint_list(
        self, agent_id: str, thread_id: str, namespace: str, before: str | None, limit: int
    ) -> list[str]:
        sql = "SELECT checkpoint_id FROM run_checkpoint_entries WHERE agent_id=? AND thread_id=? AND namespace=?"
        args: list[Any] = [agent_id, thread_id, namespace]
        if before is not None:
            sql += " AND checkpoint_id<?"
            args.append(before)
        with self._db.connect() as conn:
            return [
                str(r[0])
                for r in conn.execute(
                    sql + " ORDER BY checkpoint_id DESC LIMIT ?", (*args, limit)
                ).fetchall()
            ]

    def checkpoint_delete(self, agent_id: str, thread_id: str) -> None:
        with self._db.transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM agent_runs WHERE agent_id=? AND thread_id=? LIMIT 1",
                    (agent_id, thread_id),
                ).fetchone()
                is not None
            ):
                raise RunConflict("run_retention")
            for table in ("run_checkpoints", "run_checkpoint_entries", "run_checkpoint_writes"):
                conn.execute(
                    f"DELETE FROM {table} WHERE agent_id=? AND thread_id=?", (agent_id, thread_id)
                )

    def latest(self, agent_id: str, thread_id: str) -> RunRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE agent_id=? AND thread_id=? ORDER BY id DESC LIMIT 1",
                (agent_id, thread_id),
            ).fetchone()
        return RunRow.from_row(row) if row is not None else None

    def active(self, agent_id: str, thread_id: str) -> RunRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE agent_id=? AND thread_id=? AND state IN ('queued','running','waiting_approval','recovery_blocked')",
                (agent_id, thread_id),
            ).fetchone()
        return RunRow.from_row(row) if row is not None else None

    def create(
        self,
        run_id: str,
        agent_id: str,
        thread_id: str,
        user_id: int,
        version: str,
        request_json: str = "{}",
    ) -> RunRow:
        with self._db.transaction() as conn:
            # Lock the parent so competing PostgreSQL creators serialize as SQLite does.
            suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
            thread = conn.execute(
                "SELECT * FROM threads WHERE thread_id=?" + suffix, (thread_id,)
            ).fetchone()
            if thread is None or thread["agent_id"] != agent_id or thread["user_id"] != user_id:
                raise RunConflict("thread_owner")
            existing = conn.execute(
                "SELECT run_id FROM agent_runs WHERE agent_id=? AND thread_id=? AND state IN ('queued','running','waiting_approval','recovery_blocked')",
                (agent_id, thread_id),
            ).fetchone()
            if existing is not None:
                raise RunConflict("run_active")
            if (
                conn.execute(
                    "SELECT 1 FROM tool_executions e JOIN agent_runs r ON r.run_id=e.run_id WHERE r.agent_id=? AND r.thread_id=? AND e.state IN ('started','uncertain') LIMIT 1",
                    (agent_id, thread_id),
                ).fetchone()
                is not None
            ):
                raise RunConflict("write_uncertain")
            now = int(time.time())
            conn.execute(
                "INSERT INTO agent_runs(run_id,agent_id,thread_id,user_id,version,created_at,updated_at,request_json) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, agent_id, thread_id, user_id, version, now, now, request_json),
            )
            return self._lock(conn, run_id)

    def claim(self, run_id: str, node: str, now: int | None = None) -> RunRow:
        clock = int(time.time()) if now is None else now
        with self._db.transaction() as conn:
            # Use the same parent-first lock order as creation.
            identity = conn.execute(
                "SELECT thread_id FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if identity is None:
                raise RunConflict("run_missing")
            suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
            conn.execute(
                "SELECT thread_id FROM threads WHERE thread_id=?" + suffix, (identity[0],)
            ).fetchone()
            row = self._lock(conn, run_id)
            latest = conn.execute(
                "SELECT run_id FROM agent_runs WHERE agent_id=? AND thread_id=? ORDER BY id DESC LIMIT 1",
                (row.agent_id, row.thread_id),
            ).fetchone()
            if latest is None or latest[0] != run_id:
                raise RunConflict("run_superseded")
            if row.state not in {"queued", "waiting_approval", "recovery_blocked", "failed"}:
                raise RunConflict("run_not_resumable")
            unresolved = conn.execute(
                "SELECT 1 FROM tool_executions WHERE run_id=? AND state IN ('started','uncertain')",
                (run_id,),
            ).fetchone()
            if unresolved is not None:
                raise RunConflict("write_uncertain")
            conn.execute(
                "UPDATE agent_runs SET state='running',node=?,fence=fence+1,lease_until=?,updated_at=?,terminal_seq=NULL WHERE run_id=?",
                (node, clock + 30, clock, run_id),
            )
            return self._lock(conn, run_id)

    def assert_live(self, run_id: str, node: str, fence: int, version: str) -> RunRow:
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            self._live(row, node, fence)
            if row.version != version:
                raise RunConflict("version_changed")
            return row

    def heartbeat(self, run_id: str, node: str, fence: int, now: int | None = None) -> bool:
        clock = int(time.time()) if now is None else now
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            try:
                self._live(row, node, fence, clock)
            except RunConflict:
                return False
            conn.execute(
                "UPDATE agent_runs SET lease_until=?,updated_at=? WHERE run_id=?",
                (clock + 30, clock, run_id),
            )
            return True

    def cancel(self, run_id: str, user_id: int) -> RunRow:
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            if row.user_id != user_id:
                raise RunConflict("run_owner")
            if row.state in {"succeeded", "cancelled"}:
                return row
            conn.execute(
                "UPDATE agent_runs SET state='cancelled',fence=fence+1,lease_until=0 WHERE run_id=?",
                (run_id,),
            )
            conn.execute(
                "UPDATE tool_executions SET state='uncertain' WHERE run_id=? AND state='started'",
                (run_id,),
            )
            conn.execute(
                "UPDATE run_approvals SET state='expired' WHERE run_id=? AND state='pending'",
                (run_id,),
            )
            terminal = self._event(
                conn,
                row,
                {
                    "type": "done",
                    "state": "cancelled",
                    "result": json.loads(row.result_json) if row.result_json else {"partial": True},
                },
            )
            conn.execute(
                "UPDATE agent_runs SET terminal_seq=? WHERE run_id=?", (terminal["seq"], run_id)
            )
            return self._lock(conn, run_id)

    def pause(
        self,
        run_id: str,
        node: str,
        fence: int,
        state: str = "waiting_approval",
        *,
        result: dict[str, Any] | None = None,
    ) -> RunRow:
        if state not in {"waiting_approval", "recovery_blocked", "failed"}:
            raise RunConflict("invalid_pause")
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            self._live(row, node, fence)
            conn.execute(
                "UPDATE agent_runs SET state=?,lease_until=0,result_json=COALESCE(?,result_json) WHERE run_id=?",
                (state, encode(result) if result is not None else None, run_id),
            )
            self._event(conn, row, {"type": "run_status", "state": state})
            return self._lock(conn, run_id)

    def expire(self, now: int | None = None) -> int:
        clock = int(time.time()) if now is None else now
        with self._db.transaction() as conn:
            suffix = " FOR UPDATE" if self._db.dialect == "postgresql" else ""
            rows = conn.execute(
                "SELECT * FROM agent_runs WHERE state='running' AND lease_until<=?" + suffix,
                (clock,),
            ).fetchall()
            for raw in rows:
                row = RunRow.from_row(raw)
                conn.execute(
                    "UPDATE tool_executions SET state='uncertain' WHERE run_id=? AND state='started'",
                    (row.run_id,),
                )
                conn.execute(
                    "UPDATE agent_runs SET state='recovery_blocked',fence=fence+1,lease_until=0 WHERE run_id=?",
                    (row.run_id,),
                )
                self._event(
                    conn,
                    row,
                    {"type": "run_status", "state": "recovery_blocked", "reason": "lease_expired"},
                )
            return len(rows)

    def append_event(
        self, run_id: str, node: str, fence: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            self._live(row, node, fence)
            return self._event(conn, row, payload)

    def events(self, run_id: str, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after, limit),
            ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def commit(
        self, run_id: str, node: str, fence: int, result: dict[str, Any], state: str = "succeeded"
    ) -> dict[str, Any]:
        if state not in {"succeeded", "failed"}:
            raise RunConflict("invalid_terminal")
        encoded = encode(result)
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            if (
                row.state == state
                and row.result_json == encoded
                and row.node == node
                and row.fence == fence
            ):
                last = conn.execute(
                    "SELECT payload_json FROM run_events WHERE run_id=? AND seq=(SELECT terminal_seq FROM agent_runs WHERE run_id=?)",
                    (run_id, run_id),
                ).fetchone()
                return dict(json.loads(last[0]))
            self._live(row, node, fence)
            if (
                state == "succeeded"
                and conn.execute(
                    "SELECT 1 FROM tool_executions WHERE run_id=? AND state IN ('started','uncertain')",
                    (run_id,),
                ).fetchone()
                is not None
            ):
                raise RunConflict("write_uncertain")
            conn.execute(
                "UPDATE agent_runs SET state=?,result_json=?,lease_until=0 WHERE run_id=?",
                (state, encoded, run_id),
            )
            event = self._event(conn, row, {"type": "done", "state": state, "result": result})
            conn.execute(
                "UPDATE agent_runs SET terminal_seq=? WHERE run_id=?", (event["seq"], run_id)
            )
            return event

    def record_partial(self, run_id: str, node: str, fence: int, result: dict[str, Any]) -> None:
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            if (
                row.node != node
                or row.fence != fence
                or row.state not in {"running", "recovery_blocked"}
            ):
                return
            conn.execute(
                "UPDATE agent_runs SET result_json=? WHERE run_id=?", (encode(result), run_id)
            )
            conn.execute(
                "UPDATE tool_executions SET state='uncertain' WHERE run_id=? AND state='started'",
                (run_id,),
            )
            if (
                conn.execute(
                    "SELECT 1 FROM tool_executions WHERE run_id=? AND state='uncertain'", (run_id,)
                ).fetchone()
                is not None
            ):
                conn.execute(
                    "UPDATE agent_runs SET state='recovery_blocked',lease_until=0 WHERE run_id=?",
                    (run_id,),
                )

    def configure_tool(self, name: str, read_only: bool, enabled: bool = True) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO business_tools(name,read_only,enabled) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET read_only=excluded.read_only,enabled=excluded.enabled,revision=business_tools.revision+1",
                (name, int(read_only), int(enabled)),
            )

    def grant(self, agent_id: str, user_id: int, name: str, enabled: bool = True) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO business_tool_grants(agent_id,user_id,tool_name,enabled) VALUES (?,?,?,?) ON CONFLICT(agent_id,user_id,tool_name) DO UPDATE SET enabled=excluded.enabled,revision=business_tool_grants.revision+1",
                (agent_id, user_id, name, int(enabled)),
            )

    def tools(self) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM business_tools ORDER BY name").fetchall()
            ]

    def grants(self, name: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM business_tool_grants WHERE tool_name=? ORDER BY agent_id,user_id",
                    (name,),
                ).fetchall()
            ]

    def _policy(self, conn: Any, row: RunRow, name: str) -> dict[str, Any] | None:
        tool = conn.execute("SELECT * FROM business_tools WHERE name=?", (name,)).fetchone()
        if tool is None:
            return None
        grant = conn.execute(
            "SELECT * FROM business_tool_grants WHERE agent_id=? AND user_id=? AND tool_name=?",
            (row.agent_id, row.user_id, name),
        ).fetchone()
        if not tool["enabled"] or grant is None or not grant["enabled"]:
            raise RunConflict("tool_forbidden")
        return {
            "read_only": bool(tool["read_only"]),
            "revision": f"{tool['revision']}:{grant['revision']}",
        }

    def policy(self, run_id: str, name: str) -> dict[str, Any] | None:
        with self._db.transaction() as conn:
            return self._policy(conn, self._lock(conn, run_id), name)

    def approval(
        self,
        run_id: str,
        node: str,
        fence: int,
        call_id: str,
        name: str,
        args_json: str,
        digest: str,
        approval_id: str,
    ) -> dict[str, Any]:
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            self._live(row, node, fence)
            policy = self._policy(conn, row, name)
            if policy is None:
                raise RunConflict("tool_unregistered")
            old = conn.execute(
                "SELECT * FROM run_approvals WHERE run_id=? AND call_id=?", (run_id, call_id)
            ).fetchone()
            if old is not None:
                if (
                    old["tool_name"] != name
                    or old["args_digest"] != digest
                    or old["policy_revision"] != policy["revision"]
                ):
                    raise RunConflict("approval_changed")
                return dict(old)
            conn.execute(
                "INSERT INTO run_approvals(approval_id,run_id,call_id,tool_name,args_json,args_digest,policy_revision,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    approval_id,
                    run_id,
                    call_id,
                    name,
                    args_json,
                    digest,
                    policy["revision"],
                    int(time.time()),
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM run_approvals WHERE approval_id=?", (approval_id,)
                ).fetchone()
            )

    def approvals(self, run_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM run_approvals WHERE run_id=? ORDER BY id", (run_id,)
                ).fetchall()
            ]

    def decide(self, approval_id: str, user_id: int, digest: str, approved: bool) -> None:
        with self._db.transaction() as conn:
            first = conn.execute(
                "SELECT run_id FROM run_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
            if first is None:
                raise RunConflict("approval_missing")
            row = self._lock(conn, first[0])
            approval = conn.execute(
                "SELECT * FROM run_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
            policy = self._policy(conn, row, approval["tool_name"])
            if (
                row.user_id != user_id
                or row.state not in {"running", "waiting_approval"}
                or approval["args_digest"] != digest
                or policy is None
                or policy["revision"] != approval["policy_revision"]
            ):
                raise RunConflict("approval_stale")
            state = "approved" if approved else "rejected"
            if approval["state"] == state:
                return
            if approval["state"] != "pending":
                raise RunConflict("approval_decided")
            conn.execute(
                "UPDATE run_approvals SET state=?,decided_at=? WHERE approval_id=?",
                (state, int(time.time()), approval_id),
            )
            self._event(
                conn,
                row,
                {
                    "type": "approval_decided",
                    "approval_id": approval_id,
                    "state": state,
                    "args_digest": digest,
                },
            )

    def begin_write(
        self, run_id: str, node: str, fence: int, call_id: str, name: str, digest: str
    ) -> dict[str, Any]:
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            self._live(row, node, fence)
            policy = self._policy(conn, row, name)
            approval = conn.execute(
                "SELECT * FROM run_approvals WHERE run_id=? AND call_id=?", (run_id, call_id)
            ).fetchone()
            if (
                policy is None
                or policy["read_only"]
                or approval is None
                or approval["state"] != "approved"
                or approval["args_digest"] != digest
                or approval["tool_name"] != name
                or approval["policy_revision"] != policy["revision"]
            ):
                raise RunConflict("approval_required")
            old = conn.execute(
                "SELECT * FROM tool_executions WHERE run_id=? AND call_id=?", (run_id, call_id)
            ).fetchone()
            if old is not None:
                if old["tool_name"] != name or old["args_digest"] != digest:
                    raise RunConflict("call_changed")
                if old["state"] == "succeeded":
                    return {"action": "replay", "result": json.loads(old["result_json"])}
                if old["state"] != "not_executed":
                    raise RunConflict("write_uncertain")
                conn.execute(
                    "UPDATE tool_executions SET state='started',fence=? WHERE run_id=? AND call_id=?",
                    (fence, run_id, call_id),
                )
            else:
                conn.execute(
                    "INSERT INTO tool_executions(run_id,call_id,tool_name,args_digest,fence,state) VALUES (?,?,?,?,?,'started')",
                    (run_id, call_id, name, digest, fence),
                )
            self._event(
                conn,
                row,
                {
                    "type": "write_started",
                    "call_id": call_id,
                    "tool_name": name,
                    "args_digest": digest,
                },
            )
            return {"action": "execute"}

    def complete_write(
        self, run_id: str, node: str, fence: int, call_id: str, result: dict[str, Any]
    ) -> None:
        encoded = encode(result)
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            self._live(row, node, fence)
            old = conn.execute(
                "SELECT * FROM tool_executions WHERE run_id=? AND call_id=?", (run_id, call_id)
            ).fetchone()
            if old is None or old["fence"] != fence or old["state"] not in {"started", "succeeded"}:
                raise RunConflict("write_stale")
            if old["state"] == "succeeded":
                if old["result_json"] != encoded:
                    raise RunConflict("result_changed")
                return
            conn.execute(
                "UPDATE tool_executions SET state='succeeded',result_json=? WHERE run_id=? AND call_id=?",
                (encoded, run_id, call_id),
            )
            self._event(conn, row, {"type": "write_succeeded", "call_id": call_id})

    def uncertain(self, run_id: str, node: str, fence: int, call_id: str) -> None:
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            # Even an expired worker may flag only its own unfinished operation.
            conn.execute(
                "UPDATE tool_executions SET state='uncertain' WHERE run_id=? AND call_id=? AND fence=? AND state='started'",
                (run_id, call_id, fence),
            )
            if row.node == node and row.fence == fence and row.state == "running":
                conn.execute(
                    "UPDATE agent_runs SET state='recovery_blocked',lease_until=0 WHERE run_id=?",
                    (run_id,),
                )
                self._event(
                    conn,
                    row,
                    {
                        "type": "run_status",
                        "state": "recovery_blocked",
                        "reason": "write_uncertain",
                        "call_id": call_id,
                    },
                )

    def executions(self, run_id: str) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM tool_executions WHERE run_id=? ORDER BY call_id", (run_id,)
                ).fetchall()
            ]

    def resolve_write(
        self, run_id: str, call_id: str, admin_id: int, note: str, result: dict[str, Any] | None
    ) -> None:
        if not note.strip():
            raise RunConflict("resolution_note_required")
        with self._db.transaction() as conn:
            row = self._lock(conn, run_id)
            admin = conn.execute(
                "SELECT role,disabled FROM users WHERE id=?", (admin_id,)
            ).fetchone()
            if (
                admin is None
                or admin["role"] != "admin"
                or admin["disabled"]
                or row.state not in {"recovery_blocked", "cancelled", "failed"}
            ):
                raise RunConflict("resolution_forbidden")
            old = conn.execute(
                "SELECT * FROM tool_executions WHERE run_id=? AND call_id=?", (run_id, call_id)
            ).fetchone()
            if old is None or old["state"] != "uncertain":
                raise RunConflict("write_not_uncertain")
            state = "succeeded" if result is not None else "not_executed"
            conn.execute(
                "UPDATE tool_executions SET state=?,result_json=?,resolved_by=?,resolution_note=? WHERE run_id=? AND call_id=?",
                (
                    state,
                    encode(result) if result is not None else None,
                    admin_id,
                    note,
                    run_id,
                    call_id,
                ),
            )
            self._event(
                conn,
                row,
                {
                    "type": "write_resolved",
                    "call_id": call_id,
                    "state": state,
                    "admin_id": admin_id,
                    "note": note,
                },
            )
