import pytest

from tests.support.auth import TEST_PASSWORD, auth_header


@pytest.mark.asyncio
async def test_run_events_owner_and_cancellation(env_with_provider):
    client, server, admin = env_with_provider
    schema = client._octop_app.openapi()
    paths = schema["paths"]
    assert paths["/api/runs/{run_id}/resume"]["post"]["summary"]
    assert "RunOut" in schema["components"]["schemas"]
    response = await client.post("/api/agents", headers=admin, json={"name": "Run assistant"})
    assert response.status_code == 201, response.text
    aid = response.json()["agent_id"]
    response = await client.post(f"/api/agents/{aid}/threads", headers=admin)
    assert response.status_code == 201, response.text
    tid = response.json()["thread_id"]
    thread = server.services.thread_repo.get(tid)
    repo = server.services.run_repo
    repo.create("run-api", aid, tid, thread.user_id, "v1")
    row = repo.claim("run-api", "node")
    repo.append_event(row.run_id, "node", row.fence, {"type": "token", "content": "saved"})
    events = await client.get("/api/runs/run-api/events", headers=admin)
    assert events.status_code == 200, events.text
    assert events.json()["events"][0]["content"] == "saved"
    user = await client.post(
        "/api/users", headers=admin, json={"username": "stranger", "password": TEST_PASSWORD}
    )
    assert user.status_code == 201, user.text
    other = await auth_header(client, username="stranger")
    assert (await client.get("/api/runs/run-api/events", headers=other)).status_code == 403
    assert (await client.post("/api/runs/run-api/cancel", headers=other)).status_code == 403
    cancelled = await client.post("/api/runs/run-api/cancel", headers=admin)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    tail = await client.get("/api/runs/run-api/events?after_seq=1", headers=admin)
    assert tail.json()["events"][0]["type"] == "done"


@pytest.mark.asyncio
async def test_websocket_replays_and_tails_database_without_live_hub_publication(env_with_provider):
    from tests.support.http import chat_ws_path, ws_connect

    client, server, admin = env_with_provider
    response = await client.post("/api/agents", headers=admin, json={"name": "Replay assistant"})
    aid = response.json()["agent_id"]
    response = await client.post(f"/api/agents/{aid}/threads", headers=admin)
    tid = response.json()["thread_id"]
    owner = server.services.thread_repo.get(tid).user_id
    repo = server.services.run_repo
    repo.create("remote-run", aid, tid, owner, "v1")
    run = repo.claim("remote-run", "remote-node")
    first = repo.append_event(
        run.run_id, "remote-node", run.fence, {"type": "token", "content": "missed"}
    )
    async with ws_connect(client._octop_app, chat_ws_path(aid, admin)) as ws:
        await ws.send_json(
            {"type": "subscribe", "thread_id": tid, "run_id": run.run_id, "after_seq": 0}
        )
        assert await ws.receive_json() == first
        assert (await ws.receive_json())["active"] is True
        second = repo.append_event(
            run.run_id, "remote-node", run.fence, {"type": "token", "content": "remote"}
        )
        terminal = repo.commit(run.run_id, "remote-node", run.fence, {"text": "missedremote"})
        assert await ws.receive_json() == second
        assert await ws.receive_json() == terminal

    async with ws_connect(client._octop_app, chat_ws_path(aid, admin)) as ws:
        await ws.send_json(
            {
                "type": "subscribe",
                "thread_id": tid,
                "run_id": run.run_id,
                "after_seq": second["seq"],
            }
        )
        assert await ws.receive_json() == terminal
