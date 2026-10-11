from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import OctopError
from octop.infra.users.enterprise import EnterpriseService
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def enterprise(tmp_path: Path):
    pool = SqlitePool(tmp_path / "test.db")
    run_migrations(pool)
    services = SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(pool))
    registry = SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("provider unavailable")))
    server = SimpleNamespace(
        services=services, app_runtime=SimpleNamespace(agent_registry=registry)
    )
    yield EnterpriseService(server), services
    pool.close()


@pytest.mark.asyncio
async def test_non_admin_cannot_create_department(enterprise):
    service, _ = enterprise
    with pytest.raises(OctopError):
        await service.create_department(SimpleNamespace(is_admin=False), "Sales")


@pytest.mark.asyncio
async def test_failed_assistant_creation_is_retryable_without_losing_account(enterprise):
    service, services = enterprise
    user_id = services.user_repo.create(username="employee", role="user")
    services.enterprise_repo.create_department("sales", "Sales")
    services.enterprise_repo.assign_department(user_id, "sales")
    status = await service.ensure_personal_assistant(user_id)
    assert status["status"] == "failed"
    assert services.user_repo.get(user_id) is not None
    assert status["agent_id"] is None


@pytest.mark.asyncio
async def test_department_with_members_cannot_be_deleted(enterprise):
    service, services = enterprise
    admin = SimpleNamespace(is_admin=True, username="admin")
    department = await service.create_department(admin, "Sales")
    user_id = services.user_repo.create(username="employee", role="user")
    services.enterprise_repo.assign_department(user_id, department.department_id)
    with pytest.raises(OctopError):
        await service.delete_department(admin, department.department_id)
    assert services.enterprise_repo.department(department.department_id) is not None
