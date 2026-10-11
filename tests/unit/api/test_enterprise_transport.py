from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from octop.api.middleware.enterprise import EnterpriseMiddleware
from octop.infra.users.enterprise import runtime_access


def make_app(row):
    user = SimpleNamespace(id=7, is_admin=False, username="employee", department_id="sales")
    repo = SimpleNamespace(get=lambda _: row)
    service = SimpleNamespace(
        runtime_for=AsyncMock(return_value="hidden-runtime"), validate_use=AsyncMock()
    )
    server = SimpleNamespace(services=SimpleNamespace(agent_repo=repo), enterprise=service)
    app = FastAPI()
    app.add_middleware(EnterpriseMiddleware, server=server)

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.octop_user = user
        return await call_next(request)

    @app.get("/api/agents/{agent_id}/threads")
    async def threads(agent_id: str):
        assert runtime_access.get() == ("hidden-runtime", 7)
        return [{"agent_id": agent_id, "url": f"/api/agents/{agent_id}/workspace/download"}]

    @app.get("/api/agents/{agent_id}/workspace/tree")
    async def files(agent_id: str):
        return {"unsafe": True}

    return app, service


def test_department_threads_use_hidden_runtime_but_return_public_id():
    app, service = make_app(
        SimpleNamespace(
            agent_id="department",
            user_id=None,
            enterprise_kind="department",
            enabled=1,
            department_id="sales",
        )
    )
    with TestClient(app) as client:
        response = client.get("/api/agents/department/threads")
    assert response.status_code == 200
    assert response.json() == [
        {"agent_id": "department", "url": "/api/agents/department/workspace/download"}
    ]
    service.runtime_for.assert_awaited_once()


def test_department_user_cannot_browse_definition_workspace():
    app, _ = make_app(
        SimpleNamespace(
            agent_id="department",
            user_id=None,
            enterprise_kind="department",
            enabled=1,
            department_id="sales",
        )
    )
    with TestClient(app) as client:
        assert client.get("/api/agents/department/workspace/tree").status_code == 403


def test_hidden_runtime_cannot_be_used_directly():
    app, service = make_app(SimpleNamespace(agent_id="hidden-runtime", enterprise_kind="runtime"))
    with TestClient(app) as client:
        assert client.get("/api/agents/hidden-runtime/threads").status_code == 403
    service.runtime_for.assert_not_awaited()
