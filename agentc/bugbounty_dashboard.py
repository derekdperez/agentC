"""Bugbounty web UI — a focused control plane for the recon pipeline.

A standalone HTTP service (separate from the engine dashboard) that surfaces the
filesystem state under ``bugbounty/``:

  * **Targets**     — one row per target domain (rich, UI-managed metadata).
  * **Subdomains**  — every ``{hostname}`` discovered under a target.
  * **Assets**      — every downloaded body, with status / size / content-type.
  * **Requests**    — the pending → ready → completed pipeline, by status.
  * **Activity**    — engine state, per-domain rate-limit slots, recent runs.

It reuses the engine dashboard's CSS, table/panel renderers and run-detail modal
so the look and feel are identical. UI-managed target metadata lives in a
``meta.json`` sidecar beside the pipeline's ``state.json`` so the two never
clobber each other.

Run directly::

    python3 -m agentc.bugbounty_dashboard --host 127.0.0.1 --port 8766
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from .dashboard import (
    Paths, e, fmt_size, fmt_ts, fmt_dur, badge, table, panel,
    get_run_detail, load_runs, _load_json,
)

REFRESH_SECONDS = 5
STALE_AFTER = 12
MAX_ROWS = 600          # cap rows per table so a huge crawl can't bloat the page

ASSET_TYPES = ("html", "scripts", "stylesheets", "images", "archives", "bin", "critical")
TARGET_STATUSES = ("active", "paused", "archived")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _root() -> str:
    return os.environ.get("AGENTC_ROOT") or os.getcwd()


def bb_dir(*parts) -> str:
    return os.path.join(_root(), "bugbounty", *parts)


def targets_dir() -> str:
    return bb_dir("targets")


def requests_dir() -> str:
    return bb_dir("requests")


# --------------------------------------------------------------------------- #
# Domain helpers
# --------------------------------------------------------------------------- #
def normalize_domain(raw: str) -> str:
    d = (raw or "").strip()
    if d.startswith("http://") or d.startswith("https://"):
        d = d.split("://", 1)[1]
    d = d.rstrip("/").split("/")[0]
    return d


_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def valid_domain(d: str) -> bool:
    return bool(d) and "." in d and bool(_DOMAIN_RE.match(d)) and ".." not in d


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def _meta_defaults(domain: str) -> dict:
    return {"program": "", "status": "active", "tags": [],
            "scope_in": [], "scope_out": [], "notes": ""}


def load_meta(domain: str) -> dict:
    meta = _meta_defaults(domain)
    got = _load_json(os.path.join(targets_dir(), domain, "meta.json"))
    if isinstance(got, dict):
        meta.update({k: got.get(k, meta[k]) for k in meta})
    return meta


def _hostnames_for(target_path: str) -> list:
    """Sub-directories of a target that look like a hostname (hold an assets/)."""
    out = []
    try:
        for name in os.listdir(target_path):
            p = os.path.join(target_path, name)
            if os.path.isdir(p) and os.path.isdir(os.path.join(p, "assets")):
                out.append(name)
    except OSError:
        pass
    return sorted(out)


def _asset_counts(target_path: str) -> dict:
    """Count downloaded bodies under a target, keyed by asset type, plus total."""
    counts = {t: 0 for t in ASSET_TYPES}
    total = 0
    for body in glob.glob(os.path.join(target_path, "*", "assets", "*", "*.body")):
        total += 1
        atype = os.path.basename(os.path.dirname(body))
        if atype in counts:
            counts[atype] += 1
    counts["total"] = total
    return counts


def load_targets() -> list:
    out = []
    base = targets_dir()
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return out
    for name in entries:
        if name == "queue":
            continue
        tpath = os.path.join(base, name)
        if not os.path.isdir(tpath):
            continue
        state = _load_json(os.path.join(tpath, "state.json")) or {}
        meta = load_meta(name)
        hostnames = _hostnames_for(tpath)
        assets = _asset_counts(tpath)
        out.append({
            "domain": name,
            "program": meta["program"],
            "status": meta["status"],
            "tags": meta["tags"],
            "scope_in": meta["scope_in"],
            "scope_out": meta["scope_out"],
            "notes": meta["notes"],
            "subdomains": hostnames,
            "n_sub": len(hostnames),
            "assets": assets,
            "n_assets": assets["total"],
            "requested": len(state.get("requested_urls", []) or []),
            "discovered": len(state.get("discovered_urls", []) or []),
            "created_at": state.get("created_at", ""),
        })
    return out


def load_subdomains(targets: list) -> list:
    rows = []
    base = targets_dir()
    for t in targets:
        for host in t["subdomains"]:
            hpath = os.path.join(base, t["domain"], host)
            counts = {at: 0 for at in ASSET_TYPES}
            total = 0
            last = 0.0
            for body in glob.glob(os.path.join(hpath, "assets", "*", "*.body")):
                total += 1
                at = os.path.basename(os.path.dirname(body))
                if at in counts:
                    counts[at] += 1
                try:
                    last = max(last, os.path.getmtime(body))
                except OSError:
                    pass
            rows.append({"target": t["domain"], "hostname": host,
                         "n_assets": total, "counts": counts, "last": last})
    return rows


def load_assets() -> list:
    rows = []
    for body in glob.glob(os.path.join(targets_dir(), "*", "*", "assets", "*", "*.body")):
        parts = body.split(os.sep)
        try:
            idx = parts.index("targets")
            target = parts[idx + 1]
            hostname = parts[idx + 2]
        except (ValueError, IndexError):
            continue
        atype = os.path.basename(os.path.dirname(body))
        name = os.path.basename(body)[:-5]  # strip .body
        meta = _load_json(body[:-5] + ".json") or {}
        try:
            size = os.path.getsize(body)
            mtime = os.path.getmtime(body)
        except OSError:
            size, mtime = 0, 0
        rows.append({
            "target": target, "hostname": hostname, "type": atype, "name": name,
            "url": meta.get("url", ""), "status": meta.get("status_code", ""),
            "content_type": (meta.get("content_type", "") or "").split(";")[0],
            "size": size, "fetched": meta.get("requested_at", "") or mtime,
            "mtime": mtime, "path": body,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def _epoch(val) -> float:
    """Coerce a fetched/created value (epoch float or ISO string) to epoch secs."""
    v = _ts(val)
    return float(v) if isinstance(v, (int, float)) else 0.0


def assets_version(assets: list) -> str:
    """A cheap change-token for the asset set: count + newest mtime.

    The client compares this against its cached copy; an unchanged token means
    the cached rows are still valid, so no re-fetch is needed."""
    mx = max((a["mtime"] for a in assets), default=0)
    return f"{len(assets)}:{int(mx)}"


def asset_rows_json(assets: list) -> list:
    """Compact per-asset row arrays for the virtualized client table.

    Column order must match render_assets_panel() headers:
    [target, host, type, name, status, ctype, size, url, fetched_epoch, relpath]
    """
    rows = []
    for a in assets:
        rel = os.path.relpath(a["path"], _root())
        rows.append([
            a["target"], a["hostname"], a["type"], a["name"],
            a["status"], a["content_type"], a["size"], a["url"],
            _epoch(a["fetched"]) or a["mtime"], rel,
        ])
    return rows


def requests_version(summary: dict) -> str:
    return f"{summary['completed_total']}:{summary['pending']}:{summary['ready']}"


def request_rows_json(completed: list) -> list:
    """Compact rows for the virtualized requests table:
    [status, domain, url, source, when_epoch]."""
    return [[r["status"], r["domain"], r["url"], r["source"], r["when"]]
            for r in completed]


def _count_files(path) -> int:
    try:
        return sum(1 for n in os.listdir(path) if n.endswith(".json"))
    except OSError:
        return 0


def load_request_summary() -> dict:
    rd = requests_dir()
    comp = os.path.join(rd, "completed")
    by_status = {}
    try:
        for st in sorted(os.listdir(comp)):
            p = os.path.join(comp, st)
            if os.path.isdir(p):
                by_status[st] = _count_files(p)
    except OSError:
        pass
    return {
        "pending": _count_files(os.path.join(rd, "pending")),
        "ready": _count_files(os.path.join(rd, "ready")),
        "completed_by_status": by_status,
        "completed_total": sum(by_status.values()),
    }


def load_recent_completed(limit=MAX_ROWS) -> list:
    """Recent completed requests across all status buckets, newest first."""
    comp = os.path.join(requests_dir(), "completed")
    files = glob.glob(os.path.join(comp, "*", "*.json"))
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
               reverse=True)
    rows = []
    for p in files[:limit]:
        d = _load_json(p) or {}
        status_dir = os.path.basename(os.path.dirname(p))
        # Two on-disk shapes: {request, response} or a flat request dict.
        if "response" in d or "request" in d:
            req = d.get("request", {}) or {}
            resp = d.get("response", {}) or {}
            status = resp.get("status_code", status_dir)
        else:
            req = d
            status = status_dir
        rows.append({
            "status": status,
            "domain": req.get("domain", ""),
            "url": req.get("url", ""),
            "source": req.get("source_type", "spider"),
            "when": os.path.getmtime(p) if os.path.exists(p) else 0,
        })
    return rows


def load_rate_state() -> dict:
    d = _load_json(os.path.join(requests_dir(), "rate_state.json"))
    return d if isinstance(d, dict) else {}


def load_pending_by_domain(limit=4000) -> dict:
    """Count pending+ready requests per domain (filename-cheap parse, capped)."""
    counts = {}
    for sub in ("pending", "ready"):
        d = os.path.join(requests_dir(), sub)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names[:limit]:
            if not n.endswith(".json"):
                continue
            data = _load_json(os.path.join(d, n)) or {}
            dom = data.get("domain", "?")
            counts[dom] = counts.get(dom, 0) + 1
    return counts


def load_bb_runs(paths: Paths, limit=40) -> list:
    runs = [r for r in load_runs(paths.state_dir)
            if str(r.get("task", "")).startswith("bugbounty")]
    return runs[:limit]


def load_recent_runs(paths: Paths, n=500) -> list:
    """Load the *n* most-recently-modified run records (cheap, bounded)."""
    files = glob.glob(os.path.join(paths.state_dir, "runs", "*.json"))
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
               reverse=True)
    out = []
    for p in files[:n]:
        d = _load_json(p)
        if d:
            out.append(d)
    return out


# Noise tasks excluded from the activity feed (they fire every few seconds).
_FEED_SKIP = {"dashboard", "monitor-files"}
# Internal-plumbing stdout lines that aren't interesting activity.
_FEED_NOISE = ("File already removed", "Request file not found",
               "Skipping non-JSON file")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _clean(line: str) -> str:
    return _ANSI_RE.sub("", line).strip()


def _humanize(line: str) -> str:
    """Light touch-ups so feed lines read like a narrative."""
    line = line.strip()
    if line.startswith("Completed request:"):
        return "Requested " + line[len("Completed request:"):].strip()
    if line.startswith("Created domain state:"):
        return "Target initialised — " + line.split(":", 1)[1].strip()
    return line


def load_feed(paths: Paths, limit=400) -> list:
    """A unified, time-ordered activity stream built from every task's run
    records: each meaningful stdout line (and any failure) becomes one entry."""
    entries = []
    for r in load_recent_runs(paths, n=500):
        task = r.get("task", "")
        if task in _FEED_SKIP:
            continue
        t = r.get("finished") or r.get("started") or 0
        results = r.get("results") or []
        if not results:
            status = r.get("status", "")
            if status in ("running", "failed", "interrupted", "skipped"):
                verb = {"running": "running", "failed": "failed",
                        "interrupted": "interrupted", "skipped": "skipped"}[status]
                entries.append({"time": t, "task": task, "status": status,
                                "text": f"task {verb}" + (f": {r['error']}" if r.get("error") else "")})
            continue
        for a in results:
            ok = a.get("success")
            for raw in (a.get("stdout") or "").splitlines():
                line = _clean(raw)
                if not line or line.startswith("::set"):
                    continue
                if any(n in line for n in _FEED_NOISE):
                    continue
                entries.append({"time": t, "task": task,
                                "status": "ok" if ok else "fail",
                                "text": _humanize(line)})
            if not ok:
                err = _clean((a.get("stderr") or a.get("error") or ""))
                lines = [l for l in err.splitlines() if _clean(l)
                         and not any(n in l for n in _FEED_NOISE)]
                if lines:
                    entries.append({"time": t, "task": task, "status": "fail",
                                    "text": lines[-1][:240]})
    entries.sort(key=lambda x: x["time"], reverse=True)
    return entries[:limit]


def engine_status() -> dict:
    try:
        from . import service
        return service.status()
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #
def _write_meta(domain: str, data: dict) -> None:
    tpath = os.path.join(targets_dir(), domain)
    os.makedirs(tpath, exist_ok=True)
    meta = _meta_defaults(domain)
    meta.update({k: data.get(k, meta[k]) for k in meta})
    # Coerce list-ish fields from textarea/CSV input.
    meta["tags"] = _as_list(data.get("tags", meta["tags"]), sep=",")
    meta["scope_in"] = _as_list(data.get("scope_in", meta["scope_in"]))
    meta["scope_out"] = _as_list(data.get("scope_out", meta["scope_out"]))
    if meta["status"] not in TARGET_STATUSES:
        meta["status"] = "active"
    with open(os.path.join(tpath, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)


def _as_list(val, sep="\n") -> list:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return [x.strip() for x in str(val or "").replace("\r", "").split(sep) if x.strip()]


def _drop_queue(domain: str) -> None:
    q = os.path.join(targets_dir(), "queue")
    os.makedirs(q, exist_ok=True)
    open(os.path.join(q, domain), "w").close()


def add_target(data: dict):
    domain = normalize_domain(data.get("domain", ""))
    if not valid_domain(domain):
        return 400, {"ok": False, "errors": ["invalid domain"]}
    if os.path.isdir(os.path.join(targets_dir(), domain)):
        return 409, {"ok": False, "errors": [f"target {domain} already exists"]}
    _write_meta(domain, data)
    _drop_queue(domain)  # fires bugbounty-spider-init (state + initial + probes)
    return 200, {"ok": True, "domain": domain}


def edit_target(domain: str, data: dict):
    domain = normalize_domain(domain)
    if not os.path.isdir(os.path.join(targets_dir(), domain)):
        return 404, {"ok": False, "errors": ["target not found"]}
    _write_meta(domain, data)
    return 200, {"ok": True, "domain": domain}


def delete_target(domain: str):
    domain = normalize_domain(domain)
    tpath = os.path.join(targets_dir(), domain)
    if not os.path.isdir(tpath):
        return 404, {"ok": False, "errors": ["target not found"]}
    shutil.rmtree(tpath, ignore_errors=True)
    # Drop the domain from rate state and any stale queue trigger.
    rs_path = os.path.join(requests_dir(), "rate_state.json")
    rs = load_rate_state()
    if domain in rs:
        rs.pop(domain, None)
        try:
            with open(rs_path, "w", encoding="utf-8") as fh:
                json.dump(rs, fh, indent=2)
        except OSError:
            pass
    qf = os.path.join(targets_dir(), "queue", domain)
    if os.path.exists(qf):
        os.remove(qf)
    return 200, {"ok": True, "domain": domain}


def reprobe_target(domain: str):
    domain = normalize_domain(domain)
    if not os.path.isdir(os.path.join(targets_dir(), domain)):
        return 404, {"ok": False, "errors": ["target not found"]}
    _drop_queue(domain)
    return 200, {"ok": True, "domain": domain}


def read_asset(rel: str):
    """Return (content_bytes, content_type) for an asset body inside targets/."""
    base = os.path.realpath(targets_dir())
    full = os.path.realpath(os.path.join(_root(), rel))
    if not full.startswith(base + os.sep):
        return None, None
    try:
        with open(full, "rb") as fh:
            data = fh.read()
    except OSError:
        return None, None
    meta = _load_json(full[:-5] + ".json") if full.endswith(".body") else {}
    ctype = (meta or {}).get("content_type") or "text/plain; charset=utf-8"
    return data, ctype


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _tag_chips(tags) -> str:
    return " ".join(f'<span class="badge mut">{e(t)}</span>' for t in tags) or "—"


def _status_badge(status) -> str:
    cls = {"active": "ok", "paused": "warn", "archived": "mut"}.get(status, "mut")
    return badge(status or "?", cls)


def _http_badge(code) -> str:
    s = str(code)
    if s.startswith("2"):
        cls = "ok"
    elif s.startswith("3"):
        cls = "run"
    elif s.startswith("4") or s.startswith("5"):
        cls = "bad"
    else:
        cls = "mut"
    return badge(s or "?", cls)


def render_targets_panel(targets) -> str:
    headers = ["domain", "status", "program", "tags", "subs", "assets",
               "requested", "discovered", "created", ""]
    rows, meta = [], []
    for t in targets:
        acts = (f'<span class="act">'
                f'<button class="mini" data-act="edit" data-domain="{e(t["domain"])}">edit</button>'
                f'<button class="mini" data-act="probe" data-domain="{e(t["domain"])}">re-probe</button>'
                f'<button class="mini bad" data-act="del" data-domain="{e(t["domain"])}">del</button>'
                f'</span>')
        rows.append([
            e(t["domain"]), _status_badge(t["status"]), e(t["program"] or "—"),
            _tag_chips(t["tags"]), t["n_sub"], t["n_assets"],
            t["requested"], t["discovered"], fmt_ts(_ts(t["created_at"])), acts,
        ])
        meta.append({"kind": "target", "domain": t["domain"]})
    buttons = '<button class="add" data-act="add-target">+ add target</button>'
    return panel("targets", "Targets", len(targets),
                 table("tbl-targets", headers, rows, meta), head_buttons=buttons)


def render_subdomains_panel(subs) -> str:
    headers = ["target", "hostname", "assets", "html", "scripts", "css",
               "img", "critical", "last seen"]
    rows, meta = [], []
    for s in subs:
        c = s["counts"]
        rows.append([
            e(s["target"]), e(s["hostname"]), s["n_assets"],
            c["html"], c["scripts"], c["stylesheets"], c["images"], c["critical"],
            fmt_ts(s["last"]),
        ])
        # Selecting a subdomain row scopes the Assets panel to that host.
        meta.append({"kind": "sub", "target": s["target"], "host": s["hostname"],
                     "_class": "selectable"})
    head = ('<span class="filterbar" id="subfilter" style="display:none">'
            'filtered: <b id="subfilterval"></b>'
            '<button class="mini" id="subfilterclear">&#10005; clear</button>'
            '</span>'
            '<input id="subq" class="filter" placeholder="search…" '
            'spellcheck="false" autocomplete="off">')
    return panel("subdomains", "Subdomains", len(subs),
                 table("tbl-subdomains", headers, rows, meta),
                 head_buttons=head, filter_for=False)


def _asset_selector(targets) -> str:
    """A domain/subdomain dropdown that scopes the assets table."""
    opts = ['<option value="all">all assets</option>']
    for t in targets:
        dom = t["domain"]
        opts.append(f'<option value="{e(dom)}||*">{e(dom)} (all)</option>')
        for host in t["subdomains"]:
            label = host if host != dom else f"{host} (apex)"
            opts.append(f'<option value="{e(dom)}||{e(host)}">&nbsp;&nbsp;{e(label)}</option>')
    return f'<select id="assetsel" class="logsel" title="scope to a domain / subdomain">{"".join(opts)}</select>'


def render_assets_panel(assets, targets) -> str:
    # The body is rendered client-side as a *virtualized* table fed by
    # /api/assets (no row cap — only the visible window is in the DOM). The
    # server emits just an empty shell + headers so the look/feel matches.
    headers = ["target", "host", "type", "name", "status", "ctype", "size",
               "url", "fetched"]
    head = (_asset_selector(targets)
            + '<input id="assetq" class="filter" placeholder="search…" '
              'spellcheck="false" autocomplete="off">')
    return panel("assets", "Assets", len(assets),
                 table("tbl-assets", headers, [], None),
                 head_buttons=head, filter_for=False)


def render_requests_panel(summary) -> str:
    # Body is a client-side virtualized table fed by /api/requests (no cap).
    headers = ["status", "domain", "url", "source", "when"]
    sc = summary["completed_by_status"]
    sc_txt = " ".join(f"{k}:{v}" for k, v in sorted(sc.items()))
    sub = (f'pending <b>{summary["pending"]}</b> · ready <b>{summary["ready"]}</b> · '
           f'completed <b>{summary["completed_total"]}</b> ({e(sc_txt)})')
    note = (f'<span class="count" style="padding:0 6px">{sub}</span>'
            '<input id="reqq" class="filter" placeholder="search…" '
            'spellcheck="false" autocomplete="off">')
    return panel("requests", "Requests", summary["completed_total"],
                 table("tbl-requests", headers, [], None),
                 head_buttons=note, filter_for=False)


def render_activity_panel(paths, eng, rate, pend_by_dom, runs) -> str:
    headers = ["task", "status", "started", "duration", "trigger"]
    rows, meta = [], []
    from .dashboard import status_badge
    now = time.time()
    for r in runs:
        started = r.get("started", 0)
        finished = r.get("finished")
        if finished:
            dur = fmt_dur(finished - started)
        elif started:
            dur = (f'<span class="runtime" data-since="{started}">'
                   f'{fmt_dur(now - started)}</span>')
        else:
            dur = "—"
        rows.append([
            e(r.get("task", "")), status_badge(r.get("status", "")),
            fmt_ts(started), dur, e(r.get("trigger", "")),
        ])
        meta.append({"kind": "run", "run": r.get("id", ""), "_class": "rrow"})
    return panel("activity", "Activity", len(runs),
                 table("tbl-activity", headers, rows, meta), filter_for="tbl-activity")


def render_feed_panel(feed) -> str:
    headers = ["time", "task", "", "activity"]
    rows = []
    for f in feed:
        cls = {"ok": "ok", "fail": "bad", "running": "run",
               "interrupted": "warn", "skipped": "mut"}.get(f["status"], "mut")
        rows.append([
            fmt_ts(f["time"]),
            f'<span class="badge mut">{e(f["task"])}</span>',
            badge("●", cls),
            f'<span title="{e(f["text"])}">{e(f["text"][:200])}</span>',
        ])
    return panel("feed", "Activity feed", len(feed),
                 table("tbl-feed", headers, rows), filter_for="tbl-feed")


def render_rate_panel(rate, pend_by_dom) -> str:
    headers = ["domain", "queued", "last slot", "next ready"]
    now = time.time()
    domains = sorted(set(list(rate.keys()) + list(pend_by_dom.keys())))
    rows = []
    for d in domains:
        last = rate.get(d, 0)
        queued = pend_by_dom.get(d, 0)
        if last and last > now:
            nxt = f'<span class="runtime">in {last - now:.1f}s</span>'
        else:
            nxt = "ready"
        rows.append([e(d), queued, fmt_ts(last) if last else "—",
                     nxt if queued else "—"])
    return panel("rate", "Rate limits", len(domains),
                 table("tbl-rate", headers, rows))


def _ts(val):
    """Accept either epoch float or ISO string; return something fmt_ts handles."""
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str) and val:
        try:
            return time.mktime(time.strptime(val[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            return val
    return ""


def _header_stats(targets, subs, assets, summary, eng):
    alive = eng.get("active") or eng.get("running")
    eng_txt = "engine up" if alive else "engine down"
    eng_cls = "ok" if alive else "bad"
    return (
        f'<span class="hi"><b>{len(targets)}</b> targets</span>'
        f'<span class="hi"><b>{len(subs)}</b> subdomains</span>'
        f'<span class="hi"><b>{len(assets)}</b> assets</span>'
        f'<span class="hi">pending <b>{summary["pending"]}</b></span>'
        f'<span class="hi">ready <b>{summary["ready"]}</b></span>'
        f'<span class="hi">done <b>{summary["completed_total"]}</b></span>'
        f'<span class="badge {eng_cls}">{eng_txt}</span>'
    )


def render_page(paths: Paths) -> str:
    targets = load_targets()
    subs = load_subdomains(targets)
    assets = load_assets()
    summary = load_request_summary()
    rate = load_rate_state()
    pend_by_dom = load_pending_by_domain()
    runs = load_bb_runs(paths)
    feed = load_feed(paths)
    eng = engine_status()

    panels = (
        render_feed_panel(feed)
        + render_targets_panel(targets)
        + render_subdomains_panel(subs)
        + render_assets_panel(assets, targets)
        + render_requests_panel(summary)
        + render_activity_panel(paths, eng, rate, pend_by_dom, runs)
        + render_rate_panel(rate, pend_by_dom)
    )
    stats = _header_stats(targets, subs, assets, summary, eng)
    alive = bool(eng.get("active") or eng.get("running"))

    html = PAGE
    html = html.replace("__PANELS__", panels)
    html = html.replace("__STATS__", stats)
    html = html.replace("__REFRESH__", str(REFRESH_SECONDS))
    html = html.replace("__GENEPOCH__", str(int(time.time())))
    html = html.replace("__STALE__", str(STALE_AFTER))
    html = html.replace("__ENGINEALIVE__", "true" if alive else "false")
    html = html.replace("__STATUSES__", json.dumps(list(TARGET_STATUSES)))
    html = html.replace("__ASSETSVER__", json.dumps(assets_version(assets)))
    html = html.replace("__REQVER__", json.dumps(requests_version(summary)))
    return html


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
def make_handler(paths: Paths):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
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
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) if n else b""
                return json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                return None

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", render_page(paths))
                return
            parts = [p for p in path.split("/") if p]
            if parts[:2] == ["api", "engine"]:
                self._json(200, engine_status())
                return
            if parts[:2] == ["api", "asset"]:
                rel = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                data, ctype = read_asset(unquote(rel))
                if data is None:
                    self._json(404, {"errors": ["asset not found"]})
                else:
                    # Force text rendering in-browser for everything (recon view).
                    safe = "text/plain; charset=utf-8"
                    if ctype and ctype.split("/")[0] in ("image", "application"):
                        safe = ctype
                    self._send(200, safe, data)
                return
            if parts[:2] == ["api", "run"] and len(parts) >= 3:
                detail = get_run_detail(paths.state_dir, paths.logs_dir,
                                        unquote(parts[2]))
                self._json(200 if detail else 404,
                           detail or {"errors": ["run not found"]})
                return
            if parts[:2] == ["api", "targets"]:
                self._json(200, {"items": load_targets()})
                return
            if parts[:2] == ["api", "assets"]:
                assets = load_assets()
                self._json(200, {"version": assets_version(assets),
                                 "rows": asset_rows_json(assets)})
                return
            if parts[:2] == ["api", "requests"]:
                summary = load_request_summary()
                completed = load_recent_completed(limit=5000)
                self._json(200, {"version": requests_version(summary),
                                 "rows": request_rows_json(completed)})
                return
            self._json(404, {"errors": ["not found"]})

        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts[:2] == ["api", "engine"]:
                from . import service
                data = self._body() or {}
                ok, msg = service.control(data.get("action", ""))
                out = {"ok": ok, "message": msg}
                out.update(engine_status())
                self._json(200 if ok else 400, out)
                return
            if parts[:2] == ["api", "targets"]:
                data = self._body()
                if data is None:
                    self._json(400, {"ok": False, "errors": ["invalid JSON body"]})
                    return
                # /api/targets/<domain>/probe  → re-drop queue trigger
                if len(parts) >= 4 and parts[3] == "probe":
                    code, res = reprobe_target(unquote(parts[2]))
                else:
                    code, res = add_target(data)
                self._json(code, res)
                return
            self._json(404, {"errors": ["not found"]})

        def do_PUT(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts[:2] == ["api", "targets"] and len(parts) >= 3:
                data = self._body()
                if data is None:
                    self._json(400, {"ok": False, "errors": ["invalid JSON body"]})
                    return
                code, res = edit_target(unquote(parts[2]), data)
                self._json(code, res)
                return
            self._json(404, {"errors": ["not found"]})

        def do_DELETE(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts[:2] == ["api", "targets"] and len(parts) >= 3:
                code, res = delete_target(unquote(parts[2]))
                self._json(code, res)
                return
            self._json(404, {"errors": ["not found"]})

    return Handler


def serve(paths: Paths, host="127.0.0.1", port=8766, quiet=False):
    httpd = ThreadingHTTPServer((host, port), make_handler(paths))
    if not quiet:
        print(f"bugbounty dashboard serving at http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if not quiet:
            print("\nbugbounty dashboard stopped")
    finally:
        httpd.server_close()


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="agentC bugbounty web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--root", default=_root())
    args = p.parse_args(argv)
    os.environ.setdefault("AGENTC_ROOT", args.root)
    serve(Paths(args.root), host=args.host, port=args.port)


# --------------------------------------------------------------------------- #
# Page template — CSS is lifted verbatim from the engine dashboard so the look
# and feel match; the body/JS are bugbounty-specific.
# --------------------------------------------------------------------------- #
def _dashboard_css() -> str:
    from .dashboard import PAGE as _P
    return _P.split("<style>", 1)[1].split("</style>", 1)[0]


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>agentC · bugbounty</title>
<style>__CSS__
  /* bugbounty-specific tweaks */
  .panel { width: calc(50% - 5px); height: calc((100vh - 38px)/2 - 3px); }
  #panel-feed { width: 100%; height: calc((100vh - 38px)/2 - 3px); }
  #panel-feed table.dt td:nth-child(4) { white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; max-width: 0; width: 100%; }
  header .hi b { color: #e6edf3; }
  .frow .hint { color:#6e7681; font-size:10px; text-transform:none; letter-spacing:0; }
</style>
</head>
<body>
<header>
  <span class="brand">agentC</span>
  <span class="hi mode">bugbounty</span>
  __STATS__
  <span class="spacer"></span>
  <span class="engctl" id="engctl">
    <button class="eng eon" id="eng-start" title="start engine">start</button>
    <button class="eng eoff" id="eng-stop" title="stop engine">stop</button>
    <button class="eng" id="eng-restart" title="restart engine">restart</button>
  </span>
  <label class="pause"><input type="checkbox" id="pause"> pause</label>
  <span class="hi">upd <span id="ago">0s</span></span>
  <button id="panelsbtn" title="show / hide / reorder panels">&#9776; panels</button>
  <button id="reload" title="reload now">&#x21bb;</button>
</header>
<div class="grid">__PANELS__</div>

<!-- target add/edit modal -->
<div class="overlay" id="overlay">
  <div class="modal">
    <h3 id="mtitle">Add target</h3>
    <div class="mbody">
      <div class="errs" id="merrs"></div>
      <form id="mform">
        <div class="frow"><label for="f_domain">domain *</label>
          <input type="text" id="f_domain" placeholder="example.com" spellcheck="false" autocomplete="off">
          <span class="hint">root domain; adding drops it into the queue and kicks off spidering + critical-path probes</span></div>
        <div class="frow"><label for="f_program">program</label>
          <input type="text" id="f_program" placeholder="HackerOne / Bugcrowd program name" spellcheck="false"></div>
        <div class="frow"><label for="f_status">status</label>
          <select id="f_status"></select></div>
        <div class="frow"><label for="f_tags">tags</label>
          <input type="text" id="f_tags" placeholder="comma,separated,tags" spellcheck="false"></div>
        <div class="frow"><label for="f_scope_in">in scope</label>
          <textarea id="f_scope_in" class="mono" placeholder="one host/pattern per line"></textarea></div>
        <div class="frow"><label for="f_scope_out">out of scope</label>
          <textarea id="f_scope_out" class="mono" placeholder="one host/pattern per line"></textarea></div>
        <div class="frow"><label for="f_notes">notes</label>
          <textarea id="f_notes" placeholder="freeform notes"></textarea></div>
      </form>
    </div>
    <div class="mfoot">
      <button class="btn-cancel" id="mcancel">Cancel</button>
      <button class="btn-save" id="msave">Save</button>
    </div>
  </div>
</div>

<!-- confirm -->
<div class="overlay" id="coverlay">
  <div class="modal" style="width:420px">
    <h3>Confirm</h3>
    <div class="mbody" id="cmsg"></div>
    <div class="mfoot">
      <button class="btn-cancel" id="cno">Cancel</button>
      <button class="btn-danger" id="cyes">Delete</button>
    </div>
  </div>
</div>

<!-- panels config -->
<div class="overlay" id="poverlay">
  <div class="modal" style="width:340px">
    <h3>Panels &mdash; show / hide / reorder</h3>
    <div class="mbody" id="plist"></div>
    <div class="mfoot">
      <button class="btn-cancel" id="preset">Reset to default</button>
      <span style="flex:1"></span>
      <button class="btn-cancel" id="pclose">Close</button>
    </div>
  </div>
</div>

<!-- run detail -->
<div class="overlay" id="roverlay">
  <div class="modal wide">
    <h3 id="rdtitle">Run detail</h3>
    <div class="mbody" id="rdbody">loading…</div>
    <div class="mfoot"><button class="btn-cancel" id="rdclose">Close</button></div>
  </div>
</div>

<!-- asset viewer -->
<div class="overlay" id="aoverlay">
  <div class="modal wide">
    <h3 id="adtitle">Asset</h3>
    <div class="mbody"><pre class="rd-out" id="adbody" style="max-height:70vh">loading…</pre></div>
    <div class="mfoot">
      <a class="btn-cancel" id="adraw" target="_blank" style="text-decoration:none">Open raw</a>
      <button class="btn-cancel" id="adclose">Close</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
var REFRESH=__REFRESH__, GEN=__GENEPOCH__, STALE=__STALE__, ENGINE_ALIVE=__ENGINEALIVE__;
var STATUSES=__STATUSES__;
var modalOpen=false, confirmOpen=false, dragging=false, EMODE='add', EDOMAIN='';

function S(k,v){ try{ localStorage.setItem('agentcbb:'+k, JSON.stringify(v)); }catch(e){} }
function L(k){ try{ var v=localStorage.getItem('agentcbb:'+k); return v?JSON.parse(v):null; }catch(e){ return null; } }
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

/* ---- assets: domain/subdomain scope + search (composed) ---- */
(function(){
  var tbl=document.getElementById('tbl-assets'); if(!tbl) return;
  var sel=document.getElementById('assetsel'), q=document.getElementById('assetq');
  function apply(){
    var scope=sel?sel.value:'all', txt=(q&&q.value||'').toLowerCase();
    var tp=scope.split('||')[0], hp=scope.split('||')[1];
    var tb=tbl.tBodies[0]; if(!tb) return;
    [].forEach.call(tb.rows, function(r){
      if(r.classList.contains('empty')) return;
      var okScope = (scope==='all') ||
        (r.getAttribute('data-target')===tp && (hp==='*' || r.getAttribute('data-host')===hp));
      var okTxt = !txt || r.textContent.toLowerCase().indexOf(txt)>=0;
      r.style.display=(okScope&&okTxt)?'':'none';
    });
  }
  // restore saved scope/search
  var sv=L('assetscope'); if(sv && sel){ for(var i=0;i<sel.options.length;i++){ if(sel.options[i].value===sv) sel.value=sv; } }
  var sq=L('assetq'); if(sq && q){ q.value=sq; }
  if(sel){ sel.addEventListener('change', function(){ S('assetscope', sel.value); apply(); });
    sel.addEventListener('dblclick', function(ev){ ev.stopPropagation(); }); }
  if(q){ q.addEventListener('input', function(){ S('assetq', q.value); apply(); });
    q.addEventListener('dblclick', function(ev){ ev.stopPropagation(); }); }
  window.scopeAssets=function(target, host){
    if(!sel) return; var v=target+'||'+host;
    var found=false; for(var i=0;i<sel.options.length;i++){ if(sel.options[i].value===v){ found=true; } }
    sel.value=found?v:(target+'||*'); S('assetscope', sel.value); apply();
    var p=document.getElementById('panel-assets'); if(p) p.scrollIntoView({behavior:'smooth', block:'nearest'});
  };
  apply();
})();

/* ---- resize + collapse + reorder panels ---- */
var panels=[].slice.call(document.querySelectorAll('.panel'));
panels.forEach(function(p){
  var sz=L('size:'+p.id); if(sz){ if(sz.w) p.style.width=sz.w; if(sz.h) p.style.height=sz.h; }
  if(L('collapsed:'+p.id)) p.classList.add('collapsed');
  var ph=p.querySelector('.phead');
  ph.addEventListener('dblclick', function(){
    p.classList.toggle('collapsed'); S('collapsed:'+p.id, p.classList.contains('collapsed'));
  });
});
var preSize={};
document.addEventListener('mousedown', function(){ dragging=true; if(window._t) clearTimeout(window._t);
  panels.forEach(function(p){ preSize[p.id]=p.offsetWidth+'x'+p.offsetHeight; }); });
document.addEventListener('mouseup', function(){ panels.forEach(function(p){
  if(p.classList.contains('collapsed') || p.offsetWidth===0) return;
  var cur=p.offsetWidth+'x'+p.offsetHeight;
  if(preSize[p.id] && preSize[p.id]!==cur){ S('size:'+p.id, {w:p.offsetWidth+'px', h:p.offsetHeight+'px'}); }
}); dragging=false; schedule(); });

function panelKeys(){ return panels.map(function(p){ return p.id.replace('panel-',''); }); }
function panelTitle(k){ var p=document.getElementById('panel-'+k); var t=p&&p.querySelector('.ptitle'); return t?t.textContent:k; }
function defaultCfg(){ return {order: panelKeys(), hidden: []}; }
function loadCfg(){
  var c=L('panelcfg'); if(!c||!c.order) c=defaultCfg(); c.hidden=c.hidden||[];
  panelKeys().forEach(function(k){ if(c.order.indexOf(k)<0) c.order.push(k); });
  c.order=c.order.filter(function(k){ return document.getElementById('panel-'+k); });
  return c;
}
function applyCfg(c){
  var grid=document.querySelector('.grid');
  c.order.forEach(function(k,i){
    var p=document.getElementById('panel-'+k); if(!p) return;
    p.style.order=i; p.style.display=(c.hidden.indexOf(k)>=0)?'none':''; grid.appendChild(p);
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
      +'<span class="pmove"><button data-dir="up">&#9650;</button><button data-dir="down">&#9660;</button></span>';
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
document.getElementById('panelsbtn').addEventListener('click', function(){ renderPlist(); poverlay.style.display='flex'; modalOpen=true; schedule(); });
document.getElementById('pclose').addEventListener('click', closePanelsDlg);
document.getElementById('preset').addEventListener('click', function(){
  panels.forEach(function(p){ p.style.width=''; p.style.height=''; p.classList.remove('collapsed');
    try{ localStorage.removeItem('agentcbb:size:'+p.id); localStorage.removeItem('agentcbb:collapsed:'+p.id); }catch(e){} });
  pcfg=defaultCfg(); saveCfg(); applyCfg(pcfg); renderPlist();
});
poverlay.addEventListener('mousedown', function(ev){ if(ev.target===poverlay) closePanelsDlg(); });

/* ---- target add/edit modal ---- */
var overlay=document.getElementById('overlay'), merrs=document.getElementById('merrs'),
    mtitle=document.getElementById('mtitle');
(function(){ var sel=document.getElementById('f_status');
  STATUSES.forEach(function(s){ var o=document.createElement('option'); o.value=s; o.textContent=s; sel.appendChild(o); }); })();
function F(id){ return document.getElementById(id); }
function showErrors(list){ merrs.textContent=(list||[]).join('\n'); merrs.style.display=(list&&list.length)?'block':'none'; }
function openModal(){ overlay.style.display='flex'; modalOpen=true; schedule(); }
function closeModal(){ overlay.style.display='none'; modalOpen=false; schedule(); }
function openAdd(){
  EMODE='add'; EDOMAIN=''; showErrors([]); mtitle.textContent='Add target';
  F('f_domain').value=''; F('f_domain').removeAttribute('readonly');
  F('f_program').value=''; F('f_status').value='active'; F('f_tags').value='';
  F('f_scope_in').value=''; F('f_scope_out').value=''; F('f_notes').value='';
  openModal(); setTimeout(function(){ F('f_domain').focus(); }, 30);
}
function openEdit(domain){
  EMODE='edit'; EDOMAIN=domain; showErrors([]); mtitle.textContent='Edit '+domain;
  fetch('/api/targets').then(function(r){ return r.json(); }).then(function(d){
    var t=(d.items||[]).filter(function(x){ return x.domain===domain; })[0]||{};
    F('f_domain').value=domain; F('f_domain').setAttribute('readonly','');
    F('f_program').value=t.program||''; F('f_status').value=t.status||'active';
    F('f_tags').value=(t.tags||[]).join(', ');
    F('f_scope_in').value=(t.scope_in||[]).join('\n');
    F('f_scope_out').value=(t.scope_out||[]).join('\n');
    F('f_notes').value=t.notes||''; openModal();
  });
}
function submitTarget(){
  var domain=F('f_domain').value.trim();
  if(!domain){ showErrors(['domain is required']); return; }
  var payload={ domain:domain, program:F('f_program').value, status:F('f_status').value,
    tags:F('f_tags').value, scope_in:F('f_scope_in').value, scope_out:F('f_scope_out').value,
    notes:F('f_notes').value };
  var url=(EMODE==='edit')?'/api/targets/'+encodeURIComponent(EDOMAIN):'/api/targets';
  fetch(url, {method:(EMODE==='edit'?'PUT':'POST'), headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)})
    .then(function(r){ return r.json(); })
    .then(function(res){
      if(res.ok){ closeModal();
        toast(EMODE==='edit'?'Saved '+res.domain:'Added '+res.domain+' — spidering started');
        setTimeout(function(){ location.reload(); }, 700); }
      else { showErrors(res.errors||['save failed']); }
    }).catch(function(e){ showErrors([String(e)]); });
}
document.getElementById('msave').addEventListener('click', submitTarget);
document.getElementById('mcancel').addEventListener('click', closeModal);
overlay.addEventListener('mousedown', function(ev){ if(ev.target===overlay) closeModal(); });

/* ---- confirm ---- */
var coverlay=document.getElementById('coverlay'), cmsg=document.getElementById('cmsg'), _onYes=null;
function openConfirm(msg, onYes){ cmsg.textContent=msg; _onYes=onYes; coverlay.style.display='flex'; confirmOpen=true; schedule(); }
function closeConfirm(){ coverlay.style.display='none'; confirmOpen=false; _onYes=null; schedule(); }
document.getElementById('cno').addEventListener('click', closeConfirm);
document.getElementById('cyes').addEventListener('click', function(){ var f=_onYes; closeConfirm(); if(f) f(); });
coverlay.addEventListener('mousedown', function(ev){ if(ev.target===coverlay) closeConfirm(); });
function delTarget(domain){
  openConfirm('Delete target "'+domain+'"? This permanently removes its directory, assets and state.', function(){
    fetch('/api/targets/'+encodeURIComponent(domain), {method:'DELETE'}).then(function(r){ return r.json(); })
      .then(function(res){ if(res.ok){ toast('Deleted '+domain); setTimeout(function(){ location.reload(); }, 500); }
        else { toast('Delete failed'); } });
  });
}
function reprobe(domain){
  fetch('/api/targets/'+encodeURIComponent(domain)+'/probe', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
    .then(function(r){ return r.json(); })
    .then(function(res){ if(res.ok){ toast('Re-queued '+domain+' for probing'); setTimeout(function(){ location.reload(); }, 700); }
      else { toast('Re-probe failed'); } });
}

/* ---- button + row delegation ---- */
document.addEventListener('click', function(ev){
  var b=ev.target.closest('[data-act]');
  if(b){
    var act=b.getAttribute('data-act'), domain=b.getAttribute('data-domain');
    if(act==='add-target') openAdd();
    else if(act==='edit') openEdit(domain);
    else if(act==='del') delTarget(domain);
    else if(act==='probe') reprobe(domain);
    return;
  }
  var al=ev.target.closest('a.olink[data-asset]');
  if(al){ ev.preventDefault(); openAsset(al.getAttribute('data-asset')); return; }
  var tr=ev.target.closest('tbody tr');
  if(!tr) return;
  if(tr.getAttribute('data-kind')==='run'){ openRunDetail(tr.getAttribute('data-run')); }
  else if(tr.getAttribute('data-kind')==='sub' && window.scopeAssets){
    scopeAssets(tr.getAttribute('data-target'), tr.getAttribute('data-host')); }
});

/* ---- live runtime ticking ---- */
function tickRuntimes(){
  var now=Date.now()/1000;
  [].forEach.call(document.querySelectorAll('.runtime[data-since]'), function(el){
    var since=parseFloat(el.getAttribute('data-since'))||0; if(!since){ el.textContent='—'; return; }
    var s=Math.max(0, Math.floor(now-since));
    if(s<60) el.textContent=s+'s'; else if(s<3600) el.textContent=Math.floor(s/60)+'m'+String(s%60).padStart(2,'0')+'s';
    else el.textContent=Math.floor(s/3600)+'h'+String(Math.floor(s/60)%60).padStart(2,'0')+'m';
  });
}
tickRuntimes(); setInterval(tickRuntimes, 1000);

/* ---- run detail ---- */
var roverlay=document.getElementById('roverlay'), rdbody=document.getElementById('rdbody'), rdtitle=document.getElementById('rdtitle');
function escd(s){ return esc(s==null?'':s); }
function openRunDetail(id){
  if(!id) return; modalOpen=true; schedule(); rdtitle.textContent='Run '+id;
  rdbody.innerHTML='loading…'; roverlay.style.display='flex';
  fetch('/api/run/'+encodeURIComponent(id)).then(function(r){ return r.json(); })
    .then(function(d){ rdbody.innerHTML=renderRunDetail(d); })
    .catch(function(e){ rdbody.innerHTML='<div class="rd-empty">failed: '+escd(e)+'</div>'; });
}
function renderRunDetail(d){
  if(!d || d.errors){ return '<div class="rd-empty">run not found</div>'; }
  var dur=(d.finished&&d.started)?(d.finished-d.started).toFixed(2)+'s':'(running)';
  var h='<div class="rd-meta"><span>task <b>'+escd(d.task)+'</b></span><span>status <b>'+escd(d.status)+'</b></span>'
    +'<span>trigger <b>'+escd(d.trigger)+'</b></span><span>duration <b>'+dur+'</b></span><span>id <b>'+escd(d.id)+'</b></span>'
    +(d.error?'<span>error <b style="color:#f85149">'+escd(d.error)+'</b></span>':'')+'</div>';
  h+='<div class="rd-sec">Actions ('+((d.results||[]).length)+')</div>';
  if((d.results||[]).length){ d.results.forEach(function(a){
    h+='<div class="rd-action"><div class="rh"><span class="rn">'+escd(a.name)+'</span>'
      +'<span class="badge '+(a.success?'ok':'bad')+'">'+(a.success?'ok':'fail')+'</span>'
      +'<span style="color:#6e7681">'+escd(a.type)+' · '+(a.duration||0).toFixed(2)+'s</span></div>';
    if(a.stdout) h+='<div class="rd-out">'+escd(a.stdout)+'</div>';
    if(a.stderr) h+='<div class="rd-out err">'+escd(a.stderr)+'</div>';
    if(a.error) h+='<div class="rd-out err">'+escd(a.error)+'</div>'; h+='</div>'; });
  } else { h+='<div class="rd-empty">no actions recorded</div>'; }
  var vk=Object.keys(d.variables||{});
  if(vk.length){ h+='<div class="rd-sec">Variables</div><div class="rd-out">'+escd(JSON.stringify(d.variables,null,2))+'</div>'; }
  return h;
}
function closeRunDetail(){ roverlay.style.display='none'; modalOpen=false; schedule(); }
document.getElementById('rdclose').addEventListener('click', closeRunDetail);
roverlay.addEventListener('mousedown', function(ev){ if(ev.target===roverlay) closeRunDetail(); });

/* ---- asset viewer ---- */
var aoverlay=document.getElementById('aoverlay'), adbody=document.getElementById('adbody'),
    adtitle=document.getElementById('adtitle'), adraw=document.getElementById('adraw');
function openAsset(rel){
  modalOpen=true; schedule(); adtitle.textContent=rel.split('/').pop();
  var u='/api/asset?path='+encodeURIComponent(rel); adraw.href=u;
  adbody.textContent='loading…'; aoverlay.style.display='flex';
  fetch(u).then(function(r){ return r.text(); })
    .then(function(t){ adbody.textContent=t.slice(0, 200000); })
    .catch(function(e){ adbody.textContent='failed: '+e; });
}
function closeAsset(){ aoverlay.style.display='none'; modalOpen=false; schedule(); }
document.getElementById('adclose').addEventListener('click', closeAsset);
aoverlay.addEventListener('mousedown', function(ev){ if(ev.target===aoverlay) closeAsset(); });

/* ---- toast ---- */
var toastEl=document.getElementById('toast'), _tt;
function toast(msg){ toastEl.textContent=msg; toastEl.classList.add('show'); clearTimeout(_tt); _tt=setTimeout(function(){ toastEl.classList.remove('show'); }, 2600); }

/* ---- auto-refresh ---- */
var pause=document.getElementById('pause'); pause.checked=!!L('paused');
function schedule(){ if(window._t) clearTimeout(window._t);
  if(pause.checked || modalOpen || confirmOpen || dragging) return;
  window._t=setTimeout(function(){ location.reload(); }, REFRESH*1000); }
pause.addEventListener('change', function(){ S('paused', pause.checked); schedule(); });
document.getElementById('reload').addEventListener('click', function(){ location.reload(); });

/* ---- engine on/off ---- */
(function(){
  var bStart=document.getElementById('eng-start'), bStop=document.getElementById('eng-stop'), bRestart=document.getElementById('eng-restart');
  bStart.style.display=ENGINE_ALIVE?'none':''; bStop.style.display=ENGINE_ALIVE?'':'none'; bRestart.style.display=ENGINE_ALIVE?'':'none';
  function ctl(action, btn){
    [bStart,bStop,bRestart].forEach(function(b){ b.disabled=true; }); var old=btn.textContent; btn.textContent='…';
    fetch('/api/engine', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:action})})
      .then(function(r){ return r.json(); })
      .then(function(res){ toast(res.ok?('Engine '+action+'ed'):('Engine '+action+' failed: '+(res.message||'error')));
        setTimeout(function(){ location.reload(); }, action==='stop'?900:1800); })
      .catch(function(e){ toast('Engine '+action+' error: '+e); [bStart,bStop,bRestart].forEach(function(b){ b.disabled=false; }); btn.textContent=old; });
  }
  bStart.addEventListener('click', function(){ ctl('start', bStart); });
  bStop.addEventListener('click', function(){ ctl('stop', bStop); });
  bRestart.addEventListener('click', function(){ ctl('restart', bRestart); });
})();

document.addEventListener('keydown', function(ev){ if(ev.key==='Escape'){
  if(confirmOpen) closeConfirm();
  else if(aoverlay.style.display==='flex') closeAsset();
  else if(roverlay.style.display==='flex') closeRunDetail();
  else if(poverlay.style.display==='flex') closePanelsDlg();
  else if(modalOpen) closeModal();
} });
function tick(){ var age=Math.floor(Date.now()/1000)-GEN; var a=document.getElementById('ago'); if(a) a.textContent=age+'s'+(age>STALE?' STALE':''); }
tick(); setInterval(tick, 1000); schedule();
</script>
</body>
</html>
"""

PAGE = PAGE.replace("__CSS__", _dashboard_css())


if __name__ == "__main__":
    main()
