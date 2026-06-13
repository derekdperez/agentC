"""The workflow engine — the orchestrator.

Responsibilities:
  * load agent + task configuration
  * execute a task's actions in order, threading a shared :class:`Context`
    (variables + accumulated results) through them
  * persist run records, the event log, and the global variable store
  * wire up triggers: schedule (cron/interval), event, and Linux file events
  * run a foreground loop (``start``) that keeps schedulers/watchers alive
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from . import config as cfg
from .actions.base import resolve_path
from .actions.registry import build_action
from .agents.base import Agent
from .events import EventBus
from .logsetup import PROGRESS
from .models import AgentConfig, Event, RunRecord, Task
from .scheduler import Scheduler
from .store import StateStore
from .variables import Context, VariableStore
from .watcher import make_watcher

log = logging.getLogger("agentc.engine")


class WorkflowEngine:
    def __init__(self, agents_dir="configs/agents", tasks_dir="configs/tasks",
                 state_dir="state", mock_agents: bool = False):
        self.agents_dir = agents_dir
        self.tasks_dir = tasks_dir
        self.mock_agents = mock_agents

        self.store = StateStore(state_dir)
        self._state_dir = state_dir
        self.bus = EventBus()
        self.scheduler = Scheduler()
        self.watcher = make_watcher()

        self.agent_configs: Dict[str, AgentConfig] = {}
        self.agents: Dict[str, Agent] = {}
        self.tasks: Dict[str, Task] = {}

        # Global variables persist across runs.
        self.globals = VariableStore(self.store.load_variables())
        self._running = threading.Event()
        self._started: float = 0.0
        # (task_name, filepath) -> last trigger time; prevents double-fire from
        # editors that emit IN_CREATE then rename a temp file (IN_MOVED_TO).
        self._trigger_times: Dict[tuple, float] = {}
        self._trigger_lock = threading.Lock()
        self._DEBOUNCE = 2.0  # seconds
        self._lock_fd = None  # singleton flock fd; held for the process lifetime

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    def load(self) -> "WorkflowEngine":
        self.agent_configs = cfg.load_agents(self.agents_dir)
        self.agents = {
            name: Agent(ac, force_mock=self.mock_agents)
            for name, ac in self.agent_configs.items()
        }
        self.tasks = cfg.load_tasks(self.tasks_dir)
        log.info("loaded %d agent(s), %d task(s)", len(self.agents), len(self.tasks))
        return self

    def validate(self):
        return cfg.validate_all(self.agent_configs, self.tasks)

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def emit(self, name: str, payload: Optional[Dict[str, Any]] = None,
             source: str = "engine") -> Event:
        event = Event(name=name, payload=payload or {}, source=source)
        self.store.append_event(event)
        log.info("emit %s (%s)", name, source)
        self.bus.publish(event)
        return event

    # ------------------------------------------------------------------ #
    # Task execution
    # ------------------------------------------------------------------ #
    def run_task(self, task_name: str, event: Optional[Event] = None,
                 extra_vars: Optional[Dict[str, Any]] = None) -> RunRecord:
        task = self.tasks.get(task_name)
        if task is None:
            raise KeyError(f"unknown task {task_name!r}")
        if not task.enabled:
            log.info("task %r is disabled; skipping", task_name)
            return RunRecord(task=task_name, status="skipped")

        # Layer variables: globals < task defaults < event payload < extra.
        variables = VariableStore(self.globals.as_dict())
        variables.update(task.variables)
        if event is not None:
            variables.update({f"event_{k}": v for k, v in event.payload.items()})
        if extra_vars:
            variables.update(extra_vars)

        context = Context(variables=variables, results={}, event=event,
                          engine=self, task=task)
        run = RunRecord(task=task_name, trigger=task.trigger.type,
                        variables=variables.as_dict())
        log.info("=== run %s : task %s (trigger=%s) ===", run.id, task_name, run.trigger)
        # Only emit progress for real tasks; meta tasks (monitor/dashboard) set
        # persist=false and would otherwise flood the progress log.
        progress = task.persist
        if progress:
            trigger_note = ""
            if event is not None and event.payload.get("path"):
                trigger_note = f" ({event.name}: {event.payload['path']})"
            PROGRESS.info("task '%s' started [run %s, trigger %s]%s",
                          task_name, run.id, run.trigger, trigger_note)
        # Persist immediately (status="running") so the dashboard can see live runs.
        if task.persist:
            self.store.save_run(run)

        try:
            for spec in task.actions:
                action = build_action(spec)
                result = action.run(context)
                run.results.append(result.to_dict())
                status = "ok" if result.success else "FAIL"
                log.info("  [%s] %s (%s) %.3fs", status, action.name,
                         action.type, result.duration)
                if progress and result.success:
                    PROGRESS.info("  action '%s' (%s) ok in %.2fs",
                                  action.name, action.type, result.duration)
                elif not result.success:
                    PROGRESS.warning("  action '%s' (%s) FAILED: %s",
                                     action.name, action.type, result.error)
                if not result.success and action.on_failure != "continue":
                    run.status = "failed"
                    run.error = result.error or f"action {action.name} failed"
                    break
            else:
                run.status = "success"

            if run.status == "success":
                for ev in task.emits:
                    self.emit(context.render(ev), {"task": task_name}, source="task")
        except Exception as exc:  # noqa: BLE001
            log.exception("task %r crashed", task_name)
            run.status = "failed"
            run.error = str(exc)

        elapsed = time.time() - run.started
        if run.status == "success":
            if progress:
                PROGRESS.info("task '%s' completed in %.2fs", task_name, elapsed)
        else:
            PROGRESS.warning("task '%s' FAILED after %.2fs: %s",
                             task_name, elapsed, run.error)

        run.finished = time.time()
        run.variables = variables.as_dict()
        if task.persist:
            self.store.save_run(run)
        # Persist any variables the task promoted to the global namespace.
        self.globals.update(variables.as_dict())
        self.store.save_variables(self.globals.as_dict())
        return run

    def _run_task_async(self, task_name: str, event: Optional[Event] = None) -> None:
        threading.Thread(
            target=self.run_task, args=(task_name, event),
            name=f"task-{task_name}", daemon=True,
        ).start()

    # ------------------------------------------------------------------ #
    # Trigger registration
    # ------------------------------------------------------------------ #
    def register_triggers(self) -> None:
        for task in self.tasks.values():
            if not task.enabled:
                continue
            t = task.trigger
            name = task.name
            if t.type == "schedule":
                if t.cron:
                    self.scheduler.add_cron(t.cron, lambda n=name: self._run_task_async(n))
                    log.info("scheduled %r on cron %r", name, t.cron)
                elif t.interval:
                    self.scheduler.add_interval(t.interval, lambda n=name: self._run_task_async(n))
                    log.info("scheduled %r every %ss", name, t.interval)
            elif t.type == "event":
                self.bus.subscribe(t.event, lambda ev, n=name: self._run_triggered(n, ev))
                log.info("task %r subscribed to event %r", name, t.event)
            elif t.type == "file":
                watch_path = resolve_path(t.path)
                self.watcher.add(
                    path=watch_path, on=t.on, pattern=t.pattern, recursive=t.recursive,
                    callback=lambda fp, kind, n=name: self._on_file_event(n, fp, kind),
                    ignore=t.ignore,
                )
                log.info("task %r watches %s (on=%s pattern=%s recursive=%s)",
                         name, t.path, t.on, t.pattern, t.recursive)

    def _on_file_event(self, task_name: str, filepath: str, kind: str) -> None:
        key = (task_name, filepath)
        now = time.time()
        with self._trigger_lock:
            last = self._trigger_times.get(key, 0.0)
            if now - last < self._DEBOUNCE:
                log.debug("debounce: skipping %s for %s (%.2fs since last)", kind, filepath, now - last)
                return
            self._trigger_times[key] = now
        event = self.emit("file." + kind, {"path": filepath, "kind": kind,
                          "filename": filepath.rsplit("/", 1)[-1]}, source="watcher")
        if self._reactive_blocked(task_name):
            log.info("reactive task %r paused — not firing for %s", task_name, filepath)
            return
        self._run_task_async(task_name, event)

    def _run_triggered(self, task_name: str, event=None) -> None:
        """Run an event-triggered task unless its reaction is currently paused."""
        if self._reactive_blocked(task_name):
            log.info("reactive task %r paused — not firing", task_name)
            return
        self._run_task_async(task_name, event)

    def _reactive_blocked(self, task_name: str) -> bool:
        """True if this event/file-triggered task is paused via
        ``<state_dir>/reactive_tasks.json`` ({"paused_all": bool,
        "paused_tasks": [...]}). Re-read each call so toggles take effect
        immediately without an engine restart. Ad-hoc ``run_task`` bypasses it."""
        try:
            with open(os.path.join(self._state_dir, "reactive_tasks.json")) as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            return False
        if cfg.get("paused_all"):
            return True
        return task_name in (cfg.get("paused_tasks") or [])

    # ------------------------------------------------------------------ #
    # Daemon loop
    # ------------------------------------------------------------------ #
    def _write_status(self, status: str) -> None:
        """Write the engine heartbeat/status file the dashboard reads."""
        self.store.save_engine_status({
            "status": status,
            "pid": os.getpid(),
            "started": self._started,
            "scheduler_backend": self.scheduler.backend,
            "watcher_backend": getattr(self.watcher, "backend", "?"),
            "agents": len(self.agents),
            "tasks": len(self.tasks),
            "heartbeat": time.time(),
        })

    def _acquire_singleton_lock(self) -> bool:
        """Take an exclusive, process-lifetime lock so only one engine runs.

        Multiple concurrent engines each drive the full pipeline independently,
        multiplying outbound request rate and defeating per-domain rate limits.
        The flock is advisory and released automatically when this process exits
        (or is killed), so a crashed engine never leaves a stale lock behind.
        Returns False if another live engine already holds it.
        """
        import fcntl
        lock_path = os.path.join(self.store.root, "engine.lock")
        # "a+" so we don't truncate the holder's pid before we can read it.
        self._lock_fd = open(lock_path, "a+")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            try:
                self._lock_fd.seek(0)
                other = self._lock_fd.read().strip() or "?"
            except OSError:
                other = "?"
            log.error("another engine is already running (pid %s); refusing to start", other)
            self._lock_fd.close()
            self._lock_fd = None
            return False
        self._lock_fd.seek(0)
        self._lock_fd.truncate(0)
        self._lock_fd.write(str(os.getpid()))
        self._lock_fd.flush()
        return True

    def start(self) -> None:
        """Start schedulers + watchers and block until interrupted."""
        if not self._acquire_singleton_lock():
            raise SystemExit(
                "agentC engine is already running; only one instance is allowed. "
                "Stop the existing one first (systemctl --user stop agentc-engine)."
            )
        self._started = time.time()
        # Close out any zombie 'running' records left by a previous hard crash
        # so the dashboard's Running panel only shows what is truly active.
        reconciled = self.store.reconcile_running()
        if reconciled:
            log.info("reconciled %d interrupted run(s) from a previous crash", reconciled)
        self.register_triggers()
        self.scheduler.start()
        if self.watcher.watches:
            self.watcher.start()
        self._running.set()
        self._write_status("running")
        # Stop cleanly on SIGINT/SIGTERM so the status file flips to "stopped".
        import signal
        try:
            signal.signal(signal.SIGTERM, lambda *_: self._running.clear())
            signal.signal(signal.SIGINT, lambda *_: self._running.clear())
        except ValueError:
            pass  # not running in the main thread; KeyboardInterrupt still works
        log.info("engine running (scheduler=%s, watcher=%s) — Ctrl-C to stop",
                 self.scheduler.backend, getattr(self.watcher, "backend", "?"))
        try:
            last_heartbeat = 0.0
            while self._running.is_set():
                now = time.time()
                if now - last_heartbeat >= 2.0:
                    self._write_status("running")
                    last_heartbeat = now
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.stop()

    def stop(self) -> None:
        self._running.clear()
        self.scheduler.shutdown()
        try:
            self.watcher.stop()
        except Exception:  # noqa: BLE001
            pass
        self._write_status("stopped")
        if self._lock_fd is not None:
            try:
                self._lock_fd.close()  # releases the flock
            except OSError:
                pass
            self._lock_fd = None
        log.info("engine stopped")
