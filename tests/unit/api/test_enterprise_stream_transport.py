"""Wire-level managed assistant mapping and stream revocation acceptance."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from octop.api.middleware.enterprise import EnterpriseMiddleware
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.enterprise import runtime_access


def server_for(kind: str = "department") -> tuple[SimpleNamespace, SimpleNamespace]:
    user = SimpleNamespace(id=7, is_admin=False, username="employee", department_id="sales")
    row = SimpleNamespace(
        agent_id="public",
        enterprise_kind=kind,
        user_id=7 if kind == "personal" else None,
        is_shared=0,
        enabled=True,
        department_id="sales",
    )
    enterprise = SimpleNamespace(
        runtime_for=AsyncMock(return_value="private"), validate_use=AsyncMock(), audit=AsyncMock()
    )
    server = SimpleNamespace(
        services=SimpleNamespace(agent_repo=SimpleNamespace(get=lambda _: row)),
        enterprise=enterprise,
    )
    return server, user


def scope_for(protocol: str, user: SimpleNamespace | None = None) -> dict:
    return {
        "type": protocol,
        "asgi": {"version": "3.0"},
        "scheme": "http" if protocol == "http" else "ws",
        "path": "/api/agents/public/chat/stream"
        if protocol == "http"
        else "/api/agents/public/chat/ws",
        "raw_path": b"/api/agents/public/chat/stream",
        "method": "POST",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 1),
        "state": {"octop_user": user} if user is not None else {},
    }


@pytest.mark.asyncio
async def test_multichunk_http_json_maps_session_key_and_public_response() -> None:
    server, user = server_for()
    scope = scope_for("http", user)
    payload = json.dumps({"session_key": "public:dashboard:7:dm", "text": "你好"}).encode()
    scope["headers"] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    receive = AsyncMock(
        side_effect=[
            {"type": "http.request", "body": payload[:17], "more_body": True},
            {"type": "http.request", "body": payload[17:33], "more_body": True},
            {"type": "http.request", "body": payload[33:], "more_body": False},
        ]
    )
    sent = []

    async def send(message):
        sent.append(message)

    async def app(mapped_scope, mapped_receive, mapped_send):
        assert mapped_scope["path"] == "/api/agents/private/chat/stream"
        assert runtime_access.get() == ("private", 7)
        assert not any(k == b"content-length" for k, _ in mapped_scope["headers"])
        request = await mapped_receive()
        assert request["more_body"] is False
        assert json.loads(request["body"]) == {
            "session_key": "private:dashboard:7:dm",
            "text": "你好",
        }
        await mapped_send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await mapped_send(
            {
                "type": "http.response.body",
                "body": b'{"agent_id":"private","session_key":"private:dashboard:7:dm"}',
            }
        )

    before = runtime_access.get()
    await EnterpriseMiddleware(app, server)(scope, receive, send)
    assert json.loads(sent[-1]["body"]) == {
        "agent_id": "public",
        "session_key": "public:dashboard:7:dm",
    }
    assert runtime_access.get() == before


@pytest.mark.asyncio
async def test_websocket_token_query_auth_and_bidirectional_public_ids(monkeypatch) -> None:
    server, user = server_for()
    authenticate = MagicMock(return_value=user)
    monkeypatch.setattr("octop.api.middleware.enterprise.resolve_user_from_token", authenticate)
    scope = scope_for("websocket")
    scope["query_string"] = b"token=valid-token"
    receive = AsyncMock(
        return_value={
            "type": "websocket.receive",
            "text": json.dumps({"agent_id": "public", "session_key": "public:dashboard:7:dm"}),
        }
    )
    sent = []

    async def send(message):
        sent.append(message)

    async def app(mapped_scope, mapped_receive, mapped_send):
        assert mapped_scope["path"] == "/api/agents/private/chat/ws"
        request = await mapped_receive()
        assert json.loads(request["text"]) == {
            "agent_id": "private",
            "session_key": "private:dashboard:7:dm",
        }
        await mapped_send({"type": "websocket.accept"})
        await mapped_send(
            {
                "type": "websocket.send",
                "text": json.dumps(
                    {"agent_id": "private", "session_key": "private:dashboard:7:dm"}
                ),
            }
        )

    await EnterpriseMiddleware(app, server)(scope, receive, send)
    authenticate.assert_called_once_with(server, "valid-token")
    assert json.loads(sent[-1]["text"]) == {
        "agent_id": "public",
        "session_key": "public:dashboard:7:dm",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["personal", "department"])
@pytest.mark.parametrize("protocol", ["http", "websocket"])
async def test_open_stream_does_not_forward_content_after_access_revocation(
    kind: str, protocol: str
) -> None:
    server, user = server_for(kind)
    revoked = False
    sent = []

    async def validate(*args, **kwargs):
        assert args == ("public", 7)
        if kind == "department":
            assert kwargs["runtime_id"] == "private"
        if revoked:
            raise OctopError.localized(ErrorCode.FORBIDDEN)

    server.enterprise.validate_use.side_effect = validate

    async def send(message):
        sent.append(message)

    async def app(mapped_scope, mapped_receive, mapped_send):
        nonlocal revoked
        if protocol == "http":
            await mapped_send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            await mapped_send(
                {
                    "type": "http.response.body",
                    "body": b'data: {"text":"before-revocation"}\n\n',
                    "more_body": True,
                }
            )
        else:
            await mapped_send({"type": "websocket.accept"})
            await mapped_send({"type": "websocket.send", "text": '{"text":"before-revocation"}'})
        revoked = True
        if protocol == "http":
            await mapped_send(
                {
                    "type": "http.response.body",
                    "body": b'data: {"text":"revoked-secret"}\n\n',
                    "more_body": True,
                }
            )
        else:
            await mapped_send({"type": "websocket.send", "text": '{"text":"revoked-secret"}'})

    try:
        await EnterpriseMiddleware(app, server)(scope_for(protocol, user), AsyncMock(), send)
    except OctopError as exc:
        assert exc.code is ErrorCode.FORBIDDEN
    wire = (
        b"".join(m.get("body", b"") for m in sent)
        + "".join(m.get("text", "") for m in sent).encode()
    )
    assert b"before-revocation" in wire
    assert b"revoked-secret" not in wire
    assert server.enterprise.validate_use.await_count >= 2
    if protocol == "http":
        assert sum(message["type"] == "http.response.start" for message in sent) == 1
