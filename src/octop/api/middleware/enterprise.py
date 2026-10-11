"""Translate public department agent routes into authenticated private runtimes."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any
from urllib.parse import parse_qs

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from octop.api.deps import extract_raw_token, resolve_user_from_token
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.enterprise import db_call, runtime_access
from octop.infra.users.enterprise_access import agent_access_policy

_PUBLIC_FIELDS = frozenset(
    {
        "agent_id",
        "id",
        "session_key",
        "url",
        "preview_url",
        "download_url",
        "icon_url",
        "access_url",
    }
)


def public_payload(value: Any, internal: str, public: str, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: public_payload(v, internal, public, k) for k, v in value.items()}
    if isinstance(value, list):
        return [public_payload(v, internal, public, key) for v in value]
    if isinstance(value, str) and key in {"path", "workspace_path", "artifacts"}:
        normalized = value.replace("\\", "/")
        marker = f"/agents/{internal}/"
        if marker in normalized:
            return normalized.split(marker, 1)[1]
    if isinstance(value, str) and key in _PUBLIC_FIELDS:
        return value.replace(internal, public)
    return value


class EnterpriseMiddleware:
    def __init__(self, app: ASGIApp, server: Any) -> None:
        self.app = app
        self.server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        parts = path.split("/")
        if (
            scope["type"] not in {"http", "websocket"}
            or len(parts) < 4
            or parts[1:3] != ["api", "agents"]
            or not parts[3]
            or self.server.services is None
        ):
            await self.app(scope, receive, send)
            return
        response_started = False
        original_send = send

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await original_send(message)

        send = tracked_send
        definition_id = parts[3]
        row = await db_call(self.server.services.agent_repo.get, definition_id)
        kind = getattr(row, "enterprise_kind", "legacy")
        if not isinstance(kind, str) or kind == "legacy":
            await self.app(scope, receive, send)
            return
        try:
            if kind == "runtime":
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            connection = HTTPConnection(scope)
            user = scope.get("state", {}).get("octop_user")
            if user is None:
                raw = extract_raw_token(
                    authorization=connection.headers.get("authorization"),
                    access_token=connection.query_params.get("access_token")
                    or connection.query_params.get("token"),
                )
                if not raw:
                    raise OctopError.localized(ErrorCode.AUTH_FAILED)
                user = resolve_user_from_token(self.server, raw)
            if not agent_access_policy(row, user).can_use:
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            suffix = "/" + "/".join(parts[4:]) if len(parts) > 4 else ""
            method = scope.get("method", "GET")
            runtime_route = suffix.startswith(("/chat", "/threads")) or suffix in {
                "/upload",
                "/read",
                "/workspace/download",
                "/workspace/file",
                "/media/preview",
            }
            if not user.is_admin:
                allowed = (
                    runtime_route
                    or (method == "GET" and suffix in {"", "/status", "/avatar"})
                    or (kind == "personal" and method == "PATCH" and not suffix)
                )
                if not allowed or (suffix.startswith(("/workspace", "/media")) and method != "GET"):
                    raise OctopError.localized(ErrorCode.FORBIDDEN)
                for key in ("path", "source"):
                    requested = connection.query_params.get(key, "").replace("\\", "/")
                    if any(
                        part.startswith(".") for part in requested.split("/") if part
                    ) or requested.lstrip("/").split("/")[0] in {
                        "skills",
                        "SOUL.md",
                        "AGENTS.md",
                        "MEMORY.md",
                    }:
                        raise OctopError.localized(ErrorCode.FORBIDDEN)
            if kind != "department" or not runtime_route:
                if (
                    user.is_admin
                    and method == "GET"
                    and suffix.startswith(("/threads", "/workspace", "/media"))
                ):
                    await self.server.enterprise.audit(
                        user, "assistant.audit.read", definition_id, path
                    )

                async def checked_send(message: Message) -> None:
                    if message["type"] in {"http.response.body", "websocket.send"}:
                        await self.server.enterprise.validate_use(definition_id, user.id)
                    await send(message)

                await self.app(scope, receive, checked_send)
                return
            query = parse_qs(scope.get("query_string", b"").decode())
            try:
                target_user_id = int(query.get("as_user", [str(user.id)])[0])
            except ValueError as exc:
                raise OctopError.localized(ErrorCode.FORBIDDEN, status=400) from exc
            audit_actor = user if user.is_admin and target_user_id != user.id else None
            if target_user_id != user.id and audit_actor is None:
                raise OctopError.localized(ErrorCode.FORBIDDEN)
            if audit_actor is not None:
                if method != "GET" or scope["type"] != "http":
                    raise OctopError.localized(ErrorCode.FORBIDDEN)
                await self.server.enterprise.audit(
                    user, "assistant.audit.read", definition_id, str(target_user_id)
                )
            internal_id = await self.server.enterprise.runtime_for(
                definition_id, target_user_id, audit_actor=audit_actor
            )
        except OctopError as exc:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            elif response_started:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                await JSONResponse(exc.to_envelope(), status_code=exc.status)(scope, receive, send)
            return
        mapped_scope = dict(scope)
        mapped_scope["path"] = path.replace(
            f"/api/agents/{definition_id}", f"/api/agents/{internal_id}", 1
        )
        mapped_scope["raw_path"] = mapped_scope["path"].encode()
        mapped_scope["headers"] = [
            (k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"
        ]
        token = runtime_access.set((internal_id, user.id))
        response_start: Message | None = None
        chunks = bytearray()
        sse = False
        json_response = False

        request_read = False

        async def mapped_receive() -> Message:
            nonlocal request_read
            message = await receive()
            if message["type"] == "http.request" and not request_read:
                request_read = True
                content_type = connection.headers.get("content-type", "")
                if "application/json" in content_type:
                    body = bytearray(message.get("body", b""))
                    while message.get("more_body", False):
                        message = await receive()
                        if message["type"] != "http.request":
                            return message
                        body.extend(message.get("body", b""))
                    with suppress(ValueError):
                        body = bytearray(
                            json.dumps(
                                public_payload(json.loads(body), definition_id, internal_id),
                                ensure_ascii=False,
                            ).encode()
                        )
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
            if message["type"] == "websocket.receive" and message.get("text"):
                with suppress(ValueError):
                    payload = public_payload(
                        json.loads(message["text"]), definition_id, internal_id
                    )
                    return {**message, "text": json.dumps(payload, ensure_ascii=False)}
            return message

        async def mapped_send(message: Message) -> None:
            nonlocal response_start, sse, json_response
            if message["type"] in {"http.response.body", "websocket.send"}:
                await self.server.enterprise.validate_use(
                    definition_id, target_user_id, audit_actor=audit_actor, runtime_id=internal_id
                )
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                content_type = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                sse = b"text/event-stream" in content_type
                json_response = b"application/json" in content_type
                if json_response:
                    response_start = {
                        **message,
                        "headers": [(k, v) for k, v in headers if k.lower() != b"content-length"],
                    }
                    return
            if message["type"] == "websocket.send" and message.get("text"):
                with suppress(ValueError):
                    message = {
                        **message,
                        "text": json.dumps(
                            public_payload(json.loads(message["text"]), internal_id, definition_id),
                            ensure_ascii=False,
                        ),
                    }
            if message["type"] == "http.response.body" and json_response:
                chunks.extend(message.get("body", b""))
                if message.get("more_body", False):
                    return
                if response_start is not None:
                    await send(response_start)
                data = public_payload(json.loads(chunks), internal_id, definition_id)
                message = {**message, "body": json.dumps(data, ensure_ascii=False).encode()}
            elif message["type"] == "http.response.body" and sse:
                chunks.extend(message.get("body", b""))
                output = bytearray()
                while b"\n" in chunks:
                    line, _, remainder = chunks.partition(b"\n")
                    chunks[:] = remainder
                    line_bytes = bytes(line)
                    if line_bytes.startswith(b"data: "):
                        with suppress(ValueError):
                            line_bytes = (
                                b"data: "
                                + json.dumps(
                                    public_payload(
                                        json.loads(line_bytes[6:]), internal_id, definition_id
                                    ),
                                    ensure_ascii=False,
                                ).encode()
                            )
                    output.extend(line_bytes + b"\n")
                if not message.get("more_body", False):
                    output.extend(chunks)
                    chunks.clear()
                message = {**message, "body": bytes(output)}
            await send(message)

        try:
            await self.app(mapped_scope, mapped_receive, mapped_send)
        except OctopError as exc:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            elif response_started:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                await JSONResponse(exc.to_envelope(), status_code=exc.status)(scope, receive, send)
        finally:
            runtime_access.reset(token)
