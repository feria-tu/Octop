"""Resource permissions shared by enterprise transports and services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentAccessPolicy:
    can_use: bool = False
    can_manage: bool = False
    can_edit_profile: bool = False


def agent_access_policy(row: Any, user: Any) -> AgentAccessPolicy:
    kind = getattr(row, "enterprise_kind", "legacy")
    disabled = getattr(user, "disabled", False)
    if disabled is True or disabled == 1 or kind == "runtime":
        return AgentAccessPolicy()
    admin = bool(user.is_admin)
    owner = row.user_id is not None and row.user_id == user.id
    if kind == "department":
        department = getattr(user, "department_id", None)
        member = department is not None and department == getattr(row, "department_id", None)
        return AgentAccessPolicy(
            admin or (member and bool(getattr(row, "enabled", True))), admin, admin
        )
    if kind == "personal":
        return AgentAccessPolicy(admin or owner, admin, admin or owner)
    return AgentAccessPolicy(
        admin or owner or bool(getattr(row, "is_shared", 0)), admin or owner, admin or owner
    )
