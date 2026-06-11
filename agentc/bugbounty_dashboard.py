"""Bugbounty web UI — a focused control plane for the recon pipeline.
"""
from __future__ import annotations
import glob
import json
import os
import re
import shutil
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from .dashboard import (
    Paths, e, fmt_size, fmt_ts, fmt_dur, badge, table, panel,
    get_run_detail, load_runs, _load_json,
)
REFRESH_SECONDS = 5
STALE_AFTER = 12
MAX_ROWS = 600
ASSET_TYPES = ("html", "scripts", "stylesheets", "images", "archives", "bin", "critical")
TARGET_STATUSES = ("active", "paused", "archived")
def _root() -> str: return os.environ.get("AGENTC_ROOT") or os.getcwd()
def bb_dir(*parts) -> str: return os.path.join(_root(), "bugbounty", *parts)
def targets_dir() -> str: return bb_dir("targets")
def requests_dir() -> str: return bb_dir("requests")
def normalize_domain(raw: str) -> str:
    d = (raw or "").strip()
    if d.startswith("http://"): d = d[7:]
    if d.startswith("https://"): d = d[8:]
    return d.rstrip("/").split("/")[0]
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
def valid_domain(d: str) -> bool: return bool(d) and "." in d and bool(_DOMAIN_RE.match(d)) and ".." not in d
def _meta_defaults(domain: str) -> dict: return {"program": "", "status": "active", "tags": [], "scope_in": [], "scope_out": [], "notes": ""}
def load_meta(domain: str) -> dict:
    meta = _meta_defaults(domain); got = _load_json(os.path.join(targets_dir(), domain, "meta.json"))
    if isinstance(got, dict): meta.update({k: got.get(k, meta[k]) for k in meta})
    return meta
def _hostnames_for(target_path: str) -> list:
    out = []
    try:
        for name in os.listdir(target_path):
            if os.path.isdir(os.path.join(target_path, name)) and os.path.isdir(os.path.join(target_path, name, "assets")): out.append(name)
    except: pass
    return sorted(out)
def _asset_counts(target_path: str) -> dict:
    counts = {t: 0 for t in ASSET_TYPES}; total = 0
    for body in glob.glob(os.path.join(target_path, "*", "assets", "*", "*.body")):
        total += 1; atype = os.path.basename(os.path.dirname(body))
        if atype in counts: counts[atype] += 1
    counts["total"] = total; return counts
def load_targets() -> list:
    out = []; base = targets_dir()
    try: entries = sorted(os.listdir(base))
    except: return out
    for name in entries:
        if name == "queue" or not os.path.isdir(os.path.join(base, name)): continue
        state = _load_json(os.path.join(base, name, "state.json")) or {}
        meta = load_meta(name); hostnames = _hostnames_for(os.path.join(base, name)); assets = _asset_counts(os.path.join(base, name))
        out.append({"domain": name, "program": meta["program"], "status": meta["status"], "tags": meta["tags"], "scope_in": meta["scope_in"], "scope_out": meta["scope_out"], "notes": meta["notes"], "subdomains": hostnames, "n_sub": len(hostnames), "assets": assets, "n_assets": assets["total"], "requested": len(state.get("requested_urls", []) or []), "discovered": len(state.get("discovered_urls", []) or []), "created_at": state.get("created_at", "")})
    return out
def load_subdomains(targets: list, filter_target: str = None) -> list:
    rows = []; base = targets_dir()
    for t in targets:
        if filter_target and t["domain"] != filter_target:
            continue
        for host in t["subdomains"]:
            hpath = os.path.join(base, t["domain"], host); counts = {at: 0 for at in ASSET_TYPES}; total = 0; last = 0.0
            for body in glob.glob(os.path.join(hpath, "assets", "*", "*.body")):
                total += 1; at = os.path.basename(os.path.dirname(body))
                if at in counts: counts[at] += 1
                try: last = max(last, os.path.getmtime(body))
                except: pass
            rows.append({"target": t["domain"], "hostname": host, "n_assets": total, "counts": counts, "last": last})
    return rows
def load_assets() -> list:
    rows = []
    for body in glob.glob(os.path.join(targets_dir(), "*", "*", "assets", "*", "*.body")):
        parts = body.split(os.sep); idx = parts.index("targets"); target = parts[idx + 1]; hostname = parts[idx + 2]; atype = os.path.basename(os.path.dirname(body)); name = os.path.basename(body)[:-5]; meta = _load_json(body[:-5] + ".json") or {}
        try: size = os.path.getsize(body); mtime = os.path.getmtime(body)
        except: size, mtime = 0, 0
        rows.append({"target": target, "hostname": hostname, "type": atype, "name": name, "url": meta.get("url", ""), "status": meta.get("status_code", ""), "content_type": (meta.get("content_type", "") or "").split(";")[0], "size": size, "fetched": meta.get("requested_at", "") or mtime, "mtime": mtime, "path": body})
    rows.sort(key=lambda r: r["mtime"], reverse=True); return rows
def _ts(val):
    if not val: return 0
    if isinstance(val, (int, float)): return val
    try: return time.mktime(time.strptime(val.split(".")[0], "%Y-%m-%dT%H:%M:%S"))
    except: return 0
def assets_version(assets: list) -> str:
    mx = max((a["mtime"] for a in assets), default=0); return f"{len(assets)}:{int(mx)}"
def asset_rows_json(assets: list) -> list:
    return [[a["target"], a["hostname"], a["type"], a["name"], a["status"], a["content_type"], a["size"], a["url"], _ts(a["fetched"]), os.path.relpath(a["path"], _root())] for a in assets]
def load_request_summary() -> dict:
    out = {"pending": 0, "ready": 0, "completed": 0, "by_status": {}}
    try: out["pending"] = len(os.listdir(bb_dir("requests", "pending")))
    except: pass
    try: out["ready"] = len(os.listdir(bb_dir("requests", "ready")))
    except: pass
    try:
        comp = bb_dir("requests", "completed")
        for s in os.listdir(comp):
            if s == "other": continue
            n = len(os.listdir(os.path.join(comp, s))); out["by_status"][s] = n; out["completed"] += n
    except: pass
    return out
def load_recent_completed(limit=1000) -> list:
    rows = []; comp = bb_dir("requests", "completed")
    try:
        for s in os.listdir(comp):
            d = os.path.join(comp, s)
            for f in os.listdir(d): rows.append({"path": os.path.join(d, f), "status": s, "mtime": os.path.getmtime(os.path.join(d, f))})
    except: pass
    rows.sort(key=lambda x: x["mtime"], reverse=True); out = []
    for r in rows[:limit]:
        data = _load_json(r["path"]) or {}; req = data.get("request") or {}; resp = data.get("response") or {}
        out.append({"id": req.get("id", ""), "url": req.get("url", ""), "domain": req.get("domain", ""), "status": resp.get("status_code", r["status"]), "mtime": r["mtime"], "error": resp.get("error", "")})
    return out
def requests_version(summary: dict) -> str: return f"{summary['pending']}:{summary['ready']}:{summary['completed']}"
def request_rows_json(completed: list) -> list: return [[r["id"], r["domain"], r["url"], r["status"], r["mtime"], r["error"]] for r in completed]
def load_rate_state() -> dict: return _load_json(bb_dir("requests", "rate_state.json")) or {}
def load_activity_feed(limit=100) -> list:
    entries = []; runs = load_runs(Paths(_root()).state_dir)
    for r in runs:
        t = r.get("finished") or r.get("started") or 0; task = r.get("task", "unknown"); status = r.get("status", "unknown")
        entries.append({"time": t, "task": task, "status": status, "text": f"Run {r['id']} {status}"})
    entries.sort(key=lambda x: x["time"], reverse=True); return entries[:limit]
def engine_status() -> dict:
    try: from . import service; return service.status()
    except: return {}
def _write_meta(domain: str, data: dict) -> None:
    tpath = os.path.join(targets_dir(), domain); os.makedirs(tpath, exist_ok=True); meta = _meta_defaults(domain); meta.update({k: data.get(k, meta[k]) for k in meta})
    meta["tags"] = _as_list(data.get("tags", meta["tags"]), sep=","); meta["scope_in"] = _as_list(data.get("scope_in", meta["scope_in"])); meta["scope_out"] = _as_list(data.get("scope_out", meta["scope_out"]))
    if meta["status"] not in TARGET_STATUSES: meta["status"] = "active"
    with open(os.path.join(tpath, "meta.json"), "w", encoding="utf-8") as fh: json.dump(meta, fh, indent=2)
def _as_list(val, sep="\n") -> list:
    if isinstance(val, list): return [str(x).strip() for x in val if str(x).strip()]
    return [x.strip() for x in str(val or "").replace("\r", "").split(sep) if x.strip()]
def _drop_queue(domain: str) -> None: q = os.path.join(targets_dir(), "queue"); os.makedirs(q, exist_ok=True); open(os.path.join(q, domain), "w").close()
def add_target(data: dict):
    domain = normalize_domain(data.get("domain", ""));
    if not valid_domain(domain): return 400, {"ok": False, "errors": ["invalid domain"]}
    if os.path.isdir(os.path.join(targets_dir(), domain)): return 409, {"ok": False, "errors": [f"target {domain} already exists"]}
    _write_meta(domain, data); _drop_queue(domain); return 200, {"ok": True, "domain": domain}
def edit_target(domain: str, data: dict):
    domain = normalize_domain(domain);
    if not os.path.isdir(os.path.join(targets_dir(), domain)): return 404, {"ok": False, "errors": ["target not found"]}
    _write_meta(domain, data); return 200, {"ok": True, "domain": domain}
def delete_target(domain: str):
    domain = normalize_domain(domain); tpath = os.path.join(targets_dir(), domain);
    if not os.path.isdir(tpath): return 404, {"ok": False, "errors": ["target not found"]}
    shutil.rmtree(tpath, ignore_errors=True); rs_path = os.path.join(requests_dir(), "rate_state.json"); rs = load_rate_state()
    if domain in rs:
        rs.pop(domain, None)
        try:
            with open(rs_path, "w", encoding="utf-8") as fh: json.dump(rs, fh, indent=2)
        except: pass
    qf = os.path.join(targets_dir(), "queue", domain)
    if os.path.exists(qf): os.remove(qf)
    return 200, {"ok": True, "domain": domain}
def reprobe_target(domain: str):
    domain = normalize_domain(domain);
    if not os.path.isdir(os.path.join(targets_dir(), domain)): return 404, {"ok": False, "errors": ["target not found"]}
    _drop_queue(domain); return 200, {"ok": True, "domain": domain}
def run_task_for_target(domain: str, task: str):
    domain = normalize_domain(domain)
    if not os.path.isdir(os.path.join(targets_dir(), domain)): return 404, {"ok": False, "errors": ["target not found"]}
    if task == "subfinder":
        import subprocess
        try:
            subprocess.Popen([sys.executable, "bugbounty/scripts/trigger_subfinder.py", domain], cwd=_root())
            return 200, {"ok": True, "domain": domain, "message": "subfinder triggered"}
        except Exception as e: return 500, {"ok": False, "errors": [str(e)]}
    return 400, {"ok": False, "errors": ["unknown task"]}
def run_task_for_sub(domain: str, host: str, task: str):
    domain = normalize_domain(domain); host = normalize_domain(host)
    import subprocess
    try:
        subprocess.Popen([sys.executable, "bugbounty/scripts/trigger_sub_task.py", task, host, domain], cwd=_root())
        return 200, {"ok": True, "domain": domain, "host": host, "message": f"{task} triggered"}
    except Exception as e: return 500, {"ok": False, "errors": [str(e)]}
def open_explorer(domain: str, host: str):
    import subprocess; target_path = os.path.join(targets_dir(), domain, host)
    try:
        if os.name == "nt": subprocess.Popen(["explorer", target_path])
        elif sys.platform == "darwin": subprocess.Popen(["open", target_path])
        else: subprocess.Popen(["xdg-open", target_path])
        return 200, {"ok": True}
    except Exception as e: return 500, {"ok": False, "errors": [str(e)]}
def read_asset(rel: str):
    base = os.path.realpath(targets_dir()); full = os.path.realpath(os.path.join(_root(), rel))
    if not full.startswith(base + os.sep): return None, None
    try:
        with open(full, "rb") as fh: data = fh.read()
    except OSError: return None, None
    meta = _load_json(full[:-5] + ".json") if full.endswith(".body") else {}
    return data, meta.get("content_type") or "text/plain; charset=utf-8"
def _tag_chips(tags) -> str: return " ".join(f'<span class="badge mut">{e(t)}</span>' for t in tags) or "—"
def _status_badge(status) -> str: return badge(status or "?", {"active": "ok", "paused": "warn", "archived": "mut"}.get(status, "mut"))
def _http_badge(code) -> str:
    s = str(code); cls = "ok" if s.startswith("2") else "run" if s.startswith("3") else "bad" if (s.startswith("4") or s.startswith("5")) else "mut"
    return badge(s or "?", cls)
def render_targets_panel(targets) -> str:
    headers = ["domain", "status", "program", "tags", "subs", "assets", "requested", "discovered", "created", ""]
    rows, meta = [], []
    for t in targets:
        acts = (f'<span class="act" data-domain="{e(t["domain"])}">'
                f'<button class="mini" data-act="edit" data-domain="{e(t["domain"])}">edit</button>'
                f'<button class="mini" data-act="probe" data-domain="{e(t["domain"])}">re-probe</button>'
                f'<button class="mini bad" data-act="del" data-domain="{e(t["domain"])}">del</button>'
                f'</span>')
        rows.append([e(t["domain"]), _status_badge(t["status"]), e(t["program"] or "—"), _tag_chips(t["tags"]), t["n_sub"], t["n_assets"], t["requested"], t["discovered"], fmt_ts(_ts(t["created_at"])), acts])
        meta.append({"kind": "target", "domain": t["domain"], "_class": "selectable"})
    buttons = '<button class="add" data-act="add-target">+ add target</button>'
    return panel("targets", "Targets", len(targets), table("tbl-targets", headers, rows, meta), head_buttons=buttons)
def render_subdomains_panel(subs, selected_target: str = None) -> str:
    headers = ["target", "hostname", "assets", "html", "scripts", "css", "img", "critical", "last seen"]
    rows, meta = [], []
    filtered = selected_target is not None
    target_filter = selected_target or ""

    for s in subs:
        c = s["counts"]
        rows.append([e(s["target"]), e(s["hostname"]), s["n_assets"], c["html"], c["scripts"], c["stylesheets"], c["images"], c["critical"], fmt_ts(s["last"])])
        meta.append({
            "kind": "sub", 
            "target": s["target"], 
            "host": s["hostname"], 
            "_class": "selectable",
            "filtered": filtered,
            "filtered_by": target_filter
        })

    head_buttons = '<input id="subq" class="filter" placeholder="search…" spellcheck="false" autocomplete="off">'
    if filtered:
        head_buttons += f'<span class="filter-badge">Filtered by {e(target_filter)} <button id="clear-filter">×</button></span>'
    
    return panel("subdomains", "Subdomains", len(subs), table("tbl-subdomains", headers, rows, meta), head_buttons=head_buttons, filter_for=False)
def _asset_selector(targets) -> str:
    opts = ['<option value="all">all assets</option>']
    for t in targets:
        dom = t["domain"]; opts.append(f'<option value="{e(dom)}||*">{e(dom)} (all)</option>')
        for host in t["subdomains"]: opts.append(f'<option value="{e(dom)}||{e(host)}">&nbsp;&nbsp;{e(host)}</option>')
    return f'<select id="assetsel" class="logsel">{"".join(opts)}</select>'
def render_assets_panel(assets, targets) -> str:
    headers = ["target", "host", "type", "name", "st", "ctype", "size", "url", "fetched", ""]
    rows = []
    for a in assets[:MAX_ROWS]:
        acts = f'<a class="olink" data-asset="{e(a["path"])}">view</a>'
        rows.append([e(a["target"]), e(a["hostname"]), e(a["type"]), e(a["name"]), _http_badge(a["status"]), e(a["content_type"]), fmt_size(a["size"]), e(a["url"]), fmt_ts(_ts(a["fetched"])), acts])
    return panel("assets", "Assets", len(assets), table("tbl-assets", headers, rows), head_buttons=_asset_selector(targets))
def render_requests_panel(summary, completed) -> str:
    headers = ["id", "domain", "url", "status", "finished", ""]
    rows = []
    for r in completed[:MAX_ROWS]:
        err = f' <span class="bad" title="{e(r["error"])}">!</span>' if r["error"] else ""
        rows.append([e(r["id"]), e(r["domain"]), e(r["url"]), _http_badge(r["status"]) + err, fmt_ts(r["mtime"]), ""])
    head = f'<span class="hi"><b>{summary["pending"]}</b> pend · <b>{summary["ready"]}</b> ready · <b>{summary["completed"]}</b> done</span>'
    return panel("requests", "Requests", summary["completed"], table("tbl-requests", headers, rows), head_buttons=head)
def render_activity_panel(feed) -> str:
    headers = ["time", "task", "event"]
    rows = []
    for f in feed:
        cls = "ok" if f["status"] == "ok" else "bad" if f["status"] == "fail" else "mut"
        rows.append([fmt_ts(f["time"]), e(f["task"]), f'<span class="badge {cls}">{e(f["text"])}</span>'])
    return panel("activity", "Activity", len(feed), table("tbl-activity", headers, rows))
def render_rate_panel(rate, pend_by_dom) -> str:
    headers = ["domain", "next slot", "pending"]
    rows = []
    for dom, slot in sorted(rate.items()): rows.append([e(dom), fmt_ts(slot), pend_by_dom.get(dom, 0)])
    return panel("rate", "Rate limits", len(rows), table("tbl-rate", headers, rows))
def _header_stats(targets, subs, assets, reqs, eng) -> str:
    alive = bool(eng.get("active") or eng.get("running"))
    return (f'<span class="hi"><b>{len(targets)}</b> targets</span>'
            f'<span class="hi"><b>{len(subs)}</b> subdomains</span>'
            f'<span class="hi"><b>{len(assets)}</b> assets</span>'
            f'<span class="hi"><b>{reqs["pending"]}</b> pending</span>'
            f'<span class="hi mode {"eon" if alive else "eoff"}">engine {"up" if alive else "down"}</span>')
def render_page(paths: Paths, interactive=True) -> str:
    targets = load_targets(); subs = load_subdomains(targets); assets = load_assets(); summary = load_request_summary(); completed = load_recent_completed(); feed = load_activity_feed(); rate = load_rate_state(); eng = engine_status();
    panels = (render_targets_panel(targets) + render_subdomains_panel(subs) + render_assets_panel(assets, targets) + render_requests_panel(summary, completed) + render_activity_panel(feed) + render_rate_panel(rate, {}))
    stats = _header_stats(targets, subs, assets, summary, eng); alive = bool(eng.get("active") or eng.get("running"))
    html = PAGE.replace("__PANELS__", panels).replace("__STATS__", stats).replace("__REFRESH__", str(REFRESH_SECONDS)).replace("__GENEPOCH__", str(int(time.time()))).replace("__STALE__", str(STALE_AFTER)).replace("__ENGINEALIVE__", "true" if alive else "false").replace("__STATUSES__", json.dumps(list(TARGET_STATUSES))).replace("__ASSETSVER__", json.dumps(assets_version(assets))).replace("__REQVER__", json.dumps(requests_version(summary)))
    return html
def make_handler(paths: Paths):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def _send(self, code, ctype, body):
            if isinstance(body, str): body = body.encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def _json(self, code, obj): self._send(code, "application/json", json.dumps(obj))
        def _body(self):
            try: n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n) if n else b""; return json.loads(raw) if raw else {}
            except: return None
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"): self._send(200, "text/html; charset=utf-8", render_page(paths)); return
            parts = [p for p in path.split("/") if p]
            if parts[:2] == ["api", "engine"]: self._json(200, engine_status()); return
            if parts[:2] == ["api", "asset"]:
                rel = parse_qs(urlparse(self.path).query).get("path", [""])[0]; data, ctype = read_asset(unquote(rel))
                if data is None: self._json(404, {"errors": ["asset not found"]})
                else: self._send(200, ctype, data); return
            if parts[:2] == ["api", "run"] and len(parts) >= 3:
                detail = get_run_detail(paths.state_dir, paths.logs_dir, unquote(parts[2])); self._json(200 if detail else 404, detail or {"errors": ["run not found"]}); return
            if parts[:2] == ["api", "targets"]: self._json(200, {"items": load_targets()}); return
            if parts[:2] == ["api", "assets"]: assets = load_assets(); self._json(200, {"version": assets_version(assets), "rows": asset_rows_json(assets)}); return
            if parts[:2] == ["api", "requests"]: summary = load_request_summary(); completed = load_recent_completed(limit=5000); self._json(200, {"version": requests_version(summary), "rows": request_rows_json(completed)}); return
            self._json(404, {"errors": ["not found"]})
        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts[:2] == ["api", "engine"]:
                from . import service; data = self._body() or {}; ok, msg = service.control(data.get("action", "")); out = {"ok": ok, "message": msg}; out.update(engine_status()); self._json(200 if ok else 400, out); return
            if parts[:2] == ["api", "targets"]:
                if len(parts) >= 4 and parts[3] == "probe": code, res = reprobe_target(unquote(parts[2]))
                elif len(parts) >= 5 and parts[3] == "task": code, res = run_task_for_target(unquote(parts[2]), unquote(parts[4]))
                elif len(parts) >= 7 and parts[3] == "sub" and parts[5] == "task": code, res = run_task_for_sub(unquote(parts[2]), unquote(parts[4]), unquote(parts[6]))
                elif len(parts) >= 6 and parts[3] == "sub" and parts[5] == "explore": code, res = open_explorer(unquote(parts[2]), unquote(parts[4]))
                else:
                    data = self._body()
                    if data is None: self._json(400, {"ok": False, "errors": ["invalid JSON body"]}); return
                    code, res = add_target(data)
                self._json(code, res); return
            self._json(404, {"errors": ["not found"]})
        def do_PUT(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts[:2] == ["api", "targets"] and len(parts) >= 3:
                data = self._body()
                if data is None: self._json(400, {"ok": False, "errors": ["invalid JSON body"]}); return
                code, res = edit_target(unquote(parts[2]), data); self._json(code, res); return
            self._json(404, {"errors": ["not found"]})
        def do_DELETE(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts[:2] == ["api", "targets"] and len(parts) >= 3: code, res = delete_target(unquote(parts[2])); self._json(code, res); return
            self._json(404, {"errors": ["not found"]})
    return Handler
def serve(paths: Paths, host="127.0.0.1", port=8766, quiet=False):
    httpd = ThreadingHTTPServer((host, port), make_handler(paths))
    if not quiet: print(f"bugbounty dashboard serving at http://{host}:{port}/")
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally: httpd.server_close()
def main(argv=None):
    import argparse; p = argparse.ArgumentParser(); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8766); p.add_argument("--root", default=_root()); args = p.parse_args(argv); os.environ.setdefault("AGENTC_ROOT", args.root); serve(Paths(args.root), host=args.host, port=args.port)
def _dashboard_css() -> str:
    from .dashboard import PAGE as _P; return _P.split("<style>", 1)[1].split("</style>", 1)[0]
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><title>agentC · bugbounty</title>
<style>__CSS__
  .panel { width: calc(50% - 5px); height: calc((100vh - 38px)/2 - 3px); }
  .ctxmenu { position: fixed; z-index: 70; min-width: 124px; display: none; background: #161b22; border: 1px solid #30363d; border-radius: 5px; padding: 3px; box-shadow: 0 4px 16px rgba(0,0,0,.5); }
  .ctxmenu .ci { padding: 4px 10px; font-size: 11px; color: #c9d1d9; cursor: pointer; border-radius: 3px; white-space: nowrap; }
  .ctxmenu .ci:hover { background: #1f6feb; color: #fff; }
</style>
</head>
<body>
<header>
  <span class="brand">agentC</span> <span class="hi mode">bugbounty</span>
  __STATS__
  <span class="spacer"></span>
  <span class="engctl" id="engctl">
    <button class="eng eon" id="eng-start">start</button>
    <button class="eng eoff" id="eng-stop">stop</button>
    <button class="eng" id="eng-restart">restart</button>
  </span>
  <label class="pause"><input type="checkbox" id="pause"> pause</label>
  <span class="hi">upd <span id="ago">0s</span></span>
  <button id="panelsbtn">&#9776; panels</button>
  <button id="reload">&#x21bb;</button>
</header>
<div class="grid">__PANELS__</div>
<div class="overlay" id="overlay"><div class="modal"><h3>Add target</h3><div class="mbody"><div class="errs" id="merrs"></div><form id="mform"><div class="frow"><label>domain *</label><input type="text" id="f_domain"></div><div class="frow"><label>program</label><input type="text" id="f_program"></div><div class="frow"><label>status</label><select id="f_status"></select></div><div class="frow"><label>tags</label><input type="text" id="f_tags"></div><div class="frow"><label>in scope</label><textarea id="f_scope_in"></textarea></div><div class="frow"><label>out scope</label><textarea id="f_scope_out"></textarea></div><div class="frow"><label>notes</label><textarea id="f_notes"></textarea></div></form></div><div class="mfoot"><button id="mcancel">Cancel</button><button id="msave">Save</button></div></div></div>
<div class="overlay" id="coverlay"><div class="modal"><h3>Confirm</h3><div class="mbody" id="cmsg"></div><div class="mfoot"><button id="cno">Cancel</button><button id="cyes">Delete</button></div></div></div>
<div class="overlay" id="poverlay"><div class="modal"><h3>Panels</h3><div class="mbody" id="plist"></div><div class="mfoot"><button id="preset">Reset</button><button id="pclose">Close</button></div></div></div>
<div class="overlay" id="roverlay"><div class="modal wide"><h3 id="rdtitle">Run detail</h3><div class="mbody" id="rdbody"></div><div class="mfoot"><button id="rdclose">Close</button></div></div></div>
<div class="overlay" id="aoverlay"><div class="modal wide"><h3 id="adtitle">Asset</h3><div class="mbody"><pre id="adbody" style="max-height:70vh"></pre></div><div class="mfoot"><a id="adraw" target="_blank">Open raw</a><button id="adclose">Close</button></div></div></div>
<div class="ctxmenu" id="ctx-target"><div class="ci" data-act="enum-subs">Enumerate Subdomains</div></div>
<div class="ctxmenu" id="ctx-sub">
  <div class="ci" data-act="sub-all">Run All Spiders</div>
  <div class="ci" data-act="sub-dom">Run DOM Spider</div>
  <div class="ci" data-act="sub-script">Run Script Spider</div>
  <div class="ci" data-act="sub-critical">Run Critical Asset Scan</div>
  <div class="ci" data-act="sub-explore">Explore Folder</div>
</div>
<div class="toast" id="toast"></div>
<script>
var REFRESH=__REFRESH__, GEN=__GENEPOCH__, STALE=__STALE__, ENGINE_ALIVE=__ENGINEALIVE__, STATUSES=__STATUSES__;
var modalOpen=false, confirmOpen=false, dragging=false, EMODE='add', EDOMAIN='', ctxOpen=false, ctxTarget=null;
function esc(s){ return String(s).replace(/[&<>"]/g,function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function toast(msg){ var t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show'); setTimeout(function(){ t.classList.remove('show'); }, 2600); }
document.addEventListener('click', function(ev){
  var b=ev.target.closest('[data-act]');
  if(b){
    var act=b.getAttribute('data-act'), dom=b.getAttribute('data-domain');
    if(act==='add-target') openAdd(); else if(act==='edit') openEdit(dom); else if(act==='del') delTarget(dom); else if(act==='probe') reprobe(dom);
    return;
  }
  var tr=ev.target.closest('tbody tr');
  if(tr && tr.getAttribute('data-kind')==='sub' && window.scopeAssets){ scopeAssets(tr.getAttribute('data-target'), tr.getAttribute('data-host')); }
});
(function(){
  var mTarget=document.getElementById('ctx-target'), mSub=document.getElementById('ctx-sub');
  function hide(){ [mTarget, mSub].forEach(function(m){ if(m) m.style.display='none'; }); ctxOpen=false; ctxTarget=null; }
  document.addEventListener('contextmenu', function(ev){
    var tr=ev.target.closest('tbody tr'); if(!tr) return;
    var kind=tr.getAttribute('data-kind'), m=kind==='target'?mTarget:kind==='sub'?mSub:null;
    if(!m) return; ev.preventDefault(); ctxTarget=tr;
    m.style.left=ev.pageX+'px'; m.style.top=ev.pageY+'px'; m.style.display='block'; ctxOpen=true;
  });
  document.addEventListener('click', function(ev){
    var ci=ev.target.closest('.ci');
    if(ci && ctxTarget){
      var act=ci.getAttribute('data-act');
      if(act==='enum-subs'){
        var dom=ctxTarget.getAttribute('data-domain');
        fetch('/api/targets/'+encodeURIComponent(dom)+'/task/subfinder', {method:'POST'}).then(function(r){ return r.json(); }).then(function(res){ toast(res.ok?'Subfinder triggered':'Failed'); });
      } else if(act.startsWith('sub-')){
        var dom=ctxTarget.getAttribute('data-target'), host=ctxTarget.getAttribute('data-host');
        if(act==='sub-explore') fetch('/api/targets/'+encodeURIComponent(dom)+'/sub/'+encodeURIComponent(host)+'/explore', {method:'POST'});
        else {
          var t={'sub-all':'all_spiders','sub-dom':'dom_spider','sub-script':'script_spider','sub-critical':'critical'}[act];
          fetch('/api/targets/'+encodeURIComponent(dom)+'/sub/'+encodeURIComponent(host)+'/task/'+t, {method:'POST'}).then(function(r){ return r.json(); }).then(function(res){ toast(res.ok?'Task triggered':'Failed'); });
        }
      }
    }
    hide();
  });
})();
function tick(){ var age=Math.floor(Date.now()/1000)-GEN; var a=document.getElementById('ago'); if(a) a.textContent=age+'s'; }
tick(); setInterval(tick, 1000);
</script>
</body>
</html>
"""
PAGE = PAGE.replace("__CSS__", _dashboard_css())
if __name__ == "__main__": main()
