"""The agentC dashboard: HTML rendering + an interactive HTTP server.

Two ways to use it:

  * **Static** — ``scripts/render_dashboard.py`` calls :func:`render_page` with
    ``interactive=False`` and writes ``dashboard.html``. Refresh in a browser to
    see current state. Resize/collapse work; add/edit/delete do not (no backend).

  * **Served** — ``agentc serve`` runs :func:`serve`, an ``http.server`` that
    renders the page live and exposes a JSON API for CRUD on agents and tasks
    (writing the ``configs/`` files). Add/edit/delete work here.

Config changes made through the dashboard are written to disk immediately and
shown on the next refresh; a *running engine* applies new/changed triggers when
it is next (re)started.
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


class Paths:
    def __init__(self, root, agents_dir=None, tasks_dir=None, state_dir=None,
                 logs_dir=None):
        self.root = root
        self.agents_dir = agents_dir or os.path.join(root, "configs", "agents")
        self.tasks_dir = tasks_dir or os.path.join(root, "configs", "tasks")
        self.state_dir = state_dir or os.path.join(root, "state")
        self.logs_dir = logs_dir or os.path.join(root, "logs")
        self.out = os.path.join(root, "dashboard.html")


REFRESH_SECONDS = 5
MAX_EVENTS = 300
MAX_RUNS = 50
STALE_AFTER = 12


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def _load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def load_agents(directory):
    return [d for d in (_load_json(p) for p in
            sorted(glob.glob(os.path.join(directory, "*.json")))) if d]


def load_tasks(directory):
    return [d for d in (_load_json(p) for p in
            sorted(glob.glob(os.path.join(directory, "*.json")))) if d]


def load_runs(state_dir):
    files = glob.glob(os.path.join(state_dir, "runs", "*.json"))
    runs = [r for r in (_load_json(p) for p in files) if r]
    runs.sort(key=lambda r: r.get("started", 0), reverse=True)
    return runs


def load_events(state_dir):
    path = os.path.join(state_dir, "events.jsonl")
    events = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return events


_LOG_ORDER = ["system.log", "agent.log", "api.log", "error.log"]


def list_log_files(logs_dir):
    found = {os.path.basename(p) for p in glob.glob(os.path.join(logs_dir, "*.log"))}
    ordered = [f for f in _LOG_ORDER if f in found]
    ordered += sorted(f for f in found if f not in _LOG_ORDER)
    return ordered


def load_log_tail(logs_dir, name, n=400):
    """Return the last *n* lines of a log file (path-safe)."""
    if "/" in name or "\\" in name or ".." in name:
        return []
    path = os.path.join(logs_dir, name)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-n:]
    except OSError:
        return []


def _log_level_class(line):
    if " ERROR " in line:
        return "lvl-error"
    if " WARNING " in line or " WARN " in line:
        return "lvl-warn"
    if " DEBUG " in line:
        return "lvl-debug"
    return ""


# --------------------------------------------------------------------------- #
# CRUD on config files
# --------------------------------------------------------------------------- #
def _slug(name):
    s = re.sub(r"[^A-Za-z0-9_.-]", "-", str(name or "")).strip("-")
    return s or "unnamed"


def _find_by_name(directory, name):
    for p in glob.glob(os.path.join(directory, "*.json")):
        d = _load_json(p)
        if d and d.get("name") == name:
            return p
    return None


def get_one(directory, name):
    p = _find_by_name(directory, name)
    return _load_json(p) if p else None


def _prune(value):
    """Drop empty-string / None entries so saved config files stay tidy."""
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if v not in ("", None)}
    return value


def _write_config(directory, name, data):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, _slug(name) + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_prune(data), fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def _delete_by_name(directory, name):
    p = _find_by_name(directory, name)
    if p:
        os.remove(p)
        return True
    return False


def _validate_agent(data):
    from .models import AgentConfig
    from .config import validate_agent
    if not data.get("name"):
        return ["name is required"]
    if not data.get("cli"):
        return ["cli is required"]
    return validate_agent(AgentConfig.from_dict(data))


def _validate_task(paths, data):
    from .models import AgentConfig, Task
    from .config import validate_task
    if not data.get("name"):
        return ["name is required"]
    agents = {a["name"]: AgentConfig.from_dict(a)
              for a in load_agents(paths.agents_dir) if a.get("name")}
    try:
        task = Task.from_dict(data)
    except Exception as exc:  # noqa: BLE001
        return [f"invalid task: {exc}"]
    return validate_task(task, agents)


def create_item(paths, kind, data):
    directory = paths.agents_dir if kind == "agent" else paths.tasks_dir
    name = data.get("name")
    if not name:
        return 400, {"ok": False, "errors": ["name is required"]}
    if _find_by_name(directory, name):
        return 409, {"ok": False, "errors": [f"a {kind} named {name!r} already exists"]}
    errs = _validate_agent(data) if kind == "agent" else _validate_task(paths, data)
    if errs:
        return 400, {"ok": False, "errors": errs}
    _write_config(directory, name, data)
    return 200, {"ok": True}


def update_item(paths, kind, oldname, data):
    directory = paths.agents_dir if kind == "agent" else paths.tasks_dir
    newname = data.get("name") or oldname
    errs = _validate_agent(data) if kind == "agent" else _validate_task(paths, data)
    if errs:
        return 400, {"ok": False, "errors": errs}
    _write_config(directory, newname, data)
    if _slug(newname) != _slug(oldname):
        _delete_by_name(directory, oldname)
    return 200, {"ok": True}


def delete_item(paths, kind, name):
    directory = paths.agents_dir if kind == "agent" else paths.tasks_dir
    _delete_by_name(directory, name)
    return 200, {"ok": True}


# --------------------------------------------------------------------------- #
# Run detail, agent/output derivation
# --------------------------------------------------------------------------- #
def task_agent(task):
    """The agent a task drives, if any (first agent/tool action)."""
    for a in task.get("actions", []) or []:
        if a.get("type") in ("agent", "tool") and a.get("agent"):
            return a["agent"]
    return ""


def task_output_dir(task):
    """Where a task's outputs land — the ``completed/`` archive of a watched
    folder for file tasks, else the watch path itself."""
    t = task.get("trigger", {}) or {}
    if t.get("type") == "file" and t.get("path"):
        return os.path.join(t["path"], "completed")
    return ""


def _task_index(tasks_dir):
    return {t.get("name"): t for t in load_tasks(tasks_dir) if t.get("name")}


def get_run_detail(state_dir, logs_dir, run_id):
    """Full run record plus any log lines that mention the run id."""
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return None
    data = _load_json(os.path.join(state_dir, "runs", run_id + ".json"))
    if not data:
        return None
    logs = []
    for fname in list_log_files(logs_dir):
        for ln in load_log_tail(logs_dir, fname, 3000):
            if run_id in ln:
                logs.append(ln)
    data["log_lines"] = logs[-300:]
    return data


# --------------------------------------------------------------------------- #
# Filesystem browsing (monitored folders) — sandboxed to the project root
# --------------------------------------------------------------------------- #
def _safe_join(root, rel):
    """Resolve *rel* under *root*, refusing traversal. Returns abs path or None."""
    rel = (rel or "").strip().lstrip("/")
    full = os.path.realpath(os.path.join(root, rel))
    root_real = os.path.realpath(root)
    if full == root_real or full.startswith(root_real + os.sep):
        return full
    return None


def monitored_dirs(paths):
    """The folders watched by file-trigger tasks, as ``{path, task, recursive}``."""
    out, seen = [], set()
    for t in load_tasks(paths.tasks_dir):
        trig = t.get("trigger", {}) or {}
        p = trig.get("path")
        if trig.get("type") == "file" and p and p not in seen:
            seen.add(p)
            out.append({"path": p, "task": t.get("name", ""),
                        "recursive": bool(trig.get("recursive"))})
    return out


def list_dir(paths, rel):
    """List a directory's immediate entries (sandboxed). Returns list or None."""
    full = _safe_join(paths.root, rel)
    if not full or not os.path.isdir(full):
        return None
    entries = []
    for name in sorted(os.listdir(full), key=lambda s: (not os.path.isdir(
            os.path.join(full, s)), s.lower())):
        fp = os.path.join(full, name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        is_dir = os.path.isdir(fp)
        entries.append({
            "name": name,
            "dir": is_dir,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "rel": (rel.rstrip("/") + "/" + name) if rel else name,
        })
    return entries


def create_file(paths, rel_dir, name, content):
    """Create a new file with *content* inside *rel_dir* (sandboxed)."""
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return 400, {"ok": False, "errors": ["invalid file name"]}
    full = _safe_join(paths.root, rel_dir)
    if not full or not os.path.isdir(full):
        return 400, {"ok": False, "errors": ["target folder not found"]}
    dest = os.path.join(full, name)
    if os.path.exists(dest):
        return 409, {"ok": False, "errors": [f"{name!r} already exists"]}
    try:
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content if content is not None else "")
    except OSError as exc:
        return 500, {"ok": False, "errors": [str(exc)]}
    return 200, {"ok": True}


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
def e(value):
    return html.escape(str(value), quote=True)


def fmt_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}G"


def fmt_dur(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def fmt_ts(ts):
    if not ts:
        return "—"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (ValueError, TypeError):
        return e(ts)


def trigger_summary(task):
    t = task.get("trigger", {}) or {}
    kind = t.get("type", "manual")
    detail = {
        "schedule": t.get("cron") or (f"{t.get('interval')}s" if t.get("interval") else ""),
        "event": t.get("event", ""),
        "file": t.get("path", ""),
    }.get(kind, "")
    return f"{kind} {detail}".strip()


def badge(text, cls):
    return f'<span class="badge {cls}">{e(text)}</span>'


def status_badge(s):
    return {"success": badge("ok", "ok"),
            "failed": badge("fail", "bad"),
            "running": badge("run", "run"),
            "interrupted": badge("intr", "warn"),
            "skipped": badge("skip", "mut")}.get(s, badge(s or "?", "mut"))


def table(tid, headers, rows, row_meta=None):
    """Render a data table. *row_meta[i]* is an optional dict of ``data-*``
    attributes (and a ``_class``) applied to row *i*'s ``<tr>`` — used to make
    rows selectable / clickable."""
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    if rows:
        cells = []
        for i, row in enumerate(rows):
            attrs = ""
            meta = (row_meta or [None] * len(rows))[i] or {}
            cls = meta.pop("_class", "")
            if cls:
                attrs += f' class="{cls}"'
            for k, val in meta.items():
                attrs += f' data-{k}="{e(val)}"'
            attrs += " tabindex=\"0\""
            cells.append(f"<tr{attrs}>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
        body = "".join(cells)
    else:
        body = f'<tr class="empty"><td colspan="{len(headers)}">— none —</td></tr>'
    return (f'<table id="{tid}" class="dt"><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table>')


def panel(key, title, count, table_html, head_buttons="", filter_for=None):
    """A panel. *head_buttons* is HTML inserted left of the search box;
    *filter_for* names the table id the search box filters (defaults tbl-<key>);
    pass ``filter_for=False`` to omit the search box entirely."""
    if filter_for is False:
        search = ""
    else:
        flt = filter_for or f"tbl-{key}"
        search = (f'<input class="filter" data-t="{flt}" placeholder="search…" '
                  f'spellcheck="false" autocomplete="off">')
    return (
        f'<div class="panel" id="panel-{key}" style="grid-area:{key}">'
        f'<div class="phead"><span class="ptitle">{e(title)}</span>'
        f'<span class="count" id="count-{key}">{count}</span>'
        f'<span class="right">{head_buttons}{search}</span></div>'
        f'<div class="pbody" id="scroll-{key}">{table_html}</div>'
        f'</div>'
    )


def _crud_buttons(kind):
    """Add / Edit / Delete buttons for a panel; Edit & Delete act on the row
    selected in that panel (handled client-side)."""
    return (
        f'<button class="add" data-act="add" data-type="{kind}">+ add</button>'
        f'<button class="mini" data-act="edit-sel" data-type="{kind}">edit</button>'
        f'<button class="mini bad" data-act="del-sel" data-type="{kind}">del</button>'
    )


def render_logs_panel(paths):
    files = list_log_files(paths.logs_dir)
    if not files:
        body = '<div class="logview"><div class="logline lvl-debug">no log files yet — start the engine (agentc start)</div></div>'
        selector = ""
    else:
        options = "".join(f'<option value="{e(f)}">{e(f)}</option>' for f in files)
        selector = f'<select id="logsel" class="logsel">{options}</select>'
        blocks = ""
        for i, fname in enumerate(files):
            lines = load_log_tail(paths.logs_dir, fname, 400)
            inner = "".join(
                f'<div class="logline {_log_level_class(ln)}">{e(ln)}</div>'
                for ln in lines) or '<div class="logline lvl-debug">(empty)</div>'
            hidden = "" if i == 0 else ' style="display:none"'
            blocks += f'<div class="logview" data-log="{e(fname)}"{hidden}>{inner}</div>'
        body = blocks
    return (
        '<div class="panel" id="panel-logs" style="grid-area:logs">'
        '<div class="phead"><span class="ptitle">Logs</span>'
        f'<span class="count">{len(files)} file(s)</span>'
        f'<span class="right">{selector}'
        '<input class="filter logfilter" placeholder="grep…" spellcheck="false" '
        'autocomplete="off"></span></div>'
        f'<div class="pbody" id="scroll-logs">{body}</div>'
        '</div>'
    )


def render_folder_browser(paths):
    """The right half of the File events panel: each monitored folder, expandable
    to show its contents. Folders are selectable — the selected one is the target
    for the 'new file' action."""
    mons = monitored_dirs(paths)
    if not mons:
        return ('<div class="fbrowse"><div class="fbempty">'
                'no monitored folders (add a file-trigger task)</div></div>')
    blocks = []
    for m in mons:
        rel = m["path"]
        entries = list_dir(paths, rel) or []
        items = []
        for ent in entries:
            icon = "&#128193;" if ent["dir"] else "&#128196;"
            meta = "dir" if ent["dir"] else fmt_size(ent["size"])
            klass = "fitem dir" if ent["dir"] else "fitem"
            items.append(
                f'<div class="{klass}" data-rel="{e(ent["rel"])}"'
                f'{" data-isdir=1" if ent["dir"] else ""}>'
                f'<span class="fname">{icon} {e(ent["name"])}</span>'
                f'<span class="fmeta">{e(meta)}</span></div>')
        inner = "".join(items) or '<div class="fitem dim">(empty)</div>'
        blocks.append(
            f'<div class="folder" data-path="{e(rel)}">'
            f'<div class="fhead" tabindex="0">'
            f'<span class="fcaret">&#9656;</span>'
            f'<span class="fpath">{e(rel)}</span>'
            f'<span class="ftask">{e(m["task"])}</span>'
            f'<span class="fcount">{len(entries)}</span></div>'
            f'<div class="fbody" style="display:none">{inner}</div>'
            f'</div>')
    return '<div class="fbrowse">' + "".join(blocks) + '</div>'


# --------------------------------------------------------------------------- #
# Build the page
# --------------------------------------------------------------------------- #
def render_page(paths, interactive=True):
    now = time.time()
    engine = _load_json(os.path.join(paths.state_dir, "engine.json"), {}) or {}
    agents = load_agents(paths.agents_dir)
    tasks = load_tasks(paths.tasks_dir)
    runs = load_runs(paths.state_dir)
    events = load_events(paths.state_dir)

    hb = engine.get("heartbeat", 0)
    alive = engine.get("status") == "running" and (now - float(hb or 0)) <= STALE_AFTER
    engine_badge = badge("RUNNING", "ok") if alive else badge("STOPPED", "bad")

    task_idx = _task_index(paths.tasks_dir)

    # --- Running: only truly-active runs, rich columns, click → detail ----- #
    running = [r for r in runs if r.get("status") == "running"]
    run_rows, run_meta = [], []
    for r in running:
        tname = r.get("task", "")
        tk = task_idx.get(tname, {})
        outdir = task_output_dir(tk)
        outcell = (f'<a class="olink" data-dir="{e(outdir)}" '
                   f'title="view output folder">&#128193; open</a>') if outdir else "—"
        run_rows.append([
            fmt_ts(r.get("started")), e(tname), e(r.get("trigger", "")),
            e(task_agent(tk)) or "—",
            f'<span class="runtime" data-since="{e(r.get("started", 0))}">…</span>',
            status_badge(r.get("status")), outcell,
        ])
        run_meta.append({"run": r.get("id", ""), "kind": "run",
                         "_class": "selectable rrow"})
    running_tbl = table(
        "tbl-running",
        ["Started", "Task", "Trigger", "Agent", "Runtime", "Status", "Output"],
        run_rows, run_meta)

    # --- Tasks: selectable rows; edit/delete from the panel header --------- #
    tasks_rows, tasks_meta = [], []
    for t in tasks:
        tasks_rows.append([
            e(t.get("name", "")), e(trigger_summary(t)),
            e(len(t.get("actions", []))),
            badge("on", "ok") if t.get("enabled", True) else badge("off", "mut"),
        ])
        tasks_meta.append({"name": t.get("name", ""), "kind": "task",
                           "_class": "selectable"})
    tasks_tbl = table("tbl-tasks", ["Task", "Trigger", "Act", "State"],
                      tasks_rows, tasks_meta)

    # --- Recent runs: terminal runs, click → detail ----------------------- #
    rr_rows, rr_meta = [], []
    for r in runs[:MAX_RUNS]:
        dur = ""
        if r.get("finished") and r.get("started"):
            dur = f"{float(r['finished']) - float(r['started']):.2f}"
        rr_rows.append([
            fmt_ts(r.get("started")), e(r.get("task", "")),
            status_badge(r.get("status")), e(r.get("trigger", "")),
            e(len(r.get("results", []))), e(dur),
        ])
        rr_meta.append({"run": r.get("id", ""), "kind": "run",
                        "_class": "selectable rrow"})
    runs_tbl = table("tbl-runs", ["Started", "Task", "St", "Trigger", "Act", "Sec"],
                     rr_rows, rr_meta)

    # --- Agents: selectable rows; edit/delete from the panel header -------- #
    agents_rows, agents_meta = [], []
    for a in agents:
        agents_rows.append([
            e(a.get("name", "")), e(a.get("cli", "")), e(a.get("provider", "")),
            e(a.get("model", "")),
            badge("mock", "mut") if a.get("mock") else badge("live", "ok"),
            e(a.get("description", "")),
        ])
        agents_meta.append({"name": a.get("name", ""), "kind": "agent",
                            "_class": "selectable"})
    agents_tbl = table("tbl-agents",
                       ["Name", "CLI", "Provider", "Model", "Mode", "Description"],
                       agents_rows, agents_meta)

    # --- File events: just the events table ------------------------------- #
    file_events = [ev for ev in events if str(ev.get("name", "")).startswith("file.")]
    file_events = file_events[-MAX_EVENTS:][::-1]
    files_tbl = table("tbl-files", ["Time", "Event", "Kind", "Path", "Src"], [[
        fmt_ts(ev.get("ts")), e(ev.get("name", "")),
        e((ev.get("payload") or {}).get("kind", "")),
        e((ev.get("payload") or {}).get("path", "")),
        e(ev.get("source", "")),
    ] for ev in file_events])

    # --- Monitored folders: their own panel (split out of File events) ----- #
    folders_browser = render_folder_browser(paths)
    n_folders = len(monitored_dirs(paths))

    other = [ev for ev in events if not str(ev.get("name", "")).startswith("file.")]
    other = other[-120:][::-1]
    activity_tbl = table("tbl-activity", ["Time", "Event", "Source", "Payload"], [[
        fmt_ts(ev.get("ts")), e(ev.get("name", "")), e(ev.get("source", "")),
        e(json.dumps(ev.get("payload", {}))[:200]),
    ] for ev in other])

    tasks_btns = _crud_buttons("task") if interactive else ""
    agents_btns = _crud_buttons("agent") if interactive else ""
    files_btns = ('<button class="add" data-act="newfile">+ new file</button>'
                  if interactive else "")

    panels = (
        panel("running", "Running", len(running), running_tbl)
        + panel("tasks", "Tasks", len(tasks), tasks_tbl, head_buttons=tasks_btns)
        + panel("agents", "Agents", len(agents), agents_tbl, head_buttons=agents_btns)
        + panel("files", "File events", len(file_events), files_tbl,
                filter_for="tbl-files")
        + panel("folders", "Monitored folders", n_folders, folders_browser,
                head_buttons=files_btns, filter_for=False)
        + panel("runs", "Recent runs", min(len(runs), MAX_RUNS), runs_tbl)
        + panel("activity", "Activity", len(other), activity_tbl)
        + render_logs_panel(paths)
    )

    started = engine.get("started")
    uptime = fmt_dur(now - float(started)) if (alive and started) else ""
    nrestarts = None
    if interactive:
        try:
            from . import service
            nrestarts = service.restarts()
        except Exception:  # noqa: BLE001
            nrestarts = None
    up_html = (f'<span class="hi">up {uptime}</span>' if uptime
               else '<span class="hi">down</span>')
    rs_html = (f'<span class="hi" title="systemd auto-restarts">&#10227;{nrestarts}</span>'
               if nrestarts is not None else "")
    header_stats = (
        f'<span class="hi">pid {e(engine.get("pid", "—"))}</span>'
        f'{up_html}{rs_html}'
        f'<span class="hi">sched:{e(engine.get("scheduler_backend", "—"))}</span>'
        f'<span class="hi">watch:{e(engine.get("watcher_backend", "—"))}</span>'
        f'<span class="hi">hb {fmt_ts(hb)}</span>'
        f'<span class="hi">{len(agents)}a · {len(tasks)}t · '
        f'{len(running)} run · {len(file_events)} fe</span>'
    )
    mode = '<span class="hi mode">interactive</span>' if interactive \
        else '<span class="hi mode">read-only</span>'

    # Engine on/off controls only work on the served dashboard (they hit the API).
    if interactive:
        engine_ctl = (
            '<span class="engctl" id="engctl">'
            '<button id="eng-start" class="eng eon" title="start the engine service">'
            '&#9654; start</button>'
            '<button id="eng-stop" class="eng eoff" title="stop the engine service">'
            '&#9632; stop</button>'
            '<button id="eng-restart" class="eng" title="restart the engine service">'
            '&#10227;</button></span>'
        )
    else:
        engine_ctl = ""

    page = PAGE
    for k, v in {
        "__REFRESH__": str(REFRESH_SECONDS),
        "__GENEPOCH__": str(int(now)),
        "__STALE__": str(STALE_AFTER),
        "__INTERACTIVE__": "true" if interactive else "false",
        "__ENGINEALIVE__": "true" if alive else "false",
        "__ENGINECTL__": engine_ctl,
        "__BADGE__": engine_badge,
        "__STATS__": header_stats,
        "__MODE__": mode,
        "__PANELS__": panels,
    }.items():
        page = page.replace(k, v)
    return page


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
def make_handler(paths):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default request logging
            pass

        def _send(self, code, ctype, body):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, "application/json", json.dumps(obj))

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return None

        def _kind(self, plural):
            return {"agents": "agent", "tasks": "task"}.get(plural)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8",
                           render_page(paths, interactive=True))
                return
            parts = [p for p in path.split("/") if p]
            if parts[:2] == ["api", "engine"]:
                from . import service
                self._json(200, service.status())
                return
            if parts[:2] == ["api", "run"] and len(parts) >= 3:
                detail = get_run_detail(paths.state_dir, paths.logs_dir,
                                        unquote(parts[2]))
                self._json(200 if detail else 404,
                           detail or {"errors": ["run not found"]})
                return
            if parts[:2] == ["api", "fs"]:
                from urllib.parse import parse_qs
                if parts[2:3] == ["monitored"]:
                    self._json(200, {"items": monitored_dirs(paths)})
                    return
                rel = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                entries = list_dir(paths, rel)
                self._json(200 if entries is not None else 404,
                           {"path": rel, "entries": entries or [],
                            "ok": entries is not None})
                return
            if parts[:1] == ["api"] and parts[1:2] == ["logs"]:
                from urllib.parse import parse_qs
                if len(parts) == 2:
                    self._json(200, {"items": list_log_files(paths.logs_dir)})
                else:
                    tail = parse_qs(urlparse(self.path).query).get("tail", ["400"])[0]
                    try:
                        n = int(tail)
                    except ValueError:
                        n = 400
                    lines = load_log_tail(paths.logs_dir, unquote(parts[2]), n)
                    self._send(200, "text/plain; charset=utf-8", "\n".join(lines))
                return
            if parts[:1] == ["api"] and len(parts) >= 2:
                plural = parts[1]
                directory = paths.agents_dir if plural == "agents" else \
                    paths.tasks_dir if plural == "tasks" else None
                if directory is None:
                    self._json(404, {"errors": ["unknown collection"]})
                    return
                if len(parts) == 2:
                    items = load_agents(directory) if plural == "agents" else load_tasks(directory)
                    self._json(200, {"items": items})
                else:
                    item = get_one(directory, unquote(parts[2]))
                    self._json(200 if item else 404, item or {"errors": ["not found"]})
                return
            self._json(404, {"errors": ["not found"]})

        def _mutate(self, method):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts[:1] != ["api"] or len(parts) < 2:
                self._json(404, {"errors": ["not found"]})
                return
            kind = self._kind(parts[1])
            if kind is None:
                self._json(404, {"errors": ["unknown collection"]})
                return
            data = self._body()
            if data is None and method != "DELETE":
                self._json(400, {"ok": False, "errors": ["invalid JSON body"]})
                return
            if method == "POST":
                code, res = create_item(paths, kind, data)
            elif method == "PUT":
                code, res = update_item(paths, kind, unquote(parts[2]), data)
            else:  # DELETE
                code, res = delete_item(paths, kind, unquote(parts[2]))
            self._json(code, res)

        def _engine_ctl(self):
            from . import service
            data = self._body() or {}
            ok, msg = service.control(data.get("action", ""))
            out = {"ok": ok, "message": msg}
            out.update(service.status())
            self._json(200 if ok else 400, out)

        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts[:2] == ["api", "engine"]:
                self._engine_ctl()
                return
            if parts[:2] == ["api", "fs"]:
                data = self._body()
                if data is None:
                    self._json(400, {"ok": False, "errors": ["invalid JSON body"]})
                    return
                code, res = create_file(paths, data.get("dir", ""),
                                        data.get("name", ""), data.get("content", ""))
                self._json(code, res)
                return
            self._mutate("POST")

        def do_PUT(self):
            self._mutate("PUT")

        def do_DELETE(self):
            self._mutate("DELETE")

    return Handler


def serve(paths, host="127.0.0.1", port=8765, quiet=False):
    httpd = ThreadingHTTPServer((host, port), make_handler(paths))
    if not quiet:
        print(f"agentC dashboard serving at http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if not quiet:
            print("\ndashboard stopped")
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# Page template (tokens replaced via str.replace so CSS/JS braces stay literal)
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>agentC dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; overflow: hidden; }
  body {
    font: 11px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: #010409; color: #c9d1d9;
    display: grid; grid-template-rows: 26px 1fr;
  }
  header {
    display: flex; align-items: center; gap: 8px; padding: 0 8px;
    background: #161b22; border-bottom: 1px solid #30363d; overflow: hidden;
    white-space: nowrap;
  }
  header .brand { font-weight: 700; color: #f0f6fc; letter-spacing: .02em; }
  header .hi { color: #8b949e; }
  header .hi.mode { color: #6e7681; border: 1px solid #30363d; border-radius: 8px; padding: 0 6px; }
  header .spacer { flex: 1 1 auto; }
  header label.pause { color: #8b949e; cursor: pointer; user-select: none; }
  header button { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 3px; cursor: pointer; font: inherit; padding: 0 6px; height: 18px; }
  header button:hover { background: #30363d; }
  header button:disabled { opacity: .4; cursor: default; }
  header .engctl { display: inline-flex; align-items: center; gap: 4px; }
  header button.eng.eon  { border-color: #238636; color: #56d364; }
  header button.eng.eon:hover  { background: #133318; }
  header button.eng.eoff { border-color: #6e2622; color: #f85149; }
  header button.eng.eoff:hover { background: #2d1413; }
  /* flex-wrap layout: panels are individually resizable + collapsible */
  .grid { display: flex; flex-wrap: wrap; align-content: flex-start; gap: 5px;
    padding: 5px; height: calc(100vh - 26px); overflow: auto; }
  .panel {
    flex: 0 0 auto; width: calc(33.333% - 5px); height: calc((100vh - 38px)/2 - 3px);
    min-width: 220px; min-height: 24px; display: flex; flex-direction: column;
    background: #0d1117; border: 1px solid #30363d; border-radius: 4px;
    overflow: hidden; resize: both;
  }
  .panel.collapsed { height: 24px !important; min-height: 0; resize: none; }
  .panel.collapsed .pbody { display: none; }
  /* "fit to content" packed view: panels flow top-to-bottom into columns (CSS multi-column)
     and wrap to the next column, so short panels stack where one tall panel would sit.
     Heights are set per-panel (inline); width follows the column, so horizontal sizing is left alone. */
  .grid.packed { display: block; column-gap: 5px; column-fill: auto; }
  .grid.packed .panel { width: auto !important; margin: 0 0 5px; resize: vertical;
    break-inside: avoid; -webkit-column-break-inside: avoid; }
  .phead { display: flex; align-items: center; gap: 6px; height: 22px; flex: 0 0 22px;
    padding: 0 6px; background: #161b22; border-bottom: 1px solid #30363d;
    cursor: pointer; user-select: none; }
  .phead .ptitle { font-weight: 700; color: #adbac7; text-transform: uppercase;
    letter-spacing: .05em; font-size: 10px; }
  .phead .count { color: #6e7681; font-size: 10px; }
  .phead .right { margin-left: auto; display: flex; align-items: center; gap: 5px; }
  .phead .filter { flex: 0 0 110px; width: 110px; height: 16px;
    background: #010409; border: 1px solid #30363d; color: #c9d1d9;
    border-radius: 3px; font: 10px ui-monospace, monospace; padding: 0 5px; }
  .phead .filter:focus { outline: none; border-color: #1f6feb; }
  .pbody { flex: 1 1 auto; min-height: 0; overflow: auto; }
  table.dt { width: 100%; border-collapse: collapse; }
  table.dt th, table.dt td { text-align: left; padding: 1px 6px;
    border-bottom: 1px solid #161b22; white-space: nowrap; }
  table.dt thead th { position: sticky; top: 0; z-index: 1; background: #161b22;
    color: #8b949e; cursor: pointer; user-select: none; font-weight: 600; }
  table.dt thead th:hover { color: #e6edf3; }
  table.dt thead th.asc::after  { content: " \25B2"; color: #58a6ff; }
  table.dt thead th.desc::after { content: " \25BC"; color: #58a6ff; }
  table.dt tbody tr:nth-child(odd) td { background: rgba(255,255,255,.015); }
  table.dt tbody tr:hover td { background: #161b22; }
  table.dt tr.empty td { color: #6e7681; font-style: italic; text-align: center; }
  .badge { display: inline-block; padding: 0 6px; border-radius: 8px; font-size: 10px; font-weight: 700; }
  .ok { background: #18331f; color: #56d364; } .bad { background: #3d1d1d; color: #f85149; }
  .run { background: #16243d; color: #58a6ff; } .mut { background: #21262d; color: #8b949e; }
  button.add, button.mini { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 3px; cursor: pointer; font: 10px ui-monospace, monospace; padding: 0 5px;
    height: 16px; line-height: 14px; }
  button.add:hover, button.mini:hover { background: #30363d; }
  button.mini.bad { color: #f85149; }
  td .act { display: inline-flex; gap: 4px; }
  /* log view */
  select.logsel { height: 16px; max-width: 110px; background: #010409; color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 3px; font: 10px ui-monospace, monospace; }
  .logview { font: 11px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .logline { padding: 0 8px; white-space: pre; }
  .logline:hover { background: #161b22; }
  .logline.lvl-error { color: #f85149; }
  .logline.lvl-warn  { color: #e3b341; }
  .logline.lvl-debug { color: #6e7681; }
  .badge.warn { background: #3a2d10; color: #e3b341; }
  /* selectable / clickable rows */
  table.dt tbody tr.selectable { cursor: pointer; }
  table.dt tbody tr.rrow { cursor: pointer; }
  table.dt tbody tr.selected td { background: #15314a !important;
    box-shadow: inset 2px 0 0 #1f6feb; }
  table.dt tbody tr:focus { outline: 1px solid #1f6feb; outline-offset: -1px; }
  a.olink { color: #58a6ff; text-decoration: none; cursor: pointer; }
  a.olink:hover { text-decoration: underline; }
  .runtime { color: #58a6ff; }
  /* monitored-folders browser */
  .fbrowse { font-size: 11px; }
  .fbempty { color: #6e7681; padding: 8px; font-style: italic; }
  .folder { border-bottom: 1px solid #161b22; }
  .fhead { display: flex; align-items: center; gap: 6px; padding: 2px 6px;
    cursor: pointer; user-select: none; background: #0d1117; }
  .fhead:hover { background: #161b22; }
  .folder.selected > .fhead { background: #15314a; box-shadow: inset 2px 0 0 #1f6feb; }
  .fcaret { width: 9px; color: #6e7681; transition: transform .1s; display: inline-block; }
  .folder.open > .fhead .fcaret { transform: rotate(90deg); }
  .fpath { color: #adbac7; font-weight: 600; flex: 1; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .ftask { color: #6e7681; font-size: 10px; }
  .fcount { color: #58a6ff; font-size: 10px; min-width: 16px; text-align: right; }
  .fbody { padding: 1px 0 3px 16px; }
  .fitem { display: flex; align-items: center; gap: 6px; padding: 0 6px;
    white-space: nowrap; }
  .fitem .fname { flex: 1; overflow: hidden; text-overflow: ellipsis; }
  .fitem .fmeta { color: #6e7681; font-size: 10px; }
  .fitem.dim { color: #6e7681; font-style: italic; }
  .fitem.dir .fname { color: #58a6ff; }
  /* run-detail modal */
  .modal.wide { width: 760px; }
  .rd-meta { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-bottom: 10px; }
  .rd-meta span { color: #8b949e; } .rd-meta b { color: #c9d1d9; font-weight: 600; }
  .rd-sec { margin: 10px 0 4px; color: #adbac7; text-transform: uppercase;
    letter-spacing: .05em; font-size: 10px; border-bottom: 1px solid #30363d; padding-bottom: 2px; }
  .rd-action { border: 1px solid #21262d; border-radius: 4px; margin-bottom: 6px; }
  .rd-action .rh { display: flex; gap: 8px; align-items: center; padding: 3px 8px;
    background: #161b22; }
  .rd-action .rh .rn { font-weight: 600; color: #c9d1d9; }
  .rd-out { background: #010409; padding: 4px 8px; white-space: pre-wrap;
    word-break: break-word; max-height: 220px; overflow: auto; font-size: 11px; }
  .rd-out.err { color: #f85149; }
  .rd-logline { padding: 0 8px; white-space: pre-wrap; font-size: 11px; }
  .rd-empty { color: #6e7681; font-style: italic; }
  /* modal + confirm + toast */
  .overlay { display: none; position: fixed; inset: 0; background: rgba(1,4,9,.72);
    z-index: 50; align-items: center; justify-content: center; }
  .modal { background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
    width: 580px; max-width: 94vw; max-height: 90vh; display: flex; flex-direction: column; }
  .modal h3 { margin: 0; padding: 9px 14px; border-bottom: 1px solid #30363d;
    font-size: 13px; color: #f0f6fc; }
  .mbody { padding: 10px 14px; overflow: auto; }
  .frow { display: flex; flex-direction: column; gap: 2px; margin-bottom: 8px; }
  .frow.inline { flex-direction: row; align-items: center; gap: 8px; }
  .frow label { color: #8b949e; font-size: 10px; text-transform: uppercase; letter-spacing: .04em; }
  .frow input[type=text], .frow input[type=number], .frow select, .frow textarea {
    background: #010409; border: 1px solid #30363d; color: #c9d1d9; border-radius: 3px;
    font: 12px ui-monospace, monospace; padding: 3px 6px; width: 100%; }
  .frow textarea { min-height: 44px; resize: vertical; }
  .frow textarea.mono { min-height: 88px; }
  .frow input[type=checkbox] { width: 14px; height: 14px; }
  .mfoot { padding: 10px 14px; border-top: 1px solid #30363d; display: flex;
    justify-content: flex-end; gap: 8px; }
  .mfoot button { padding: 4px 13px; border-radius: 4px; border: 1px solid #30363d;
    cursor: pointer; font: 12px ui-monospace, monospace; }
  .btn-save { background: #1f6feb; color: #fff; border-color: #1f6feb; }
  .btn-cancel { background: #21262d; color: #c9d1d9; }
  .btn-danger { background: #da3633; color: #fff; border-color: #da3633; }
  .errs { color: #f85149; font-size: 11px; margin-bottom: 8px; white-space: pre-wrap; display: none; }
  .prow { display: flex; align-items: center; gap: 8px; padding: 3px 0; border-bottom: 1px solid #161b22; }
  .prow .pname { flex: 1; text-transform: uppercase; letter-spacing: .04em; color: #adbac7; font-size: 11px; }
  .prow.hidden .pname { color: #6e7681; text-decoration: line-through; }
  .prow .pmove button { height: 17px; width: 22px; padding: 0; }
  .toast { position: fixed; bottom: 14px; left: 50%; transform: translateX(-50%);
    background: #161b22; border: 1px solid #30363d; color: #c9d1d9; padding: 6px 12px;
    border-radius: 5px; font-size: 11px; z-index: 60; opacity: 0; transition: opacity .2s;
    pointer-events: none; max-width: 80vw; }
  .toast.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <span class="brand">agentC</span>
  __BADGE__ __MODE__
  __STATS__
  <span class="spacer"></span>
  __ENGINECTL__
  <label class="pause"><input type="checkbox" id="pause"> pause</label>
  <span class="hi">upd <span id="ago">0s</span></span>
  <button id="panelsbtn" title="show / hide / reorder panels">&#9776; panels</button>
  <button id="reload" title="reload now">&#x21bb;</button>
</header>
<div class="grid">__PANELS__</div>

<div class="overlay" id="overlay">
  <div class="modal">
    <h3 id="mtitle">Add</h3>
    <div class="mbody"><div class="errs" id="merrs"></div><form id="mform"></form></div>
    <div class="mfoot">
      <button class="btn-cancel" id="mcancel">Cancel</button>
      <button class="btn-save" id="msave">Save</button>
    </div>
  </div>
</div>
<div class="overlay" id="coverlay">
  <div class="modal" style="width:400px">
    <h3>Confirm</h3>
    <div class="mbody" id="cmsg"></div>
    <div class="mfoot">
      <button class="btn-cancel" id="cno">Cancel</button>
      <button class="btn-danger" id="cyes">Delete</button>
    </div>
  </div>
</div>
<div class="overlay" id="poverlay">
  <div class="modal" style="width:340px">
    <h3>Panels &mdash; show / hide / reorder</h3>
    <div class="mbody" id="plist"></div>
    <div class="mfoot">
      <button class="btn-cancel" id="preset" title="restore default order, visibility and sizes">Reset to default</button>
      <button class="btn-cancel" id="pfit" title="resize &amp; reflow panels to fit their content">Fit to content</button>
      <span style="flex:1"></span>
      <button class="btn-cancel" id="pclose">Close</button>
    </div>
  </div>
</div>
<div class="overlay" id="roverlay">
  <div class="modal wide">
    <h3 id="rdtitle">Run detail</h3>
    <div class="mbody" id="rdbody">loading…</div>
    <div class="mfoot">
      <button class="btn-cancel" id="rdclose">Close</button>
    </div>
  </div>
</div>
<div class="overlay" id="noverlay">
  <div class="modal" style="width:560px">
    <h3>New file</h3>
    <div class="mbody">
      <div class="errs" id="nerrs"></div>
      <div class="frow"><label>target folder</label>
        <input type="text" id="ndir" readonly></div>
      <div class="frow"><label for="nname">file name *</label>
        <input type="text" id="nname" placeholder="task.txt" spellcheck="false"></div>
      <div class="frow"><label for="ncontent">contents</label>
        <textarea id="ncontent" class="mono" placeholder="describe the task…"></textarea></div>
    </div>
    <div class="mfoot">
      <button class="btn-cancel" id="ncancel">Cancel</button>
      <button class="btn-save" id="nsave">Save</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
var REFRESH=__REFRESH__, GEN=__GENEPOCH__, STALE=__STALE__, INTERACTIVE=__INTERACTIVE__, ENGINE_ALIVE=__ENGINEALIVE__;
var modalOpen=false, confirmOpen=false, CTYPE='', CMODE='', CNAME='';

function S(k,v){ try{ localStorage.setItem('agentc:'+k, JSON.stringify(v)); }catch(e){} }
function L(k){ try{ var v=localStorage.getItem('agentc:'+k); return v?JSON.parse(v):null; }catch(e){ return null; } }
function num(s){ var m=String(s).replace(/[, ]/g,'').match(/^-?\d+(?:\.\d+)?/); return m?parseFloat(m[0]):null; }
function throttle(fn,ms){ var t=0; return function(){ var n=Date.now(); if(n-t>ms){ t=n; fn(); } }; }
function esc(s){ return String(s).replace(/[&<>"]/g,function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

/* ---- sort / filter / scroll ---- */
function sortTable(tbl, idx, dir){
  var tb=tbl.tBodies[0]; if(!tb) return;
  var rows=[].slice.call(tb.rows).filter(function(r){ return !r.classList.contains('empty'); });
  rows.sort(function(a,b){
    var x=a.cells[idx]?a.cells[idx].textContent.trim():'', y=b.cells[idx]?b.cells[idx].textContent.trim():'';
    var nx=num(x), ny=num(y), r;
    if(nx!==null && ny!==null) r=nx-ny; else r=x.localeCompare(y);
    return dir==='desc'? -r : r;
  });
  rows.forEach(function(r){ tb.appendChild(r); });
}
function setSort(tbl, idx, dir, noSave){
  sortTable(tbl, idx, dir);
  var ths=tbl.tHead.rows[0].cells;
  for(var i=0;i<ths.length;i++){ ths[i].classList.remove('asc','desc'); }
  ths[idx].classList.add(dir);
  if(!noSave) S('sort:'+tbl.id, {idx:idx, dir:dir});
}
function applyFilter(tbl, q){
  q=(q||'').toLowerCase(); var tb=tbl.tBodies[0]; if(!tb) return;
  [].forEach.call(tb.rows, function(r){
    if(r.classList.contains('empty')) return;
    r.style.display = (!q || r.textContent.toLowerCase().indexOf(q)>=0) ? '' : 'none';
  });
}
document.querySelectorAll('table.dt').forEach(function(tbl){
  var ths=tbl.tHead.rows[0].cells;
  for(var i=0;i<ths.length;i++){ (function(idx){
    ths[idx].addEventListener('click', function(){
      var cur=L('sort:'+tbl.id)||{}; var dir=(cur.idx===idx && cur.dir==='asc')?'desc':'asc';
      setSort(tbl, idx, dir);
    });
  })(i); }
  var st=L('sort:'+tbl.id); if(st && typeof st.idx==='number') setSort(tbl, st.idx, st.dir, true);
});
document.querySelectorAll('input.filter').forEach(function(inp){
  var tbl=document.getElementById(inp.getAttribute('data-t')); if(!tbl) return;
  var key='filter:'+tbl.id, saved=L(key);
  if(saved){ inp.value=saved; applyFilter(tbl, saved); }
  inp.addEventListener('input', function(){ applyFilter(tbl, inp.value); S(key, inp.value); });
  inp.addEventListener('dblclick', function(ev){ ev.stopPropagation(); });
});
document.querySelectorAll('.pbody').forEach(function(sc){
  var key='scroll:'+sc.id, s=L(key);
  if(s){ sc.scrollTop=s.top||0; sc.scrollLeft=s.left||0; }
  sc.addEventListener('scroll', throttle(function(){ S(key,{top:sc.scrollTop,left:sc.scrollLeft}); }, 150));
});

/* ---- log view: file selector + grep filter ---- */
(function(){
  var sel=document.getElementById('logsel'), filt=document.querySelector('.logfilter');
  var sc=document.getElementById('scroll-logs');
  function views(){ return [].slice.call(document.querySelectorAll('.logview')); }
  function current(){ var v=sel?sel.value:null; return document.querySelector('.logview[data-log="'+v+'"]') || views()[0]; }
  function show(){ views().forEach(function(v){ v.style.display=(!sel||v.getAttribute('data-log')===sel.value)?'':'none'; }); }
  function grep(q){ var v=current(); if(!v) return; q=(q||'').toLowerCase();
    [].forEach.call(v.querySelectorAll('.logline'), function(ln){
      ln.style.display=(!q||ln.textContent.toLowerCase().indexOf(q)>=0)?'':'none'; }); }
  if(sel){
    var sv=L('logsel'); if(sv){ for(var i=0;i<sel.options.length;i++){ if(sel.options[i].value===sv){ sel.value=sv; } } }
    show();
    sel.addEventListener('change', function(){ S('logsel', sel.value); show(); grep(filt?filt.value:''); });
    sel.addEventListener('dblclick', function(ev){ ev.stopPropagation(); });
  }
  if(filt){
    var sf=L('logfilter'); if(sf){ filt.value=sf; }
    filt.addEventListener('input', function(){ grep(filt.value); S('logfilter', filt.value); });
    filt.addEventListener('dblclick', function(ev){ ev.stopPropagation(); });
  }
  if(filt && filt.value) grep(filt.value);
  // jump to newest lines on first view unless the user has a saved scroll position
  if(sc && !L('scroll:scroll-logs')) sc.scrollTop = sc.scrollHeight;
})();

/* ---- resize (persist) + collapse (double-click title) ---- */
var panels=[].slice.call(document.querySelectorAll('.panel'));
panels.forEach(function(p){
  var sz=L('size:'+p.id); if(sz){ if(sz.w) p.style.width=sz.w; if(sz.h) p.style.height=sz.h; }
  if(L('collapsed:'+p.id)) p.classList.add('collapsed');
  var ph=p.querySelector('.phead');
  ph.addEventListener('dblclick', function(){
    p.classList.toggle('collapsed');
    S('collapsed:'+p.id, p.classList.contains('collapsed'));
  });
});
var preSize={};
document.addEventListener('mousedown', function(){ panels.forEach(function(p){ preSize[p.id]=p.offsetWidth+'x'+p.offsetHeight; }); });
document.addEventListener('mouseup', function(){ panels.forEach(function(p){
  if(p.classList.contains('collapsed') || p.offsetWidth===0) return;
  var cur=p.offsetWidth+'x'+p.offsetHeight;
  if(preSize[p.id] && preSize[p.id]!==cur){ S('size:'+p.id, {w:p.offsetWidth+'px', h:p.offsetHeight+'px'}); }
}); });

/* ---- panel show/hide/reorder config (persists until reset) ---- */
function panelKeys(){ return panels.map(function(p){ return p.id.replace('panel-',''); }); }
function panelTitle(k){ var p=document.getElementById('panel-'+k); var t=p&&p.querySelector('.ptitle'); return t?t.textContent:k; }
function defaultCfg(){ return {order: panelKeys(), hidden: []}; }
function loadCfg(){
  var c=L('panelcfg'); if(!c||!c.order) c=defaultCfg();
  c.hidden=c.hidden||[];
  panelKeys().forEach(function(k){ if(c.order.indexOf(k)<0) c.order.push(k); });   // include any new panels
  c.order=c.order.filter(function(k){ return document.getElementById('panel-'+k); }); // drop stale
  return c;
}
function applyCfg(c){
  var grid=document.querySelector('.grid');
  c.order.forEach(function(k,i){
    var p=document.getElementById('panel-'+k); if(!p) return;
    p.style.order=i; p.style.display=(c.hidden.indexOf(k)>=0)?'none':'';
    grid.appendChild(p);   // mirror order into the DOM so it also holds in packed (multicol) mode
  });
}
var pcfg=loadCfg(); applyCfg(pcfg);
function saveCfg(){ S('panelcfg', pcfg); }
var plist=document.getElementById('plist'), poverlay=document.getElementById('poverlay');
function renderPlist(){
  plist.innerHTML='';
  pcfg.order.forEach(function(k){
    var vis=pcfg.hidden.indexOf(k)<0;
    var row=document.createElement('div'); row.className='prow'+(vis?'':' hidden'); row.setAttribute('data-key',k);
    row.innerHTML='<input type="checkbox"'+(vis?' checked':'')+'><span class="pname">'+esc(panelTitle(k))+'</span>'
      +'<span class="pmove"><button data-dir="up" title="move up">&#9650;</button>'
      +'<button data-dir="down" title="move down">&#9660;</button></span>';
    plist.appendChild(row);
  });
}
plist.addEventListener('change', function(ev){
  if(ev.target.type!=='checkbox') return;
  var k=ev.target.closest('.prow').getAttribute('data-key'), i=pcfg.hidden.indexOf(k);
  if(ev.target.checked){ if(i>=0) pcfg.hidden.splice(i,1); } else if(i<0){ pcfg.hidden.push(k); }
  saveCfg(); applyCfg(pcfg); renderPlist();
});
plist.addEventListener('click', function(ev){
  var b=ev.target.closest('button[data-dir]'); if(!b) return;
  var k=b.closest('.prow').getAttribute('data-key'), idx=pcfg.order.indexOf(k);
  var j=(b.getAttribute('data-dir')==='up')?idx-1:idx+1; if(j<0||j>=pcfg.order.length) return;
  pcfg.order.splice(idx,1); pcfg.order.splice(j,0,k); saveCfg(); applyCfg(pcfg); renderPlist();
});
function closePanelsDlg(){ poverlay.style.display='none'; modalOpen=false; schedule(); }
/* leave the packed view and clear every per-panel resize / collapse override
   so panels fall back to the CSS grid defaults */
function resetPanelSizes(){
  var grid=document.querySelector('.grid');
  grid.classList.remove('packed'); grid.style.columnWidth='';
  try{ localStorage.removeItem('agentc:packed'); }catch(e){}
  panels.forEach(function(p){
    p.style.width=''; p.style.height=''; p.classList.remove('collapsed');
    try{ localStorage.removeItem('agentc:size:'+p.id); localStorage.removeItem('agentc:collapsed:'+p.id); }catch(e){}
  });
}
/* Fit each visible panel's HEIGHT to its content (widths untouched), then switch the
   grid into a multi-column "packed" layout so panels flow top-to-bottom and stack:
   several short panels fill the vertical space a single tall panel would occupy. */
function fitToContent(){
  var grid=document.querySelector('.grid');
  var maxH=Math.max(120, grid.clientHeight-6);   // never taller than the viewport
  var vis=[], ws=[];
  pcfg.order.forEach(function(k){
    if(pcfg.hidden.indexOf(k)>=0) return;                 // skip hidden
    var p=document.getElementById('panel-'+k);
    if(!p) return;
    if(p.classList.contains('collapsed')){ vis.push(p); return; } // keep collapsed, but it counts for column width
    ws.push(p.offsetWidth);                                // remember current widths (we won't change them)
    var head=p.querySelector('.phead'), body=p.querySelector('.pbody');
    // measure natural content height at the current width (table rows don't reflow on width)
    var ph=p.style.height; p.style.height='auto';
    var hh=head?head.offsetHeight:22, bh=body?body.scrollHeight:0;
    var H=Math.min(Math.max(hh+bh+2, hh+24), maxH);
    p.style.height=ph;
    p.dataset.fitH=H;                                      // stash; applied after measuring all
    vis.push(p);
  });
  // pick a column width from the panels' own widths so horizontal sizing is preserved (~same column count)
  ws.sort(function(a,b){ return a-b; });
  var colW=ws.length ? Math.min(ws[Math.floor(ws.length/2)], grid.clientWidth) : 300;
  grid.style.columnWidth=colW+'px';
  grid.classList.add('packed');
  // apply + persist the measured heights (height only — width is left to the column)
  vis.forEach(function(p){
    if(p.dataset.fitH){ p.style.height=p.dataset.fitH+'px'; S('size:'+p.id, {h:p.dataset.fitH+'px'}); delete p.dataset.fitH; }
  });
  S('packed', {cw:colW});
  applyCfg(pcfg); renderPlist();
}
document.getElementById('panelsbtn').addEventListener('click', function(){ renderPlist(); poverlay.style.display='flex'; modalOpen=true; schedule(); });
document.getElementById('pclose').addEventListener('click', closePanelsDlg);
document.getElementById('preset').addEventListener('click', function(){ resetPanelSizes(); pcfg=defaultCfg(); saveCfg(); applyCfg(pcfg); renderPlist(); });
document.getElementById('pfit').addEventListener('click', fitToContent);
poverlay.addEventListener('mousedown', function(ev){ if(ev.target===poverlay) closePanelsDlg(); });
if(L('packed')) fitToContent();   // restore packed view after a refresh (re-fits to current data)

/* ---- CRUD modal ---- */
var AGENT_FIELDS=[
  {k:'name', t:'text', req:true},
  {k:'cli', t:'select', opts:['claude','opencode','codex','gemini','mock'], req:true},
  {k:'provider', t:'select', opts:['anthropic','openai','nvidia','opencode','openrouter','google']},
  {k:'model', t:'text'},
  {k:'system_prompt', t:'textarea'},
  {k:'api_key_env', t:'text'},
  {k:'base_url', t:'text'},
  {k:'extra_args', label:'extra_args (JSON array)', t:'json', def:'[]'},
  {k:'timeout', label:'timeout (seconds, 0 = no limit)', t:'number', def:0},
  {k:'mock', t:'checkbox'},
  {k:'description', t:'textarea'}
];
var TASK_FIELDS=[
  {k:'name', t:'text', req:true},
  {k:'description', t:'textarea'},
  {k:'enabled', t:'checkbox', def:true},
  {k:'persist', t:'checkbox', def:true},
  {k:'trigger.type', label:'trigger type', t:'select', opts:['manual','schedule','event','file']},
  {k:'trigger.cron', label:'cron (schedule)', t:'text'},
  {k:'trigger.interval', label:'interval seconds (schedule)', t:'number'},
  {k:'trigger.event', label:'event name (event)', t:'text'},
  {k:'trigger.path', label:'watch path (file)', t:'text'},
  {k:'trigger.on', label:'on (file)', t:'select', opts:['','created','modified','deleted','moved','any']},
  {k:'trigger.pattern', label:'pattern (file)', t:'text'},
  {k:'trigger.recursive', label:'recursive (file)', t:'checkbox'},
  {k:'trigger.ignore', label:'ignore globs (JSON array, file)', t:'json', def:'[]'},
  {k:'variables', label:'variables (JSON object)', t:'json', def:'{}'},
  {k:'emits', label:'emits (JSON array)', t:'json', def:'[]'},
  {k:'actions', label:'actions (JSON array)', t:'json', def:'[]', req:true}
];
function fieldsFor(type){ return type==='agent'?AGENT_FIELDS:TASK_FIELDS; }
function getNested(o,k){ return k.split('.').reduce(function(a,p){ return (a==null)?undefined:a[p]; }, o); }
function setNested(o,k,v){ var ks=k.split('.'); var c=o; for(var i=0;i<ks.length-1;i++){ c[ks[i]]=c[ks[i]]||{}; c=c[ks[i]]; } c[ks[ks.length-1]]=v; }

var mform=document.getElementById('mform'), merrs=document.getElementById('merrs'),
    mtitle=document.getElementById('mtitle'), overlay=document.getElementById('overlay');
function buildForm(type, data){
  var h='';
  fieldsFor(type).forEach(function(f){
    var id='f_'+f.k.replace(/\./g,'_'), lbl=(f.label||f.k)+(f.req?' *':'');
    var val=getNested(data, f.k); if(val===undefined||val===null) val=(f.def!==undefined?f.def:'');
    if(f.t==='checkbox'){
      h+='<div class="frow inline"><input type="checkbox" id="'+id+'" data-k="'+f.k+'" data-t="bool"'+(val?' checked':'')+'><label for="'+id+'">'+lbl+'</label></div>';
      return;
    }
    h+='<div class="frow"><label for="'+id+'">'+lbl+'</label>';
    if(f.t==='textarea'){ h+='<textarea id="'+id+'" data-k="'+f.k+'" data-t="text">'+esc(val)+'</textarea>'; }
    else if(f.t==='json'){ var s=(typeof val==='string')?val:JSON.stringify(val,null,2); h+='<textarea id="'+id+'" class="mono" data-k="'+f.k+'" data-t="json">'+esc(s)+'</textarea>'; }
    else if(f.t==='select'){ h+='<select id="'+id+'" data-k="'+f.k+'" data-t="text">'+f.opts.map(function(o){ return '<option'+(String(o)===String(val)?' selected':'')+'>'+esc(o)+'</option>'; }).join('')+'</select>'; }
    else if(f.t==='number'){ h+='<input type="number" id="'+id+'" data-k="'+f.k+'" data-t="num" value="'+esc(val)+'">'; }
    else { h+='<input type="text" id="'+id+'" data-k="'+f.k+'" data-t="text" value="'+esc(val)+'">'; }
    h+='</div>';
  });
  return h;
}
function collectForm(){
  var obj={}, errs=[];
  mform.querySelectorAll('[data-k]').forEach(function(el){
    var k=el.getAttribute('data-k'), t=el.getAttribute('data-t'), v;
    if(t==='bool'){ v=el.checked; }
    else if(t==='num'){ if(el.value==='') return; v=parseFloat(el.value); }
    else if(t==='json'){ if(el.value.trim()===''){ return; } try{ v=JSON.parse(el.value); }catch(e){ errs.push(k+': invalid JSON ('+e.message+')'); return; } }
    else { v=el.value; if(v==='') return; }
    setNested(obj,k,v);
  });
  return {obj:obj, errs:errs};
}
function showErrors(list){ merrs.textContent=list.join('\n'); merrs.style.display=list.length?'block':'none'; }
function openModal(){ overlay.style.display='flex'; modalOpen=true; schedule(); }
function closeModal(){ overlay.style.display='none'; modalOpen=false; schedule(); }
function openForm(type, mode, name){
  CTYPE=type; CMODE=mode; CNAME=name||''; showErrors([]);
  mtitle.textContent=(mode==='edit'?'Edit ':'Add ')+type;
  if(mode==='edit'){
    fetch('/api/'+type+'s/'+encodeURIComponent(name)).then(function(r){return r.json();})
      .then(function(d){ mform.innerHTML=buildForm(type, d||{}); openModal(); })
      .catch(function(e){ mform.innerHTML=buildForm(type,{}); showErrors([String(e)]); openModal(); });
  } else { mform.innerHTML=buildForm(type, {}); openModal(); }
}
function submitForm(){
  var c=collectForm(); if(c.errs.length){ showErrors(c.errs); return; }
  var url='/api/'+CTYPE+'s'+(CMODE==='edit'?'/'+encodeURIComponent(CNAME):'');
  fetch(url, {method:(CMODE==='edit'?'PUT':'POST'), headers:{'Content-Type':'application/json'}, body:JSON.stringify(c.obj)})
    .then(function(r){ return r.json(); })
    .then(function(res){
      if(res.ok){ closeModal(); toast('Saved — restart the engine to apply trigger changes'); setTimeout(function(){ location.reload(); }, 700); }
      else { showErrors(res.errors||['save failed']); }
    }).catch(function(e){ showErrors([String(e)]); });
}
document.getElementById('msave').addEventListener('click', submitForm);
document.getElementById('mcancel').addEventListener('click', closeModal);
overlay.addEventListener('mousedown', function(ev){ if(ev.target===overlay) closeModal(); });

/* ---- confirm dialog ---- */
var coverlay=document.getElementById('coverlay'), cmsg=document.getElementById('cmsg'), _onYes=null;
function openConfirm(msg, onYes){ cmsg.textContent=msg; _onYes=onYes; coverlay.style.display='flex'; confirmOpen=true; schedule(); }
function closeConfirm(){ coverlay.style.display='none'; confirmOpen=false; _onYes=null; schedule(); }
document.getElementById('cno').addEventListener('click', closeConfirm);
document.getElementById('cyes').addEventListener('click', function(){ var f=_onYes; closeConfirm(); if(f) f(); });
coverlay.addEventListener('mousedown', function(ev){ if(ev.target===coverlay) closeConfirm(); });
function delItem(type, name){
  openConfirm('Delete '+type+' "'+name+'"? This permanently removes its config file.', function(){
    fetch('/api/'+type+'s/'+encodeURIComponent(name), {method:'DELETE'}).then(function(r){ return r.json(); })
      .then(function(res){ if(res.ok){ toast('Deleted '+name); setTimeout(function(){ location.reload(); }, 500); } else { toast('Delete failed'); } });
  });
}

/* ---- row selection (persists across auto-refresh) ---- */
var selByKind = L('rowsel') || {agent:null, task:null};
function saveSel(){ S('rowsel', selByKind); }
function tableForKind(kind){ return document.getElementById(kind==='agent'?'tbl-agents':'tbl-tasks'); }
function selectRow(tr){
  var tbl=tr.closest('table'); if(tbl){ [].forEach.call(tbl.querySelectorAll('tr.selected'),
    function(r){ r.classList.remove('selected'); }); }
  tr.classList.add('selected');
  var kind=tr.getAttribute('data-kind');
  if(kind==='agent'||kind==='task'){ selByKind[kind]=tr.getAttribute('data-name'); saveSel(); }
}
function reapplySel(){
  ['agent','task'].forEach(function(kind){
    var nm=selByKind[kind]; if(!nm) return;
    var tbl=tableForKind(kind); if(!tbl) return;
    var tr=tbl.querySelector('tr[data-name="'+(window.CSS&&CSS.escape?CSS.escape(nm):nm)+'"]');
    if(tr) tr.classList.add('selected');
  });
}
reapplySel();
// Click a row: runs open the detail modal; agents/tasks get selected for edit/del.
document.addEventListener('click', function(ev){
  if(ev.target.closest('[data-act]')) return;       // header buttons handled below
  var ol=ev.target.closest('a.olink');
  if(ol){ ev.preventDefault(); ev.stopPropagation(); openFolders(ol.getAttribute('data-dir')); return; }
  var tr=ev.target.closest('tbody tr');
  if(!tr || tr.classList.contains('empty') || !tr.hasAttribute('data-kind')) return;
  selectRow(tr);
  if(tr.getAttribute('data-kind')==='run') openRunDetail(tr.getAttribute('data-run'));
});

/* ---- button delegation ---- */
document.addEventListener('click', function(ev){
  var b=ev.target.closest('[data-act]'); if(!b) return;
  var act=b.getAttribute('data-act'), type=b.getAttribute('data-type'), name=b.getAttribute('data-name');
  if(act==='add') openForm(type,'add');
  else if(act==='edit') openForm(type,'edit',name);
  else if(act==='del') delItem(type,name);
  else if(act==='edit-sel'){ var n=selByKind[type]; if(!n){ toast('Select a '+type+' row first'); return; } openForm(type,'edit',n); }
  else if(act==='del-sel'){ var n2=selByKind[type]; if(!n2){ toast('Select a '+type+' row first'); return; } delItem(type,n2); }
  else if(act==='newfile') openNewFile();
});

/* ---- live runtime ticking for active runs ---- */
function tickRuntimes(){
  var now=Date.now()/1000;
  [].forEach.call(document.querySelectorAll('.runtime[data-since]'), function(el){
    var since=parseFloat(el.getAttribute('data-since'))||0; if(!since){ el.textContent='—'; return; }
    var s=Math.max(0, Math.floor(now-since));
    if(s<60) el.textContent=s+'s';
    else if(s<3600) el.textContent=Math.floor(s/60)+'m'+String(s%60).padStart(2,'0')+'s';
    else el.textContent=Math.floor(s/3600)+'h'+String(Math.floor(s/60)%60).padStart(2,'0')+'m';
  });
}
tickRuntimes(); setInterval(tickRuntimes, 1000);

/* ---- run detail modal ---- */
var roverlay=document.getElementById('roverlay'), rdbody=document.getElementById('rdbody'),
    rdtitle=document.getElementById('rdtitle');
function escd(s){ return esc(s==null?'':s); }
function openRunDetail(id){
  if(!id) return;
  modalOpen=true; schedule();
  rdtitle.textContent='Run '+id;
  rdbody.innerHTML='loading…'; roverlay.style.display='flex';
  fetch('/api/run/'+encodeURIComponent(id)).then(function(r){ return r.json(); })
    .then(function(d){ rdbody.innerHTML=renderRunDetail(d); })
    .catch(function(e){ rdbody.innerHTML='<div class="rd-empty">failed to load: '+escd(e)+'</div>'; });
}
function renderRunDetail(d){
  if(!d || d.errors){ return '<div class="rd-empty">run not found</div>'; }
  var dur = (d.finished&&d.started)? (d.finished-d.started).toFixed(2)+'s' : '(running)';
  var h='<div class="rd-meta">'
    +'<span>task <b>'+escd(d.task)+'</b></span>'
    +'<span>status <b>'+escd(d.status)+'</b></span>'
    +'<span>trigger <b>'+escd(d.trigger)+'</b></span>'
    +'<span>duration <b>'+dur+'</b></span>'
    +'<span>id <b>'+escd(d.id)+'</b></span>'
    +(d.error?'<span>error <b style="color:#f85149">'+escd(d.error)+'</b></span>':'')
    +'</div>';
  h+='<div class="rd-sec">Actions ('+((d.results||[]).length)+')</div>';
  if((d.results||[]).length){
    d.results.forEach(function(a){
      h+='<div class="rd-action"><div class="rh">'
        +'<span class="rn">'+escd(a.name)+'</span>'
        +'<span class="badge '+(a.success?'ok':'bad')+'">'+(a.success?'ok':'fail')+'</span>'
        +'<span style="color:#6e7681">'+escd(a.type)+' · '+(a.duration||0).toFixed(2)+'s</span></div>';
      if(a.stdout) h+='<div class="rd-out">'+escd(a.stdout)+'</div>';
      if(a.stderr) h+='<div class="rd-out err">'+escd(a.stderr)+'</div>';
      if(a.error) h+='<div class="rd-out err">'+escd(a.error)+'</div>';
      h+='</div>';
    });
  } else { h+='<div class="rd-empty">no actions recorded</div>'; }
  var vk=Object.keys(d.variables||{});
  if(vk.length){ h+='<div class="rd-sec">Variables</div><div class="rd-out">'
    +escd(JSON.stringify(d.variables,null,2))+'</div>'; }
  h+='<div class="rd-sec">Log messages ('+((d.log_lines||[]).length)+')</div>';
  if((d.log_lines||[]).length){
    h+='<div class="rd-out" style="max-height:200px">'
      +d.log_lines.map(function(l){ return '<div class="rd-logline">'+escd(l)+'</div>'; }).join('')
      +'</div>';
  } else { h+='<div class="rd-empty">no log lines mention this run</div>'; }
  return h;
}
function closeRunDetail(){ roverlay.style.display='none'; modalOpen=false; schedule(); }
document.getElementById('rdclose').addEventListener('click', closeRunDetail);
roverlay.addEventListener('mousedown', function(ev){ if(ev.target===roverlay) closeRunDetail(); });

/* ---- folder browser (Monitored folders panel) ---- */
var SELECTED_FOLDER = L('selfolder') || null;
(function(){
  var bs=document.querySelector('.fbrowse'); if(!bs) return;
  function selectFolder(folder){
    [].forEach.call(document.querySelectorAll('.folder.selected'), function(f){ f.classList.remove('selected'); });
    folder.classList.add('selected'); SELECTED_FOLDER=folder.getAttribute('data-path'); S('selfolder', SELECTED_FOLDER);
  }
  // restore selection / first folder as default target
  var folders=[].slice.call(document.querySelectorAll('.folder'));
  var match=folders.filter(function(f){ return f.getAttribute('data-path')===SELECTED_FOLDER; })[0];
  if(match) match.classList.add('selected'); else if(folders[0]){ SELECTED_FOLDER=folders[0].getAttribute('data-path'); }
  bs.addEventListener('click', function(ev){
    var fh=ev.target.closest('.fhead'); if(!fh) return;
    var folder=fh.closest('.folder');
    folder.classList.toggle('open');
    var body=folder.querySelector('.fbody'); if(body) body.style.display=folder.classList.contains('open')?'':'none';
    selectFolder(folder);
  });
})();
function openFolders(dir){
  // expand the monitored folder that owns this output dir and scroll it into view
  var panel=document.getElementById('panel-folders'); if(panel) panel.classList.remove('collapsed');
  var folders=[].slice.call(document.querySelectorAll('.folder'));
  var owner=folders.filter(function(f){ var p=f.getAttribute('data-path'); return dir && (dir===p || dir.indexOf(p+'/')===0); })[0];
  if(owner){ owner.classList.add('open'); var b=owner.querySelector('.fbody'); if(b) b.style.display='';
    owner.scrollIntoView({block:'nearest'}); toast('Output dir: '+dir); }
  else { toast('Output dir: '+dir); }
}

/* ---- new file modal ---- */
var noverlay=document.getElementById('noverlay'), nerrs=document.getElementById('nerrs');
function openNewFile(){
  if(!SELECTED_FOLDER){ var f0=document.querySelector('.folder'); SELECTED_FOLDER=f0?f0.getAttribute('data-path'):null; }
  if(!SELECTED_FOLDER){ toast('No monitored folder to add to'); return; }
  document.getElementById('ndir').value=SELECTED_FOLDER;
  document.getElementById('nname').value=''; document.getElementById('ncontent').value='';
  nerrs.style.display='none';
  modalOpen=true; schedule(); noverlay.style.display='flex';
  setTimeout(function(){ document.getElementById('nname').focus(); }, 30);
}
function closeNewFile(){ noverlay.style.display='none'; modalOpen=false; schedule(); }
function saveNewFile(){
  var name=document.getElementById('nname').value.trim(),
      content=document.getElementById('ncontent').value,
      dir=document.getElementById('ndir').value;
  if(!name){ nerrs.textContent='file name is required'; nerrs.style.display='block'; return; }
  fetch('/api/fs', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({dir:dir, name:name, content:content})})
    .then(function(r){ return r.json(); })
    .then(function(res){
      if(res.ok){ closeNewFile(); toast('Created '+name+' in '+dir); setTimeout(function(){ location.reload(); }, 600); }
      else { nerrs.textContent=(res.errors||['save failed']).join('\n'); nerrs.style.display='block'; }
    }).catch(function(e){ nerrs.textContent=String(e); nerrs.style.display='block'; });
}
document.getElementById('nsave').addEventListener('click', saveNewFile);
document.getElementById('ncancel').addEventListener('click', closeNewFile);
noverlay.addEventListener('mousedown', function(ev){ if(ev.target===noverlay) closeNewFile(); });

/* ---- toast ---- */
var toastEl=document.getElementById('toast'), _tt;
function toast(msg){ toastEl.textContent=msg; toastEl.classList.add('show'); clearTimeout(_tt); _tt=setTimeout(function(){ toastEl.classList.remove('show'); }, 2600); }

/* ---- auto-refresh (paused while a dialog is open) ---- */
var pause=document.getElementById('pause'); pause.checked=!!L('paused');
function schedule(){ if(window._t) clearTimeout(window._t);
  if(pause.checked || modalOpen || confirmOpen) return;
  window._t=setTimeout(function(){ location.reload(); }, REFRESH*1000); }
pause.addEventListener('change', function(){ S('paused', pause.checked); schedule(); });
document.getElementById('reload').addEventListener('click', function(){ location.reload(); });

/* ---- engine on/off (drives the systemd engine service) ---- */
(function(){
  var box=document.getElementById('engctl'); if(!box) return;
  var bStart=document.getElementById('eng-start'), bStop=document.getElementById('eng-stop'),
      bRestart=document.getElementById('eng-restart');
  // Reflect current state: offer Start when down, Stop/Restart when up.
  bStart.style.display = ENGINE_ALIVE ? 'none' : '';
  bStop.style.display  = ENGINE_ALIVE ? '' : 'none';
  bRestart.style.display = ENGINE_ALIVE ? '' : 'none';
  function ctl(action, btn){
    [bStart,bStop,bRestart].forEach(function(b){ b.disabled=true; });
    var old=btn.textContent; btn.textContent='…';
    fetch('/api/engine', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:action})})
      .then(function(r){ return r.json(); })
      .then(function(res){
        if(res.ok){ toast('Engine '+action+'ed'); }
        else { toast('Engine '+action+' failed: '+(res.message||'error')); }
        // Give systemd a moment to flip state + the engine to write its heartbeat.
        setTimeout(function(){ location.reload(); }, action==='stop'?900:1800);
      })
      .catch(function(e){ toast('Engine '+action+' error: '+e);
        [bStart,bStop,bRestart].forEach(function(b){ b.disabled=false; }); btn.textContent=old; });
  }
  bStart.addEventListener('click', function(){ ctl('start', bStart); });
  bStop.addEventListener('click', function(){ ctl('stop', bStop); });
  bRestart.addEventListener('click', function(){ ctl('restart', bRestart); });
})();
document.addEventListener('keydown', function(ev){ if(ev.key==='Escape'){
  if(confirmOpen) closeConfirm();
  else if(roverlay.style.display==='flex') closeRunDetail();
  else if(noverlay.style.display==='flex') closeNewFile();
  else if(poverlay.style.display==='flex') closePanelsDlg();
  else if(modalOpen) closeModal();
} });
function tick(){ var age=Math.floor(Date.now()/1000)-GEN; var a=document.getElementById('ago'); if(a) a.textContent=age+'s'+(age>STALE?' STALE':''); }
tick(); setInterval(tick, 1000); schedule();
</script>
</body>
</html>
"""
