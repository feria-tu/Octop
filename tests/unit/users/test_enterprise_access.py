"""Acceptance criteria for enterprise agent access."""

from types import SimpleNamespace

import pytest

from octop.infra.users.enterprise_access import agent_access_policy


def test_employee_can_use_own_department_agent_without_management() -> None:
    row = SimpleNamespace(
        user_id=1, is_shared=0, enterprise_kind="department", department_id="sales"
    )
    user = SimpleNamespace(id=2, is_admin=False, department_id="sales")

    policy = agent_access_policy(row, user)

    assert policy.can_use is True
    assert policy.can_manage is False
    assert policy.can_edit_profile is False


@pytest.mark.parametrize("shared", [0, 1])
def test_department_agent_denies_other_department_even_when_shared(shared: int) -> None:
    row = SimpleNamespace(
        user_id=2, is_shared=shared, enterprise_kind="department", department_id="sales"
    )
    user = SimpleNamespace(id=2, is_admin=False, department_id="engineering")

    policy = agent_access_policy(row, user)

    assert policy.can_use is False
    assert policy.can_manage is False
    assert policy.can_edit_profile is False


@pytest.mark.parametrize("department_id", [None, "engineering"])
def test_employee_without_matching_department_is_denied(
    department_id: str | None,
) -> None:
    row = SimpleNamespace(
        user_id=1, is_shared=1, enterprise_kind="department", department_id="sales"
    )
    user = SimpleNamespace(id=2, is_admin=False, department_id=department_id)

    policy = agent_access_policy(row, user)

    assert policy.can_use is False
    assert policy.can_manage is False
    assert policy.can_edit_profile is False


def test_personal_assistant_owner_has_limited_profile_access() -> None:
    row = SimpleNamespace(user_id=2, is_shared=0, enterprise_kind="personal", department_id=None)
    user = SimpleNamespace(id=2, is_admin=False, department_id="sales")

    policy = agent_access_policy(row, user)

    assert policy.can_use is True
    assert policy.can_edit_profile is True
    assert policy.can_manage is False


@pytest.mark.parametrize("shared", [0, 1])
def test_personal_assistant_is_not_shared_with_other_employees(shared: int) -> None:
    row = SimpleNamespace(
        user_id=1, is_shared=shared, enterprise_kind="personal", department_id="sales"
    )
    user = SimpleNamespace(id=2, is_admin=False, department_id="sales")

    policy = agent_access_policy(row, user)

    assert policy.can_use is False
    assert policy.can_manage is False
    assert policy.can_edit_profile is False


@pytest.mark.parametrize("kind", ["personal", "department"])
def test_administrator_can_manage_enterprise_agents(kind: str) -> None:
    row = SimpleNamespace(user_id=1, is_shared=0, enterprise_kind=kind, department_id="sales")
    user = SimpleNamespace(id=2, is_admin=True, department_id="engineering")

    policy = agent_access_policy(row, user)

    assert policy.can_use is True
    assert policy.can_manage is True
    assert policy.can_edit_profile is True


@pytest.mark.parametrize("is_admin", [False, True])
@pytest.mark.parametrize("user_id", [1, 2])
def test_hidden_runtime_denies_public_access_even_for_owner_or_admin(
    is_admin: bool, user_id: int
) -> None:
    row = SimpleNamespace(user_id=1, is_shared=1, enterprise_kind="runtime", department_id="sales")
    user = SimpleNamespace(id=user_id, is_admin=is_admin, department_id="sales")

    policy = agent_access_policy(row, user)

    assert policy.can_use is False
    assert policy.can_manage is False
    assert policy.can_edit_profile is False


@pytest.mark.parametrize("explicit_legacy", [False, True])
@pytest.mark.parametrize(
    ("owner_id", "shared", "is_admin", "can_use", "can_manage"),
    [
        (2, 0, False, True, True),
        (1, 0, False, False, False),
        (1, 1, False, True, False),
        (None, 0, False, False, False),
        (None, 1, False, True, False),
        (1, 0, True, True, True),
    ],
)
def test_legacy_agent_keeps_owner_shared_and_admin_behavior(
    explicit_legacy: bool,
    owner_id: int | None,
    shared: int,
    is_admin: bool,
    can_use: bool,
    can_manage: bool,
) -> None:
    row = SimpleNamespace(user_id=owner_id, is_shared=shared)
    if explicit_legacy:
        row.enterprise_kind = "legacy"
        row.department_id = None
    user = SimpleNamespace(id=2, is_admin=is_admin, department_id="sales")

    policy = agent_access_policy(row, user)

    assert policy.can_use is can_use
    assert policy.can_manage is can_manage
    assert policy.can_edit_profile is can_manage


@pytest.mark.parametrize("kind", ["legacy", "personal", "department", "runtime"])
@pytest.mark.parametrize("is_admin", [False, True])
def test_disabled_user_has_no_access_even_with_ownership_or_admin_role(
    kind: str, is_admin: bool
) -> None:
    row = SimpleNamespace(user_id=2, is_shared=1, enterprise_kind=kind, department_id="sales")
    user = SimpleNamespace(id=2, is_admin=is_admin, department_id="sales", disabled=True)

    policy = agent_access_policy(row, user)

    assert policy.can_use is False
    assert policy.can_manage is False
    assert policy.can_edit_profile is False
