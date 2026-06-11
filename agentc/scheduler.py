"""Scheduling of cron / interval tasks.

Prefers APScheduler's ``BackgroundScheduler`` when it is installed; otherwise
falls back to a dependency-free background thread that evaluates a small 5-field
cron implementation once a minute and interval jobs by elapsed time.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

log = logging.getLogger("agentc.scheduler")

try:  # pragma: no cover - exercised only when the lib is present
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    _HAVE_APSCHEDULER = True
except ImportError:  # pragma: no cover
    _HAVE_APSCHEDULER = False


# --------------------------------------------------------------------------- #
# Minimal cron support (used only by the fallback)
# --------------------------------------------------------------------------- #
def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


class CronSpec:
    """A 5-field cron expression: minute hour day-of-month month day-of-week."""

    def __init__(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron expression must have 5 fields: {expr!r}")
        self.minute = _parse_field(fields[0], 0, 59)
        self.hour = _parse_field(fields[1], 0, 23)
        self.dom = _parse_field(fields[2], 1, 31)
        self.month = _parse_field(fields[3], 1, 12)
        self.dow = _parse_field(fields[4], 0, 6)  # 0 = Monday (matches struct_tm? see below)

    def matches(self, t: time.struct_time) -> bool:
        # time.struct_time tm_wday: Monday=0 .. Sunday=6 — keep cron 0=Monday.
        return (
            t.tm_min in self.minute
            and t.tm_hour in self.hour
            and t.tm_mday in self.dom
            and t.tm_mon in self.month
            and t.tm_wday in self.dow
        )


class _ThreadScheduler:
    """Fallback scheduler: one daemon thread, second-resolution tick."""

    def __init__(self):
        self._cron_jobs: List[tuple[CronSpec, Callable]] = []
        self._interval_jobs: List[tuple[float, Callable, list]] = []  # (every, fn, [next_ts])
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def add_cron(self, expr: str, fn: Callable) -> None:
        self._cron_jobs.append((CronSpec(expr), fn))

    def add_interval(self, seconds: float, fn: Callable) -> None:
        self._interval_jobs.append((seconds, fn, [time.time() + seconds]))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="agentc-sched", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        last_minute = -1
        while not self._stop.is_set():
            now = time.time()
            lt = time.localtime(now)
            if lt.tm_min != last_minute:
                last_minute = lt.tm_min
                for spec, fn in self._cron_jobs:
                    if spec.matches(lt):
                        _safe(fn)
            for every, fn, nxt in self._interval_jobs:
                if now >= nxt[0]:
                    nxt[0] = now + every
                    _safe(fn)
            self._stop.wait(1.0)


def _safe(fn: Callable) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001
        log.exception("scheduled job raised")


class Scheduler:
    """Uniform façade over APScheduler or the thread fallback."""

    def __init__(self):
        if _HAVE_APSCHEDULER:
            self._impl = BackgroundScheduler()
            self.backend = "apscheduler"
        else:
            self._impl = _ThreadScheduler()
            self.backend = "thread"

    def add_cron(self, expr: str, fn: Callable) -> None:
        if self.backend == "apscheduler":
            self._impl.add_job(fn, CronTrigger.from_crontab(expr))
        else:
            self._impl.add_cron(expr, fn)

    def add_interval(self, seconds: float, fn: Callable) -> None:
        if self.backend == "apscheduler":
            self._impl.add_job(fn, IntervalTrigger(seconds=seconds))
        else:
            self._impl.add_interval(seconds, fn)

    def start(self) -> None:
        self._impl.start()
        log.info("scheduler started (backend=%s)", self.backend)

    def shutdown(self) -> None:
        try:
            if self.backend == "apscheduler":
                # Don't block on in-flight jobs (e.g. a long agent run) — otherwise
                # Stop/Restart hang until the current task finishes. Let the process
                # exit promptly; systemd's cgroup SIGTERM reaps the worker's child.
                self._impl.shutdown(wait=False)
            else:
                self._impl.shutdown()
        except Exception:  # noqa: BLE001
            pass
