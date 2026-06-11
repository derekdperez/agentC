"""JSON/YAML-file-backed state.

Everything durable lives under a single state directory:

    state/
      variables.json     global variable store
      events.jsonl       append-only event log
      runs/<id>.json      one file per task run

YAML is supported for *input* config files when PyYAML is available; state is
always written as JSON (stdlib-only, git-friendly).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List

from .models import Event, RunRecord


def load_structured(path: str) -> Any:
    """Load a .json / .yaml / .yml file."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                f"{path} is YAML but PyYAML is not installed; use JSON instead"
            ) from exc
        return yaml.safe_load(text)
    return json.loads(text)


class StateStore:
    def __init__(self, root: str = "state"):
        self.root = root
        self.runs_dir = os.path.join(root, "runs")
        self.vars_path = os.path.join(root, "variables.json")
        self.events_path = os.path.join(root, "events.jsonl")
        self.engine_path = os.path.join(root, "engine.json")
        self._lock = threading.Lock()
        os.makedirs(self.runs_dir, exist_ok=True)

    # -- engine status / heartbeat ---------------------------------------- #
    def save_engine_status(self, data: Dict[str, Any]) -> None:
        with self._lock:
            _atomic_write(self.engine_path, json.dumps(data, indent=2, default=str))

    # -- global variables -------------------------------------------------- #
    def load_variables(self) -> Dict[str, Any]:
        if not os.path.exists(self.vars_path):
            return {}
        with open(self.vars_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save_variables(self, variables: Dict[str, Any]) -> None:
        with self._lock:
            _atomic_write(self.vars_path, json.dumps(variables, indent=2, default=str))

    # -- event log --------------------------------------------------------- #
    def append_event(self, event: Event) -> None:
        with self._lock:
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), default=str) + "\n")

    # -- runs -------------------------------------------------------------- #
    def save_run(self, run: RunRecord) -> None:
        path = os.path.join(self.runs_dir, f"{run.id}.json")
        with self._lock:
            _atomic_write(path, json.dumps(run.to_dict(), indent=2, default=str))

    def reconcile_running(self) -> int:
        """Flip any run still marked 'running' to 'interrupted'.

        A run record is written as ``running`` the moment it starts; if the
        engine is killed mid-run (crash, SIGKILL, power loss) the final status
        is never written and the record is a zombie. The engine calls this on
        startup — at that point nothing it owns is actually running, so every
        leftover ``running`` record is stale and gets closed out.

        Returns the number of records reconciled.
        """
        n = 0
        try:
            names = os.listdir(self.runs_dir)
        except OSError:
            return 0
        for fname in names:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.runs_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if data.get("status") == "running":
                data["status"] = "interrupted"
                data["error"] = data.get("error") or "interrupted (engine restart)"
                data["finished"] = data.get("finished") or time.time()
                with self._lock:
                    _atomic_write(path, json.dumps(data, indent=2, default=str))
                n += 1
        return n

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        files = sorted(
            (os.path.join(self.runs_dir, f) for f in os.listdir(self.runs_dir)
             if f.endswith(".json")),
            key=os.path.getmtime,
            reverse=True,
        )
        runs = []
        for path in files[:limit]:
            with open(path, "r", encoding="utf-8") as fh:
                runs.append(json.load(fh))
        return runs


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
