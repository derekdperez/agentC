"""Loading and validating agent / task configuration files."""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

from .actions.registry import build_action
from .agents.adapters import ADAPTERS
from .models import AgentConfig, Task
from .store import load_structured

_CONFIG_EXTS = (".json", ".yaml", ".yml")


def _iter_config_files(directory: str):
    if not os.path.isdir(directory):
        return
    for name in sorted(os.listdir(directory)):
        if name.endswith(_CONFIG_EXTS) and not name.startswith("_"):
            yield os.path.join(directory, name)


def load_agents(directory: str) -> Dict[str, AgentConfig]:
    agents: Dict[str, AgentConfig] = {}
    for path in _iter_config_files(directory):
        data = load_structured(path)
        config = AgentConfig.from_dict(data)
        agents[config.name] = config
    return agents


def load_tasks(directory: str) -> Dict[str, Task]:
    tasks: Dict[str, Task] = {}
    for path in _iter_config_files(directory):
        data = load_structured(path)
        task = Task.from_dict(data)
        tasks[task.name] = task
    return tasks


def validate_agent(config: AgentConfig) -> List[str]:
    errors: List[str] = []
    if config.cli not in ADAPTERS:
        errors.append(f"agent {config.name!r}: unknown cli {config.cli!r}")
    if not config.name:
        errors.append("agent is missing a name")
    return errors


def validate_task(task: Task, agents: Dict[str, AgentConfig]) -> List[str]:
    errors: List[str] = []
    if not task.actions:
        errors.append(f"task {task.name!r}: has no actions")
    for i, spec in enumerate(task.actions):
        label = spec.get("name", f"#{i}")
        try:
            build_action(spec)
        except ValueError as exc:
            errors.append(f"task {task.name!r} action {label}: {exc}")
        if spec.get("type") in ("agent", "tool"):
            agent_name = spec.get("agent")
            if agent_name and agent_name not in agents:
                errors.append(
                    f"task {task.name!r} action {label}: unknown agent {agent_name!r}")

    t = task.trigger
    if t.type == "schedule" and not (t.cron or t.interval):
        errors.append(f"task {task.name!r}: schedule trigger needs 'cron' or 'interval'")
    if t.type == "event" and not t.event:
        errors.append(f"task {task.name!r}: event trigger needs 'event'")
    if t.type == "file" and not t.path:
        errors.append(f"task {task.name!r}: file trigger needs 'path'")
    return errors


def validate_all(agents: Dict[str, AgentConfig],
                 tasks: Dict[str, Task]) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    for agent in agents.values():
        errors += validate_agent(agent)
    for task in tasks.values():
        errors += validate_task(task, agents)
    return (not errors), errors
