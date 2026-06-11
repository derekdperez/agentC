"""Variable storage and ``${...}`` template resolution.

Templates are resolved against an execution :class:`Context`:

    ${name}              -> a task / global variable
    ${env.HOME}          -> an environment variable
    ${event.path}        -> a field on the triggering event's payload
    ${greet.stdout}      -> stdout of a previously-run action named "greet"
    ${greet.exit_code}   -> that action's exit code
    ${greet.output.key}  -> a structured output the action produced

Unknown references resolve to an empty string (and are logged by the caller),
which keeps a long task from crashing on a single typo.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict

_TOKEN = re.compile(r"\$\{([^}]+)\}")


class VariableStore:
    """A small mutable key/value store with JSON-friendly values."""

    def __init__(self, initial: Dict[str, Any] | None = None):
        self._data: Dict[str, Any] = dict(initial or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, values: Dict[str, Any]) -> None:
        self._data.update(values)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data


def _resolve_token(token: str, context: "Context") -> Any:
    token = token.strip()
    head, _, tail = token.partition(".")

    if head == "env":
        return os.environ.get(tail, "")

    if head == "event" and context.event is not None:
        if not tail:
            return context.event.name
        return context.event.payload.get(tail, "")

    if head in context.results:
        result = context.results[head]
        attr = tail or "stdout"
        if attr.startswith("output."):
            return result.outputs.get(attr.split(".", 1)[1], "")
        return getattr(result, attr, "")

    # Fall back to the variable store (supporting dotted access into dicts).
    value: Any = context.variables.get(head, None)
    if tail and isinstance(value, dict):
        for part in tail.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break
    return value if value is not None else ""


def render(value: Any, context: "Context") -> Any:
    """Recursively resolve ``${...}`` templates inside *value*."""

    if isinstance(value, str):
        # If the whole string is a single token, preserve the resolved type
        # (e.g. an int or a dict) instead of stringifying it.
        match = _TOKEN.fullmatch(value.strip())
        if match:
            return _resolve_token(match.group(1), context)
        return _TOKEN.sub(lambda m: str(_resolve_token(m.group(1), context)), value)
    if isinstance(value, list):
        return [render(v, context) for v in value]
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    return value


class Context:
    """Per-run execution context handed to every action."""

    def __init__(self, variables, results=None, event=None, engine=None, task=None):
        # ``variables`` may be a VariableStore or a plain dict.
        self.variables = variables if isinstance(variables, VariableStore) else VariableStore(variables)
        self.results: Dict[str, Any] = results if results is not None else {}
        self.event = event
        self.engine = engine
        self.task = task

    def render(self, value: Any) -> Any:
        return render(value, self)

    def emit(self, name: str, payload: Dict[str, Any] | None = None, source: str = "action") -> None:
        """Emit an event onto the engine's bus (if attached)."""
        if self.engine is not None:
            self.engine.emit(name, payload or {}, source=source)
