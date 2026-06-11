"""Control the agentC engine as a supervised systemd **user** service.

The dashboard's Start / Stop / Restart buttons call into here. We shell out to
``systemctl --user`` so the engine runs supervised (``Restart=always``) and is
independent of the dashboard process — the two are separate services, so a
crash of one never takes down the other, and a crashed engine is brought back
automatically.

Everything degrades gracefully: if systemd or the unit isn't installed, the
control calls return a clear, actionable message instead of raising.
"""

from __future__ import annotations

import shutil
import subprocess

ENGINE_UNIT = "agentc-engine.service"
DASHBOARD_UNIT = "agentc-dashboard.service"
_ACTIONS = {"start", "stop", "restart"}


def _systemctl(*args, timeout=15):
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def available() -> bool:
    """True if ``systemctl --user`` is usable on this host."""
    if shutil.which("systemctl") is None:
        return False
    try:
        return _systemctl("--version", timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def unit_installed(unit: str = ENGINE_UNIT) -> bool:
    if not available():
        return False
    try:
        r = _systemctl("list-unit-files", "--no-legend", unit)
    except subprocess.SubprocessError:
        return False
    return unit in (r.stdout or "")


def is_active(unit: str = ENGINE_UNIT) -> bool:
    if not available():
        return False
    try:
        r = _systemctl("is-active", unit, timeout=5)
    except subprocess.SubprocessError:
        return False
    return (r.stdout or "").strip() == "active"


def restarts() -> "int | None":
    """How many times systemd has auto-restarted the engine (crash count)."""
    if not available():
        return None
    try:
        r = _systemctl("show", ENGINE_UNIT, "-p", "NRestarts", "--value", timeout=5)
    except subprocess.SubprocessError:
        return None
    v = (r.stdout or "").strip()
    return int(v) if v.isdigit() else None


def status() -> dict:
    """A small snapshot the dashboard can surface."""
    return {
        "systemd": available(),
        "installed": unit_installed(),
        "active": is_active(),
        "restarts": restarts(),
        "unit": ENGINE_UNIT,
    }


def control(action: str):
    """Run start/stop/restart on the engine unit.

    Returns ``(ok: bool, message: str)``.
    """
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        return False, f"unknown action {action!r} (expected start/stop/restart)"
    if not available():
        return False, "systemctl --user is unavailable on this host"
    if not unit_installed():
        return False, (f"{ENGINE_UNIT} is not installed — run "
                       "scripts/install_service.sh first")
    try:
        # stop/restart may wait for the engine to wind down; allow generous time
        # (still under systemd's default 90s TimeoutStopSec).
        r = _systemctl(action, ENGINE_UNIT, timeout=60)
    except subprocess.TimeoutExpired:
        return False, f"systemctl {action} timed out"
    except subprocess.SubprocessError as exc:  # noqa: BLE001
        return False, f"systemctl {action} error: {exc}"
    if r.returncode == 0:
        return True, f"engine {action} ok"
    return False, (r.stderr or r.stdout or f"systemctl {action} failed").strip()
