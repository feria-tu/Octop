"""Enterprise account lifecycle through the real HTTP and SQLite layers."""

import asyncio

import pytest

from tests.support.auth import TEST_PASSWORD, auth_header


@pytest.mark.asyncio
async def test_employee_private_department_runtime_and_revocation(env_with_provider):
    client, server, admin = env_with_provider
    sales = await client.post("/api/enterprise/departments", headers=admin, json={"name": "Sales"})
    other = await client.post(
        "/api/enterprise/departments", headers=admin, json={"name": "Finance"}
    )
    assert sales.status_code == other.status_code == 201
    department_id = sales.json()["department_id"]
    users = []
    for name in ("alice", "bob"):
        response = await client.post(
            "/api/users",
            headers=admin,
            json={"username": name, "password": TEST_PASSWORD, "department_id": department_id},
        )
        assert response.status_code == 201, response.text
        user = response.json()
        assert user["assistant"]["status"] == "ready", user
        users.append((user, await auth_header(client, username=name)))
    alice, alice_auth = users[0]
    bob, bob_auth = users[1]
    definition = await client.post(
        f"/api/enterprise/departments/{department_id}/agents",
        headers=admin,
        json={"name": "Sales helper"},
    )
    assert definition.status_code == 201, definition.text
    aid = definition.json()["agent_id"]
    assert definition.json()["enabled"] is False
    assert (await client.get(f"/api/agents/{aid}/threads", headers=alice_auth)).status_code == 403
    published = await client.patch(
        f"/api/enterprise/agents/{aid}", headers=admin, json={"enabled": True}
    )
    assert published.status_code == 200, published.text
    created = await asyncio.gather(
        *(client.post(f"/api/agents/{aid}/threads", headers=auth) for _, auth in users)
    )
    assert all(r.status_code == 201 for r in created), [r.text for r in created]
    records = server.services.enterprise_repo.runtimes(definition_id=aid)
    assert len(records) == 2 and len({r.runtime_id for r in records}) == 2
    for response in created:
        assert aid in response.json()["session_key"]
        assert all(r.runtime_id not in response.text for r in records)
    # Use the fixture's disk-backed workspace adapter to exercise actual file I/O.
    for record in records:
        harness = server.app_runtime.agent_registry.get_agent(record.runtime_id)
        harness.use_workspace_dir(server.services.paths.agent_workspace(record.runtime_id))
    uploaded = await client.post(
        f"/api/agents/{aid}/upload",
        headers=alice_auth,
        files={"file": ("private.txt", b"alice-only", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    uploaded_path = uploaded.json()["path"]
    own_file = await client.get(
        f"/api/agents/{aid}/workspace/download", headers=alice_auth, params={"path": uploaded_path}
    )
    assert own_file.status_code == 200 and own_file.content == b"alice-only", own_file.text
    assert (
        await client.get(
            f"/api/agents/{aid}/workspace/download",
            headers=bob_auth,
            params={"path": uploaded_path},
        )
    ).status_code in {403, 404}
    alice_runtime = next(r for r in records if r.user_id == alice["id"])
    absolute = str(
        server.services.paths.agent_workspace(alice_runtime.runtime_id) / uploaded_path.lstrip("/")
    )
    assert (
        await client.get(
            f"/api/agents/{aid}/workspace/download", headers=bob_auth, params={"path": absolute}
        )
    ).status_code == 403
    own_threads = await client.get(f"/api/agents/{aid}/threads", headers=alice_auth)
    assert {r["thread_id"] for r in own_threads.json()} == {created[0].json()["thread_id"]}
    assert (
        await client.get(
            f"/api/agents/{aid}/threads/{created[1].json()['thread_id']}/history",
            headers=alice_auth,
        )
    ).status_code in {403, 404}
    assert (
        await client.get(f"/api/agents/{records[0].runtime_id}/threads", headers=admin)
    ).status_code == 403
    assert (
        await client.patch(
            f"/api/agents/{aid}", headers=alice_auth, json={"system_prompt": "override"}
        )
    ).status_code == 403
    personal = alice["assistant"]["agent_id"]
    assert (
        await client.patch(
            f"/api/agents/{personal}",
            headers=alice_auth,
            json={"name": "My helper", "icon_name": "Bot"},
        )
    ).status_code == 200
    assert (
        await client.patch(
            f"/api/agents/{personal}",
            headers=alice_auth,
            json={"config": {"backend": {"root_dir": "/"}}},
        )
    ).status_code == 403
    assert (await client.get(f"/api/agents/{personal}", headers=bob_auth)).status_code == 403
    assert (
        await client.post("/api/agents", headers=alice_auth, json={"name": "Unmanaged"})
    ).status_code == 403
    audited = await client.get(f"/api/agents/{aid}/threads?as_user={alice['id']}", headers=admin)
    assert audited.status_code == 200, audited.text
    assert server.services.audit_repo.query(actor="admin", action="assistant.audit.read")
    transferred = await client.patch(
        f"/api/users/{alice['id']}",
        headers=admin,
        json={"department_id": other.json()["department_id"]},
    )
    assert transferred.status_code == 200, transferred.text
    assert (await client.get(f"/api/agents/{aid}/threads", headers=alice_auth)).status_code == 403
    stopped = await client.patch(
        f"/api/enterprise/agents/{aid}", headers=admin, json={"enabled": False}
    )
    assert stopped.status_code == 200
    assert (await client.get(f"/api/agents/{aid}/threads", headers=bob_auth)).status_code == 403
    # Historical audit survives a transfer and publication revocation.
    assert (
        await client.get(f"/api/agents/{aid}/threads?as_user={alice['id']}", headers=admin)
    ).status_code == 200
    assert (
        await client.patch(f"/api/users/{alice['id']}", headers=admin, json={"disabled": True})
    ).status_code == 200
    assert (
        await client.get(f"/api/agents/{aid}/threads?as_user={alice['id']}", headers=admin)
    ).status_code == 200
    assert (
        await client.get(f"/api/agents/{personal}/threads?as_user={alice['id']}", headers=admin)
    ).status_code == 200
    assert (await client.delete(f"/api/users/{bob['id']}", headers=admin)).status_code == 204
    assert not server.services.enterprise_repo.runtimes(user_id=bob["id"])
