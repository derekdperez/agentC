"""PythonAction — run a Python executable file or an inline snippet.

The action runs in a child interpreter (``sys.executable``) so a misbehaving
snippet can't take down the engine. The triggering event payload and the
current variables are exposed to inline code via the ``AGENTC_CONTEXT``
environment variable (JSON). Anything the snippet prints to stdout becomes the
action's stdout; a trailing line of the form ``::set name=value`` is parsed
into a structured output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict

from ..models import ActionResult
from ..variables import Context
from .base import Action, resolve_path


class PythonAction(Action):
    """Spec fields:
        executable — path to a .py file to run
        code       — inline Python source (used if ``executable`` is absent)
        args       — list of arguments
        env        — extra environment variables
        timeout    — seconds (default 300)
    """

    type = "python"

    def execute(self, context: Context) -> ActionResult:
        spec: Dict[str, Any] = context.render(self.spec)
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
        env["AGENTC_CONTEXT"] = json.dumps({
            "variables": context.variables.as_dict(),
            "event": context.event.to_dict() if context.event else None,
        })
        timeout = spec.get("timeout", 300)
        args = [str(a) for a in (spec.get("args") or [])]

        tmp_path = None
        try:
            if spec.get("executable"):
                target = resolve_path(spec["executable"])
            else:
                code = spec.get("code")
                if not code:
                    return ActionResult(self.name, self.type, success=False,
                                        error="python action needs 'executable' or 'code'")
                fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="agentc_")
                with os.fdopen(fd, "w") as fh:
                    fh.write(code)
                target = tmp_path

            proc = subprocess.run([sys.executable, target, *args],
                                  capture_output=True, text=True,
                                  env=env, timeout=timeout)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        outputs = _parse_directives(proc.stdout)
        return ActionResult(
            name=self.name,
            type=self.type,
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            outputs=outputs,
            error=None if proc.returncode == 0 else f"exit {proc.returncode}",
        )


def _parse_directives(stdout: str) -> Dict[str, Any]:
    """Collect ``::set key=value`` lines into a dict of structured outputs."""
    outputs: Dict[str, Any] = {}
    for line in stdout.splitlines():
        if line.startswith("::set "):
            key, _, value = line[len("::set "):].partition("=")
            outputs[key.strip()] = value.strip()
    return outputs
