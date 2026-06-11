"""The Action interface and the common run lifecycle.

Subtypes (ShellAction, PythonAction, AgentAction, ...) only implement
:meth:`execute`. The base class handles the parts every action shares:
template rendering of its spec, an optional ``when`` guard, capturing output
into variables, and emitting events on success.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from ..models import ActionResult
from ..variables import Context

log = logging.getLogger("agentc.action")


class Action(ABC):
    """Base class for everything the engine can execute as a step.

    Common spec fields (all optional unless noted):
        name        — identifier, also the key its result is stored under
        type        — action type (required; selects the subclass)
        when        — guard; the action is skipped unless this renders truthy
        capture     — variable name to store this action's stdout into
        set         — mapping of variables to set after the action runs
        emits       — event name (or list) to emit on success
        on_failure  — "stop" (default) or "continue"
    """

    type: str = "base"

    def __init__(self, spec: Dict[str, Any]):
        self.spec = spec
        self.name: str = spec.get("name", self.type)
        self.when = spec.get("when")
        self.capture = spec.get("capture")
        self.set_vars: Dict[str, Any] = spec.get("set", {}) or {}
        emits = spec.get("emits", [])
        self.emits: List[str] = [emits] if isinstance(emits, str) else list(emits)
        self.on_failure: str = spec.get("on_failure", "stop")

    # -- subclasses implement this ----------------------------------------- #
    @abstractmethod
    def execute(self, context: Context) -> ActionResult:
        """Do the work. Receives a context with templates *already available*
        via ``context.render`` — subclasses render the fields they need."""

    # -- shared lifecycle -------------------------------------------------- #
    def run(self, context: Context) -> ActionResult:
        start = time.time()

        if self.when is not None:
            guard = context.render(self.when)
            if not _truthy(guard):
                log.info("action %r skipped (when=%r)", self.name, guard)
                return ActionResult(self.name, self.type, success=True,
                                    outputs={"skipped": True})

        try:
            result = self.execute(context)
        except Exception as exc:  # noqa: BLE001
            log.exception("action %r crashed", self.name)
            result = ActionResult(self.name, self.type, success=False,
                                  error=str(exc), exit_code=1)

        result.duration = round(time.time() - start, 4)

        # Make the result visible to later actions immediately.
        context.results[self.name] = result

        if result.success:
            self._post_success(context, result)
        return result

    def _post_success(self, context: Context, result: ActionResult) -> None:
        if self.capture:
            context.variables.set(self.capture, result.stdout.strip())
        if self.set_vars:
            context.variables.update(context.render(self.set_vars))
        for name in self.emits:
            rendered = context.render(name)
            context.emit(str(rendered), {"action": self.name, "task":
                         getattr(context.task, "name", None)})


def resolve_path(path: str) -> str:
    """Resolve a (possibly relative) action path.

    A relative path is tried against the current directory first, then against
    ``$AGENTC_ROOT`` (the project root, set by the launcher). This lets a task's
    ``executable`` reference bundled scripts no matter where the CLI runs from.
    """
    if not path or os.path.isabs(path) or os.path.exists(path):
        return path
    root = os.environ.get("AGENTC_ROOT")
    if root:
        candidate = os.path.join(root, path)
        if os.path.exists(candidate):
            return candidate
    return path


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "none")
    return bool(value)
