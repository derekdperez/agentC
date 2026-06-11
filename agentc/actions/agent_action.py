"""AgentAction (a.k.a. ToolAction) — invoke a configured agent.

This is the bridge between a task and an AI agent: it renders a prompt, hands
it to the named agent (which shells out to its CLI tool, or to the mock runner)
and captures the agent's reply as the action's stdout.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from ..models import ActionResult
from ..variables import Context
from .base import Action


class AgentAction(Action):
    """Spec fields:
        agent       — name of the agent to invoke (required)
        prompt      — the prompt text (templated)
        input_file  — file whose contents are appended to the prompt
        output_file — write the agent's reply to this path
    """

    type = "agent"

    def execute(self, context: Context) -> ActionResult:
        spec: Dict[str, Any] = context.render(self.spec)
        agent_name = spec.get("agent")
        if not agent_name:
            return ActionResult(self.name, self.type, success=False,
                                error="agent action requires 'agent'")

        engine = context.engine
        agent = engine.agents.get(agent_name) if engine else None
        if agent is None:
            return ActionResult(self.name, self.type, success=False,
                                error=f"unknown agent '{agent_name}'")

        prompt = spec.get("prompt", "")
        if spec.get("input_file") and os.path.exists(spec["input_file"]):
            with open(spec["input_file"], "rb") as fh:
                raw = fh.read()
            # Binary files (e.g. editor swap files) carry null bytes that crash
            # subprocess exec and waste a model call. Reject them with a clear
            # message instead of letting ValueError bubble up and fail the task.
            if b"\x00" in raw:
                return ActionResult(self.name, self.type, success=False,
                                    error=f"input_file {spec['input_file']!r} "
                                          "looks binary (contains null bytes); skipping")
            text = raw.decode("utf-8", errors="replace")
            prompt = f"{prompt}\n\n{text}"

        # Final safeguard: strip any stray null bytes the prompt template may hold.
        prompt = prompt.replace("\x00", "")

        result = agent.run(prompt, context)
        result.name = self.name  # report under the action's name

        if result.success and spec.get("output_file"):
            with open(spec["output_file"], "w", encoding="utf-8") as fh:
                fh.write(result.stdout)
            result.outputs["output_file"] = spec["output_file"]

        return result


# ToolAction is an alias: a tool-style step that calls an agent tool.
class ToolAction(AgentAction):
    type = "tool"
