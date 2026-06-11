"""CLI adapters — turn an :class:`AgentConfig` + prompt into a command line.

Each supported CLI tool (claude, opencode, codex, gemini) has an adapter that
knows how to express *provider*, *model* and *system prompt* in that tool's own
flags and environment. A :class:`MockAdapter` produces deterministic output so
the whole engine is runnable without any CLI installed.

Provider → connection defaults live in :data:`PROVIDERS`; an agent config may
override ``base_url`` / ``api_key_env`` explicitly.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple

from ..models import AgentConfig

# Per-provider connection hints. ``base_url`` may be None when the CLI already
# defaults to the right endpoint for that provider.
PROVIDERS: Dict[str, Dict[str, str | None]] = {
    "anthropic":  {"api_key_env": "ANTHROPIC_API_KEY", "base_url": None},
    "openai":     {"api_key_env": "OPENAI_API_KEY",    "base_url": "https://api.openai.com/v1"},
    "nvidia":     {"api_key_env": "NVIDIA_API_KEY",    "base_url": "https://integrate.api.nvidia.com/v1"},
    "opencode":   {"api_key_env": "OPENCODE_API_KEY",  "base_url": None},
    "openrouter": {"api_key_env": "OPENROUTER_API_KEY","base_url": "https://openrouter.ai/api/v1"},
    "google":     {"api_key_env": "GEMINI_API_KEY",    "base_url": None},
}


class CLIAdapter(ABC):
    """Builds the argv + environment to invoke one CLI tool."""

    name: str = "base"

    def resolve(self, agent: AgentConfig) -> Tuple[str | None, str | None]:
        """Return (api_key, base_url) using provider defaults + overrides."""
        defaults = PROVIDERS.get(agent.provider, {})
        api_key_env = agent.api_key_env or defaults.get("api_key_env")
        api_key = os.environ.get(api_key_env) if api_key_env else None
        base_url = agent.base_url or defaults.get("base_url")
        return api_key, base_url

    @abstractmethod
    def build(self, agent: AgentConfig, prompt: str) -> Tuple[List[str], Dict[str, str]]:
        """Return (argv, extra_env)."""

    def parse(self, stdout: str) -> str:
        """Hook for adapters whose CLI emits structured output."""
        return stdout


class ClaudeAdapter(CLIAdapter):
    name = "claude"

    def build(self, agent, prompt):
        api_key, base_url = self.resolve(agent)
        argv = ["claude", "-p", prompt]
        if agent.model:
            argv += ["--model", agent.model]
        if agent.system_prompt:
            argv += ["--append-system-prompt", agent.system_prompt]
        argv += agent.extra_args
        env: Dict[str, str] = {}
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        return argv, env


class OpencodeAdapter(CLIAdapter):
    name = "opencode"

    def build(self, agent, prompt):
        api_key, base_url = self.resolve(agent)
        model = f"{agent.provider}/{agent.model}" if agent.model else agent.provider
        argv = ["opencode", "run", prompt, "-m", model]
        argv += agent.extra_args
        env: Dict[str, str] = {}
        if api_key:
            env[(agent.api_key_env or "OPENCODE_API_KEY")] = api_key
        if base_url:
            env["OPENCODE_BASE_URL"] = base_url
        return argv, env


class CodexAdapter(CLIAdapter):
    name = "codex"

    def build(self, agent, prompt):
        api_key, base_url = self.resolve(agent)
        argv = ["codex", "exec", prompt]
        if agent.model:
            argv += ["-m", agent.model]
        argv += agent.extra_args
        env: Dict[str, str] = {}
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
        return argv, env


class GeminiAdapter(CLIAdapter):
    name = "gemini"

    def build(self, agent, prompt):
        api_key, base_url = self.resolve(agent)
        argv = ["gemini", "-p", prompt]
        if agent.model:
            argv += ["-m", agent.model]
        argv += agent.extra_args
        env: Dict[str, str] = {}
        if api_key:
            env["GEMINI_API_KEY"] = api_key
        if base_url:
            env["GOOGLE_GEMINI_BASE_URL"] = base_url
        return argv, env


class MockAdapter(CLIAdapter):
    """Deterministic, dependency-free runner used for dry runs and tests."""

    name = "mock"

    def build(self, agent, prompt):  # pragma: no cover - not used for exec
        return ["true"], {}

    def reply(self, agent: AgentConfig, prompt: str) -> str:
        head = prompt.strip().splitlines()[0] if prompt.strip() else "(empty prompt)"
        return (
            f"[mock:{agent.cli}/{agent.provider}/{agent.model or 'default'}] "
            f"{agent.name} received: {head}"
        )


ADAPTERS: Dict[str, CLIAdapter] = {
    "claude": ClaudeAdapter(),
    "opencode": OpencodeAdapter(),
    "codex": CodexAdapter(),
    "gemini": GeminiAdapter(),
    "mock": MockAdapter(),
}


def get_adapter(cli: str) -> CLIAdapter:
    if cli not in ADAPTERS:
        raise ValueError(f"unknown CLI tool {cli!r}; known: {', '.join(sorted(ADAPTERS))}")
    return ADAPTERS[cli]
