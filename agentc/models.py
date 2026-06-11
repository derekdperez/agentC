"""Core data models shared across the engine.

These are deliberately plain dataclasses so they serialize cleanly to the
JSON/YAML files that back the engine's state.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
@dataclass
class AgentConfig:
    """Configuration for a single agent.

    An agent binds a CLI *tool* (claude, opencode, codex, gemini) to an API
    *provider*, a *model* and a *system prompt*.
    """

    name: str
    cli: str                      # claude | opencode | codex | gemini | mock
    provider: str = "anthropic"   # anthropic | openai | nvidia | opencode | ...
    model: str = ""
    system_prompt: str = ""
    api_key_env: Optional[str] = None   # env var holding the API key
    base_url: Optional[str] = None      # provider base URL override
    extra_args: List[str] = field(default_factory=list)
    timeout: Optional[int] = None   # seconds to cap a run; None/0 = no limit
    mock: bool = False            # force the deterministic mock runner
    description: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------- #
# Tasks & triggers
# --------------------------------------------------------------------------- #
@dataclass
class Trigger:
    """How a task is started.

    type:
        manual   — only via the CLI / API (default)
        schedule — cron or interval based
        event    — fired when a named event is emitted
        file     — fired by a Linux filesystem event on a watched path
    """

    type: str = "manual"
    # schedule
    cron: Optional[str] = None          # "*/5 * * * *"
    interval: Optional[float] = None    # seconds
    # event
    event: Optional[str] = None         # event name (supports trailing '*')
    # file
    path: Optional[str] = None          # file or directory to watch
    on: str = "created"                 # created | modified | deleted | moved | any
    pattern: str = "*"                  # glob filter for filenames
    recursive: bool = False
    ignore: List[str] = field(default_factory=list)  # path/name globs to skip

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Trigger":
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Task:
    name: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)
    trigger: Trigger = field(default_factory=Trigger)
    description: str = ""
    emits: List[str] = field(default_factory=list)   # events emitted on success
    enabled: bool = True
    persist: bool = True   # write a run record to disk (off for noisy meta tasks)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        trigger = Trigger.from_dict(data.get("trigger", {}) or {})
        emits = data.get("emits", [])
        if isinstance(emits, str):
            emits = [emits]
        return cls(
            name=data["name"],
            actions=data.get("actions", []),
            variables=dict(data.get("variables", {})),
            trigger=trigger,
            description=data.get("description", ""),
            emits=emits,
            enabled=data.get("enabled", True),
            persist=data.get("persist", True),
        )


# --------------------------------------------------------------------------- #
# Runtime results & events
# --------------------------------------------------------------------------- #
@dataclass
class ActionResult:
    name: str
    type: str
    success: bool = True
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Event:
    """Something that happened. Actions and the engine emit these; triggers
    consume them."""

    name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    source: str = "engine"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunRecord:
    """The durable record of a single task execution."""

    task: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"   # running | success | failed
    trigger: str = "manual"
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    results: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
