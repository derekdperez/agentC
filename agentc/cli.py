"""Command-line interface for the agentC workflow engine.

    agentc list                     show agents and tasks
    agentc validate                 validate all configs
    agentc run <task> [-v k=v ...]  run a task ad-hoc
    agentc start                    start the engine (schedules + file watchers)
    agentc emit <event> [-p k=v]    emit an event onto the bus
    agentc agent <name> <prompt>    invoke a single agent directly
    agentc runs [-n N]              show recent run history
    agentc serve [--port N]         serve the interactive dashboard (add/edit/delete)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from typing import Dict, List

from .engine import WorkflowEngine

# When launched via the ``agentc-cli`` wrapper from another directory, anchor
# the default config/state locations to the project root instead of CWD.
_ROOT = os.environ.get("AGENTC_ROOT", ".")


def _default(*parts: str) -> str:
    return os.path.join(_ROOT, *parts) if _ROOT != "." else os.path.join(*parts)


def _load_env_file() -> None:
    """Load KEY=VALUE pairs from the project-root ``.env`` into the environment.

    Existing environment variables win (we never overwrite), and the file is
    optional. This is how secrets like NVIDIA_API_KEY reach the agent CLIs.
    """
    path = _default(".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _kv(pairs: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"expected key=value, got {pair!r}")
        k, v = pair.split("=", 1)
        out[k] = v
    return out


def _engine(args) -> WorkflowEngine:
    return WorkflowEngine(
        agents_dir=args.agents_dir,
        tasks_dir=args.tasks_dir,
        state_dir=args.state_dir,
        mock_agents=args.mock,
    ).load()


def cmd_list(args):
    eng = _engine(args)
    print(f"Agents ({len(eng.agents)}):")
    for name, a in sorted(eng.agent_configs.items()):
        print(f"  • {name:16} {a.cli}/{a.provider}/{a.model or 'default'}"
              + (" [mock]" if a.mock else ""))
    print(f"\nTasks ({len(eng.tasks)}):")
    for name, t in sorted(eng.tasks.items()):
        trig = t.trigger.type
        detail = {"schedule": t.trigger.cron or f"{t.trigger.interval}s",
                  "event": t.trigger.event, "file": t.trigger.path}.get(trig, "")
        print(f"  • {name:16} {len(t.actions)} action(s)  trigger={trig} {detail or ''}"
              + ("" if t.enabled else "  [disabled]"))


def cmd_validate(args):
    eng = _engine(args)
    ok, errors = eng.validate()
    if ok:
        print("✓ all configurations valid")
        return 0
    print("✗ validation errors:")
    for e in errors:
        print(f"  - {e}")
    return 1


def cmd_run(args):
    eng = _engine(args)
    ok, errors = eng.validate()
    if not ok:
        print("Refusing to run; fix config errors:")
        for e in errors:
            print(f"  - {e}")
        return 1
    run = eng.run_task(args.task, extra_vars=_kv(args.var))
    print(f"\nRun {run.id}: {run.status}")
    for r in run.results:
        mark = "✓" if r["success"] else "✗"
        out = (r.get("stdout") or "").strip()
        preview = (out[:200] + "…") if len(out) > 200 else out
        print(f"  {mark} {r['name']} ({r['type']})  {r['duration']}s")
        if preview:
            for line in preview.splitlines():
                print(f"      {line}")
        if not r["success"] and r.get("error"):
            print(f"      error: {r['error']}")
    return 0 if run.status == "success" else 1


def _paths_for(args):
    from .dashboard import Paths
    root = os.path.abspath(_ROOT) if _ROOT != "." else os.getcwd()
    return Paths(root, agents_dir=args.agents_dir, tasks_dir=args.tasks_dir,
                 state_dir=args.state_dir)


def cmd_start(args):
    eng = _engine(args)
    ok, errors = eng.validate()
    if not ok:
        print("Refusing to start; fix config errors:")
        for e in errors:
            print(f"  - {e}")
        return 1
    if args.serve:
        from .dashboard import serve
        paths = _paths_for(args)
        threading.Thread(target=serve, args=(paths, args.host, args.port),
                         kwargs={"quiet": False}, daemon=True).start()
    eng.start()
    return 0


def cmd_serve(args):
    from .dashboard import serve
    serve(_paths_for(args), host=args.host, port=args.port)
    return 0


def cmd_emit(args):
    eng = _engine(args)
    eng.register_triggers()
    eng.emit(args.event, _kv(args.payload), source="cli")
    # Give async subscribers a moment to run.
    import time
    time.sleep(args.wait)
    print(f"emitted {args.event!r}")
    return 0


def cmd_agent(args):
    eng = _engine(args)
    agent = eng.agents.get(args.name)
    if agent is None:
        print(f"unknown agent {args.name!r}")
        return 1
    result = agent.run(args.prompt)
    print(result.stdout)
    return 0 if result.success else 1


def cmd_runs(args):
    eng = _engine(args)
    runs = eng.store.list_runs(limit=args.n)
    if not runs:
        print("no runs recorded yet")
        return 0
    for r in runs:
        print(f"  {r['id']}  {r['task']:16} {r['status']:8} "
              f"trigger={r['trigger']}  actions={len(r['results'])}")
    if args.json:
        print(json.dumps(runs, indent=2))
    return 0


def cmd_reactions(args):
    """List / pause / resume reactive (event- and file-triggered) tasks via
    ``<state-dir>/reactive_tasks.json`` — the same file the engine consults
    before firing a triggered task. Ad-hoc ``agentc exec`` is never gated."""
    path = os.path.join(args.state_dir, "reactive_tasks.json")
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("paused_all", False)
    cfg.setdefault("paused_tasks", [])

    def _save():
        os.makedirs(args.state_dir, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(cfg, fh, indent=2)

    act = args.action
    if act in ("pause-all", "resume-all"):
        cfg["paused_all"] = (act == "pause-all")
        _save()
    elif act in ("pause", "resume"):
        if not args.task:
            print(f"usage: agentc reactions {act} <task-name>")
            return 1
        paused = set(cfg["paused_tasks"])
        paused.add(args.task) if act == "pause" else paused.discard(args.task)
        cfg["paused_tasks"] = sorted(paused)
        _save()

    eng = _engine(args)
    reactive = [(n, t.trigger.type, t.trigger.event or t.trigger.path or "")
                for n, t in sorted(eng.tasks.items())
                if t.trigger.type in ("event", "file")]
    print(f"reactions  paused_all={cfg['paused_all']}")
    if not reactive:
        print("  (no event/file-triggered tasks loaded)")
    for n, typ, src in reactive:
        is_paused = cfg["paused_all"] or n in cfg["paused_tasks"]
        print(f"  [{'PAUSED' if is_paused else ' on   '}] {n:30} {typ}:{src}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentc", description="AI agent team orchestration")
    p.add_argument("--agents-dir", default=_default("configs", "agents"))
    p.add_argument("--tasks-dir", default=_default("configs", "tasks"))
    p.add_argument("--state-dir", default=_default("state"))
    p.add_argument("--logs-dir", default=_default("logs"))
    p.add_argument("--mock", action="store_true",
                   help="force all agents through the deterministic mock runner")
    p.add_argument("--log", default="INFO", help="log level (DEBUG, INFO, WARNING)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list agents and tasks").set_defaults(func=cmd_list)
    sub.add_parser("validate", help="validate configs").set_defaults(func=cmd_validate)

    sp = sub.add_parser("run", help="run a task ad-hoc")
    sp.add_argument("task")
    sp.add_argument("-v", "--var", action="append", default=[], metavar="K=V")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("start", help="start the engine daemon")
    sp.add_argument("--serve", action="store_true",
                    help="also serve the interactive dashboard")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("serve", help="serve the interactive dashboard (add/edit/delete)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("emit", help="emit an event")
    sp.add_argument("event")
    sp.add_argument("-p", "--payload", action="append", default=[], metavar="K=V")
    sp.add_argument("--wait", type=float, default=1.0, help="seconds to wait for subscribers")
    sp.set_defaults(func=cmd_emit)

    sp = sub.add_parser("agent", help="invoke one agent directly")
    sp.add_argument("name")
    sp.add_argument("prompt")
    sp.set_defaults(func=cmd_agent)

    sp = sub.add_parser("runs", help="show recent runs")
    sp.add_argument("-n", type=int, default=15)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("reactions",
                        help="list/pause/resume event- & file-triggered tasks "
                             "(so adding a target won't auto-enumerate/spider)")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["list", "pause", "resume", "pause-all", "resume-all"])
    sp.add_argument("task", nargs="?", help="task name (for pause/resume)")
    sp.set_defaults(func=cmd_reactions)
    return p


def main(argv=None) -> int:
    _load_env_file()
    args = build_parser().parse_args(argv)
    from .logsetup import configure
    configure(logs_dir=args.logs_dir, level=args.log)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
