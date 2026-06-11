"""ShellAction — run a shell executable file or an inline command."""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict

from ..models import ActionResult
from ..variables import Context
from .base import Action, resolve_path


class ShellAction(Action):
    """Spec fields:
        executable — path to a shell script to run (made executable if needed)
        command    — inline shell command (used if ``executable`` is absent)
        args       — list of arguments appended to the executable
        env        — extra environment variables
        cwd        — working directory
        timeout    — seconds (default 300)
        shell      — interpreter for ``command`` (default /bin/bash)
    """

    type = "shell"

    def execute(self, context: Context) -> ActionResult:
        spec: Dict[str, Any] = context.render(self.spec)
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
        cwd = spec.get("cwd") or None
        timeout = spec.get("timeout", 300)

        if spec.get("executable"):
            path = resolve_path(spec["executable"])
            if os.path.exists(path) and not os.access(path, os.X_OK):
                os.chmod(path, 0o755)
            argv = [path] + [str(a) for a in (spec.get("args") or [])]
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  env=env, cwd=cwd, timeout=timeout)
        else:
            command = spec.get("command")
            if not command:
                return ActionResult(self.name, self.type, success=False,
                                    error="shell action needs 'executable' or 'command'")
            shell = spec.get("shell", "/bin/bash")
            proc = subprocess.run([shell, "-c", command], capture_output=True,
                                  text=True, env=env, cwd=cwd, timeout=timeout)

        return ActionResult(
            name=self.name,
            type=self.type,
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            error=None if proc.returncode == 0 else f"exit {proc.returncode}",
        )
