"""The Agent — a runnable wrapper around an :class:`AgentConfig`.

``Agent.run`` resolves the configured CLI adapter, builds the command, and
executes it. If the CLI binary isn't on ``PATH`` (or the agent is configured
with ``mock: true`` / the engine is in mock mode), it transparently falls back
to the deterministic mock runner so a workflow still completes end-to-end.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from typing import Optional

from ..logsetup import API, PROGRESS
from ..models import ActionResult, AgentConfig
from ..variables import Context
from .adapters import MockAdapter, get_adapter

log = logging.getLogger("agentc.agent")


def _api_request(cfg, argv, extra_env, mock=False):
    tag = "REQUEST(mock)" if mock else "REQUEST"
    # Log the full command (the request); env *names* only — never key values.
    API.info("%s agent=%s cli=%s model=%s\n  argv: %s\n  env: %s",
             tag, cfg.name, cfg.cli, cfg.model or "(default)",
             argv, sorted(extra_env.keys()) if extra_env else [])


def _api_response(cfg, exit_code, stdout, stderr, elapsed):
    API.info("RESPONSE agent=%s exit=%s elapsed=%.2fs\n--- stdout ---\n%s\n"
             "--- stderr ---\n%s\n--- end ---",
             cfg.name, exit_code, elapsed, stdout or "", stderr or "")


class Agent:
    def __init__(self, config: AgentConfig, force_mock: bool = False):
        self.config = config
        self.force_mock = force_mock
        self.adapter = get_adapter(config.cli)

    @property
    def name(self) -> str:
        return self.config.name

    def _should_mock(self) -> bool:
        if self.force_mock or self.config.mock or self.config.cli == "mock":
            return True
        # Fall back to mock when the CLI binary is unavailable.
        return shutil.which(self.config.cli) is None

    def run(self, prompt: str, context: Optional[Context] = None) -> ActionResult:
        cfg = self.config
        start = time.time()

        if self._should_mock():
            PROGRESS.info("agent '%s' (%s, mock) invoked", cfg.name, cfg.cli)
            _api_request(cfg, ["mock", prompt], {}, mock=True)
            text = MockAdapter().reply(cfg, prompt)
            _api_response(cfg, 0, text, "", time.time() - start)
            PROGRESS.info("agent '%s' replied (mock, %d chars)", cfg.name, len(text))
            return ActionResult(name=cfg.name, type="agent", success=True,
                                stdout=text, outputs={"mock": True, "agent": cfg.name})

        argv, extra_env = self.adapter.build(cfg, prompt)
        env = dict(os.environ)
        env.update(extra_env)
        log.info("agent %s -> %s", cfg.name, argv[0])
        PROGRESS.info("agent '%s' calling %s (%s/%s)…",
                      cfg.name, cfg.cli, cfg.provider, cfg.model or "default")
        _api_request(cfg, argv, extra_env)
        # AI agent runs can legitimately take a long time, so there is no timeout
        # by default. A positive cfg.timeout still caps the run; None/0 = unlimited.
        run_timeout = cfg.timeout if (cfg.timeout and cfg.timeout > 0) else None
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  env=env, timeout=run_timeout)
        except FileNotFoundError:
            log.warning("agent %s: CLI %r not found; using mock", cfg.name, cfg.cli)
            text = MockAdapter().reply(cfg, prompt)
            _api_response(cfg, 0, text, "(cli not found; mock fallback)", time.time() - start)
            return ActionResult(name=cfg.name, type="agent", success=True,
                                stdout=text, outputs={"mock": True, "fallback": True})
        except subprocess.TimeoutExpired:
            log.error("agent %s timed out after %ss", cfg.name, cfg.timeout)
            _api_response(cfg, 124, "", f"timeout after {cfg.timeout}s", time.time() - start)
            PROGRESS.warning("agent '%s' timed out after %ss", cfg.name, cfg.timeout)
            return ActionResult(name=cfg.name, type="agent", success=False,
                                error=f"agent timed out after {cfg.timeout}s", exit_code=124)

        elapsed = time.time() - start
        _api_response(cfg, proc.returncode, proc.stdout, proc.stderr, elapsed)
        text = self.adapter.parse(proc.stdout)
        # A clean exit with no output means the agent did nothing useful — an
        # empty model completion or a soft API failure that the CLI still
        # reported as exit 0. Treat that as a failure so the workflow stops
        # instead of "completing" a task with an empty result (and archiving it).
        produced_nothing = proc.returncode == 0 and not (text or "").strip()
        success = proc.returncode == 0 and not produced_nothing
        if success:
            PROGRESS.info("agent '%s' replied in %.2fs (%d chars)",
                          cfg.name, elapsed, len(text or ""))
        elif produced_nothing:
            PROGRESS.warning("agent '%s' exited 0 but produced no output in %.2fs",
                             cfg.name, elapsed)
        else:
            PROGRESS.warning("agent '%s' exited %s in %.2fs",
                             cfg.name, proc.returncode, elapsed)
        if produced_nothing:
            error = "agent exited 0 but produced no output"
        elif proc.returncode == 0:
            error = None
        else:
            error = f"exit {proc.returncode}"
        return ActionResult(
            name=cfg.name,
            type="agent",
            success=success,
            exit_code=proc.returncode,
            stdout=text,
            stderr=proc.stderr,
            outputs={"agent": cfg.name},
            error=error,
        )
