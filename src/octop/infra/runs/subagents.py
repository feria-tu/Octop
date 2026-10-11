"""Keep nested Deep Agents graphs inside the parent execution policy."""

from __future__ import annotations

from collections.abc import Callable
from types import MethodType
from typing import Any

from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

from octop.infra.runs.middleware import DurableRunMiddleware, external_tool_names


def _external_names(item: DurableRunMiddleware, names: set[str]) -> Callable[[], set[str]]:
    def resolve() -> set[str]:
        return names | (item.external_names() if item.external_names else set())

    return resolve


def govern_subagents(
    specs: list[Any], model: Any, parent_tools: list[Any], middleware: list[Any]
) -> list[Any]:
    governed = [dict(spec) for spec in specs]
    if not any(spec["name"] == "general-purpose" for spec in governed):
        governed.append({**GENERAL_PURPOSE_SUBAGENT, "model": model, "tools": parent_tools})
    policies = [
        item
        for item in middleware
        if isinstance(item, DurableRunMiddleware)
        or type(item).__name__ == "EnterpriseToolPolicyMiddleware"
    ]
    for spec in governed:
        if "runnable" in spec:
            # An opaque precompiled graph cannot be proven to enforce the policy.
            raise ValueError("Precompiled subagents cannot enter a governed execution")
        names = external_tool_names(list(spec.get("tools", parent_tools)))
        scoped_policies = [
            DurableRunMiddleware(
                item.agent_id,
                item.loaded_version,
                _external_names(item, names),
            )
            if isinstance(item, DurableRunMiddleware)
            else item
            for item in policies
        ]
        spec["middleware"] = [
            item
            for item in spec.get("middleware", [])
            if not isinstance(item, DurableRunMiddleware)
        ] + scoped_policies
    return governed


def install_subagent_policy(agent: Any) -> None:
    if getattr(agent, "_octop_subagents_governed", False):
        return
    original = getattr(agent, "_resolve_subagents", None)
    if not callable(original):
        return

    def resolve(instance: Any, *, parent_tools: list[Any] | None = None) -> list[Any]:
        return govern_subagents(
            original(parent_tools=parent_tools),
            instance._build_seed_model(),
            list(parent_tools or []),
            list(instance.config.middleware or []),
        )

    agent._resolve_subagents = MethodType(resolve, agent)
    agent._octop_subagents_governed = True
    agent._init_graph()
