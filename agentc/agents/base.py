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
        if proc.returncode == 0:
            PROGRESS.info("agent '%s' replied in %.2fs (%d chars)",
                          cfg.name, elapsed, len(proc.stdout or ""))
        else:
            PROGRESS.warning("agent '%s' exited %s in %.2fs",
                             cfg.name, proc.returncode, elapsed)
        return ActionResult(
            name=cfg.name,
            type="agent",
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=self.adapter.parse(proc.stdout),
            stderr=proc.stderr,
            outputs={"agent": cfg.name},
            error=None if proc.returncode == 0 else f"exit {proc.returncode}",
        )
