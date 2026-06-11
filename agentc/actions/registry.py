"""Action factory: maps a spec's ``type`` to an Action subclass."""

from __future__ import annotations

from typing import Any, Dict, Type

from .base import Action
from .shell import ShellAction
from .python_action import PythonAction
from .agent_action import AgentAction, ToolAction

_REGISTRY: Dict[str, Type[Action]] = {
    ShellAction.type: ShellAction,
    PythonAction.type: PythonAction,
    AgentAction.type: AgentAction,
    ToolAction.type: ToolAction,
}


def register_action(cls: Type[Action]) -> Type[Action]:
    """Decorator / function to register a custom Action subtype."""
    _REGISTRY[cls.type] = cls
    return cls


def known_types() -> list[str]:
    return sorted(_REGISTRY)


def build_action(spec: Dict[str, Any]) -> Action:
    atype = spec.get("type")
    if atype not in _REGISTRY:
        raise ValueError(
            f"unknown action type {atype!r}; known types: {', '.join(known_types())}"
        )
    return _REGISTRY[atype](spec)
