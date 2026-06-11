"""Centralized file logging for agentC.

Configures four rotating log files under ``logs/``:

  * ``system.log`` — all INFO+ activity from the engine, scheduler, watcher, …
  * ``error.log``  — WARNING/ERROR only (a quick place to spot problems)
  * ``agent.log``  — human-readable *progress* messages (task lifecycle, agent
    invocations) via the :data:`PROGRESS` logger
  * ``api.log``    — raw agent API calls: the exact command/request and the full
    raw response (stdout/stderr/exit) via the :data:`API` logger

Use :data:`PROGRESS` and :data:`API` from anywhere to emit to the dedicated
files. ``configure`` is idempotent — safe to call more than once.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

# Dedicated loggers (import these where you want to write progress / API logs).
PROGRESS = logging.getLogger("agentc.progress")
API = logging.getLogger("agentc.api")

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"
_CONFIGURED = False


def _rotating(path, level, max_bytes, backups=3):
    h = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups,
                            encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(logging.Formatter(_FMT, _DATE))
    return h


def configure(logs_dir="logs", level="INFO", console=True):
    """Wire up console + file logging. Returns the resolved logs directory."""
    global _CONFIGURED
    os.makedirs(logs_dir, exist_ok=True)
    fmt = logging.Formatter(_FMT, _DATE)
    console_level = getattr(logging, str(level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Drop any handlers from a previous configure()/basicConfig().
    for h in list(root.handlers):
        root.removeHandler(h)

    if console:
        ch = logging.StreamHandler()
        ch.setLevel(console_level)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    root.addHandler(_rotating(os.path.join(logs_dir, "system.log"),
                              logging.INFO, 2_000_000))
    root.addHandler(_rotating(os.path.join(logs_dir, "error.log"),
                              logging.WARNING, 1_000_000))

    # Progress: its own file, but also propagates to root (console + system.log).
    PROGRESS.handlers = [_rotating(os.path.join(logs_dir, "agent.log"),
                                   logging.INFO, 2_000_000)]
    PROGRESS.setLevel(logging.INFO)
    PROGRESS.propagate = True

    # API: raw request/response only in api.log (kept off console + system.log).
    API.handlers = [_rotating(os.path.join(logs_dir, "api.log"),
                              logging.DEBUG, 5_000_000)]
    API.setLevel(logging.DEBUG)
    API.propagate = False

    _CONFIGURED = True
    return logs_dir


LOG_FILES = ["system.log", "agent.log", "api.log", "error.log"]
