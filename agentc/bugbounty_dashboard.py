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
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty
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
# Tiny TTL cache — shields the every-few-seconds refresh from re-scanning huge
# on-disk directories (the runs dir can hold tens of thousands of files). A few
# seconds of staleness on a live dashboard is invisible; re-statting 50k+ files
# on every poll is not. Thread-safe for the ThreadingHTTPServer workers.
# --------------------------------------------------------------------------- #
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}


def _cached(key, ttl: float, producer):
    now = time.time()
    with _CACHE_LOCK:
        ent = _CACHE.get(key)
        if ent and ent[0] > now:
            return ent[1]
    val = producer()
    with _CACHE_LOCK:
        _CACHE[key] = (now + ttl, val)
    return val


# --------------------------------------------------------------------------- #
# Real-time push (SSE). The engine appends to ``state/events.jsonl``; a watcher
# thread tails it and fans new events out to all connected dashboards, which
# then do an immediate (version-checked) refresh — so panels update the moment
# something happens instead of waiting for the next poll.
# --------------------------------------------------------------------------- #
class _SSEBroker:
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self) -> Queue:
        q: Queue = Queue(maxsize=200)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, msg: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except Exception:  # noqa: BLE001 — full queue: drop, client catches up via fallback poll
                pass

    def count(self) -> int:
        with self._lock:
            return len(self._subs)


_SSE = _SSEBroker()


def _event_watcher(paths: "Paths") -> None:
    """Tail ``events.jsonl`` and publish each new engine event to SSE clients."""
    ev_path = os.path.join(paths.state_dir, "events.jsonl")
    try:
        last_size = os.path.getsize(ev_path)
    except OSError:
        last_size = 0
    while True:
        try:
            try:
                sz = os.path.getsize(ev_path)
            except OSError:
                sz = 0
            if sz < last_size:           # log truncated/rotated — start over
                last_size = 0
            if sz > last_size:
                with open(ev_path, "r", encoding="utf-8", errors="ignore") as fh:
                    fh.seek(last_size)
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        _SSE.publish({"type": "event", "name": ev.get("name", ""),
                                      "ts": ev.get("ts")})
                last_size = sz
        except Exception:  # noqa: BLE001 — never let the watcher die
            pass
        time.sleep(0.6)


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
        atype = os.path.basename(os.path.dirname(body))
        name = os.path.basename(body)[:-5]
        if atype == "critical" and name.startswith("CRITICAL_"):
            continue  # skip old content-matched duplicate copies
        total += 1
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
                at = os.path.basename(os.path.dirname(body))
                nm = os.path.basename(body)[:-5]
                if at == "critical" and nm.startswith("CRITICAL_"):
                    continue  # skip old content-matched duplicate copies
                total += 1
                if at in counts:
                    counts[at] += 1
                try:
                    last = max(last, os.path.getmtime(body))
                except OSError:
                    pass
            rows.append({"target": t["domain"], "hostname": host,
                         "n_assets": total, "counts": counts, "last": last})
    return rows


def _source_type_label(st: str) -> str:
    return {
        "dom": "DOM Spider", "script": "Script Spider", "seed": "Seed",
        "subfinder": "Subfinder", "critical_probe": "Critical Probe",
        "manual": "Manual", "critical": "Critical Probe",
    }.get(st or "", st or "")


def _queue_metrics_html(metrics: dict) -> str:
    """Render queue throughput metrics as header-style spans for the queue panel titlebar."""
    if not metrics:
        return ""
    rps = metrics.get("req_per_sec", 0.0)
    rpm = metrics.get("req_per_min", 0)
    max_dom = metrics.get("max_domain", "")
    max_ct = metrics.get("max_domain_count", 0)
    spct = metrics.get("success_pct", 100.0)
    rl = metrics.get("rate_limited", 0)
    status = metrics.get("status", "healthy")
    rl_cls = "bad" if rl > 0 else "mut"
    st_cls = "ok" if status == "healthy" else "bad"
    parts = [
        f'<span class="hi">req/sec <b>{rps}</b></span>',
        f'<span class="hi">req/min <b>{rpm}</b></span>',
    ]
    if max_dom:
        short = max_dom if len(max_dom) <= 24 else max_dom[:21] + "…"
        parts.append(f'<span class="hi" title="{e(max_dom)}">max/domain/min <b>{e(short)} ({max_ct})</b></span>')
    parts += [
        f'<span class="hi">success <b>{spct}%</b></span>',
        f'<span class="badge {rl_cls}">RL/min {rl}</span>',
        f'<span class="badge {st_cls}">{e(status)}</span>',
    ]
    return " ".join(parts)


def _build_source_type_map() -> dict:
    """Build {asset_id: source_type} from completed/200 request records.

    Fallback for assets whose sidecar pre-dates the source_type field."""
    out = {}
    d200 = os.path.join(requests_dir(), "completed", "200")
    try:
        for fname in os.listdir(d200):
            if not fname.endswith(".json"):
                continue
            rec = _load_json(os.path.join(d200, fname)) or {}
            req = rec.get("request") or {}
            aid = req.get("id") or fname[:-5]
            if aid:
                out[aid] = req.get("source_type", "")
    except OSError:
        pass
    return out


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
        # Skip old content-matched CRITICAL_ copies (they duplicate assets/html/ entries)
        if atype == "critical" and name.startswith("CRITICAL_"):
            continue
        meta = _load_json(body[:-5] + ".json") or {}
        try:
            size = os.path.getsize(body)
            mtime = os.path.getmtime(body)
        except OSError:
            size, mtime = 0, 0
        rows.append({
            "target": target, "hostname": hostname, "type": atype, "name": name,
            "source_type": meta.get("source_type", ""),
            "url": meta.get("url", ""), "status": meta.get("status_code", ""),
            "content_type": (meta.get("content_type", "") or "").split(";")[0],
            "duration_s": meta.get("duration_s", ""),
            "asset_id": meta.get("id", ""),
            "size": size, "fetched": meta.get("requested_at", "") or mtime,
            "mtime": mtime, "path": body,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def load_critical_assets() -> list:
    """Critical findings flagged by check_critical.py: asset bodies that matched
    a critical-paths pattern (.env, etc.), combined across ALL targets. Read from
    the ``assets/critical/*.json`` detection sidecars."""
    out = []
    for side in glob.glob(os.path.join(targets_dir(), "*", "*", "assets",
                                       "critical", "*.json")):
        parts = side.split(os.sep)
        try:
            idx = parts.index("targets")
            target, host = parts[idx + 1], parts[idx + 2]
        except (ValueError, IndexError):
            continue
        meta = _load_json(side) or {}
        body = side[:-5] + ".body"
        try:
            mtime = os.path.getmtime(side)
            size = os.path.getsize(body) if os.path.exists(body) else 0
        except OSError:
            mtime, size = 0, 0
        out.append({
            "target": target, "host": host,
            "matched": meta.get("matched_pattern", ""),
            "filename": meta.get("filename", os.path.basename(body)[:-5]),
            "original": meta.get("original_path", ""),
            "detected": _epoch(meta.get("detected_at")) or mtime,
            "size": size, "relpath": os.path.relpath(body, _root()), "mtime": mtime,
        })
    out.sort(key=lambda r: r["detected"], reverse=True)
    return out


def critical_version(crit: list) -> str:
    mx = max((c["detected"] for c in crit), default=0)
    return f"cr:{len(crit)}:{int(mx)}"


def critical_rows_json(crit: list) -> list:
    """[target, host, matched, filename, relpath, size, detected_epoch]."""
    return [[c["target"], c["host"], c["matched"], c["filename"], c["relpath"],
             c["size"], c["detected"]] for c in crit]


def _epoch(val) -> float:
    """Coerce a fetched/created value (epoch float or ISO string) to epoch secs."""
    v = _ts(val)
    return float(v) if isinstance(v, (int, float)) else 0.0


def assets_version(assets: list) -> str:
    """Change-token: schema version prefix + count + newest mtime.

    Incrementing the schema prefix (v3) invalidates cached sessionStorage rows
    after column layout changes so the client always re-fetches."""
    mx = max((a["mtime"] for a in assets), default=0)
    return f"v3:{len(assets)}:{int(mx)}"


def asset_rows_json(assets: list) -> list:
    """Compact per-asset row arrays for the virtualized client table.

    Column order must match render_assets_panel() / AssetsVT:
    [target, fetched_epoch, type, found_by, status, ctype, size, duration,
     url, relpath, asset_id, hostname]
    Indices 9-11 are not displayed but used for viewer, raw-request modal,
    and scope filtering respectively.
    """
    src_map = _build_source_type_map()
    rows = []
    for a in assets:
        rel = os.path.relpath(a["path"], _root())
        st = a.get("source_type") or src_map.get(a.get("asset_id", ""), "")
        found_by = _source_type_label(st)
        dur = a.get("duration_s", "")
        rows.append([
            a["target"],
            _epoch(a["fetched"]) or a["mtime"],
            a["type"],
            found_by,
            a["status"],
            a["content_type"],
            a["size"],
            f"{dur}s" if dur else "",
            a["url"],
            rel,                # [9]  relpath for asset viewer
            a["asset_id"],      # [10] id for raw-request modal
            a["hostname"],      # [11] hostname for scope filter
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


def load_asset_raw(asset_id: str) -> dict:
    """Return the full request+response record for an asset, keyed by id.

    The completed request JSON at requests/completed/{status}/{id}.json stores
    the original request headers/body and the response headers/status; the
    response body lives in the .body file referenced by body_file."""
    if not asset_id or not re.match(r"^[a-f0-9]{1,64}$", asset_id):
        return {"error": "invalid id"}
    comp = os.path.join(requests_dir(), "completed")
    try:
        for st in sorted(os.listdir(comp)):
            p = os.path.join(comp, st, asset_id + ".json")
            if os.path.exists(p):
                d = _load_json(p) or {}
                resp = d.get("response") or d
                body_file = resp.get("body_file")
                body_text = None
                if body_file:
                    full = os.path.join(_root(), body_file)
                    try:
                        with open(full, "rb") as fh:
                            raw = fh.read(65536)
                        body_text = raw.decode("utf-8", errors="replace")
                    except OSError:
                        pass
                d["_body_text"] = body_text
                return d
    except OSError:
        pass
    return {"error": "not found"}


def load_rate_state() -> dict:
    d = _load_json(os.path.join(requests_dir(), "rate_state.json"))
    return d if isinstance(d, dict) else {}


def load_request_metrics() -> dict:
    """Compute req/sec (last 3s), req/min, success %, rate-limited/min from completed requests."""
    now = time.time()
    cut60 = now - 60.0
    cut3 = now - 3.0
    comp = os.path.join(requests_dir(), "completed")
    sec_count = 0
    min_total = 0
    min_success = 0
    min_429 = 0
    domain_counts: dict = {}
    try:
        for st_dir in os.listdir(comp):
            spath = os.path.join(comp, st_dir)
            if not os.path.isdir(spath):
                continue
            try:
                sc = int(st_dir)
            except ValueError:
                continue
            try:
                fnames = os.listdir(spath)
            except OSError:
                continue
            for fname in fnames:
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(spath, fname)
                try:
                    mt = os.path.getmtime(fpath)
                except OSError:
                    continue
                if mt < cut60:
                    continue
                min_total += 1
                if 200 <= sc < 300:
                    min_success += 1
                if sc == 429:
                    min_429 += 1
                if mt >= cut3:
                    sec_count += 1
                if min_total <= 500:  # cap JSON reads for performance
                    try:
                        d = _load_json(fpath) or {}
                        req = d.get("request") or d
                        dom = req.get("domain", "?")
                        domain_counts[dom] = domain_counts.get(dom, 0) + 1
                    except Exception:  # noqa: BLE001
                        pass
    except OSError:
        pass
    rps = round(sec_count / 3.0, 1)
    spct = round(min_success * 100.0 / min_total, 1) if min_total else 100.0
    max_dom = max(domain_counts, key=domain_counts.get) if domain_counts else ""
    max_ct = domain_counts.get(max_dom, 0)
    degraded = min_total > 0 and (spct < 90.0 or min_429 > 0)
    return {
        "req_per_sec": rps,
        "req_per_min": min_total,
        "max_domain": max_dom,
        "max_domain_count": max_ct,
        "success_pct": spct,
        "rate_limited": min_429,
        "status": "degraded" if degraded else "healthy",
    }


def _scan_pending_by_domain(limit: int) -> dict:
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


def load_pending_by_domain(limit=4000) -> dict:
    """Count pending+ready requests per domain (capped parse, TTL-cached)."""
    return _cached(("pend_by_dom", limit), 6.0,
                   lambda: _scan_pending_by_domain(limit))


def load_bb_runs(paths: Paths, limit=40, recent: list = None) -> list:
    """Newest bugbounty runs. Scans only recent run records (never the full
    runs dir, which can hold tens of thousands of files). Pass ``recent`` to
    reuse an already-loaded ``load_recent_runs`` list."""
    if recent is None:
        recent = load_recent_runs(paths, n=1200)
    runs = [r for r in recent if str(r.get("task", "")).startswith("bugbounty")]
    return runs[:limit]


def _scan_recent_runs(rdir: str, n: int) -> list:
    """Load the *n* most-recently-modified run records (cheap, bounded).

    Uses ``os.scandir`` so the runs dir is enumerated in a single pass and only
    the newest *n* files are actually JSON-parsed — the directory can hold tens
    of thousands of records (every task run leaves one)."""
    ents = []
    try:
        with os.scandir(rdir) as it:
            for de in it:
                if not de.name.endswith(".json"):
                    continue
                try:
                    mt = de.stat().st_mtime
                except OSError:
                    mt = 0
                ents.append((mt, de.path))
    except OSError:
        return []
    ents.sort(key=lambda x: x[0], reverse=True)
    out = []
    for mt, p in ents[:n]:
        d = _load_json(p)
        if d:
            out.append(d)
    return out


def load_recent_runs(paths: Paths, n=500) -> list:
    """Cached wrapper over :func:`_scan_recent_runs` (TTL bounded)."""
    rdir = os.path.join(paths.state_dir, "runs")
    return _cached(("recent_runs", rdir, n), 6.0,
                   lambda: _scan_recent_runs(rdir, n))


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


def _run_hint(r: dict) -> str:
    """Short context string extracted from a run's variables (domain, URL, etc.)."""
    v = r.get("variables") or {}
    domain = v.get("domain") or ""
    url = v.get("url") or ""
    trigger = v.get("event_filename") or ""
    if url:
        return f" {url}"
    if domain:
        return f" [{domain}]"
    if trigger and not trigger.endswith(".json"):
        return f" [{trigger}]"
    return ""


def load_feed(paths: Paths, limit=400, recent: list = None) -> list:
    """A unified, time-ordered activity stream built from every task's run
    records.  Every run produces at least one entry so every task is visible.
    Pass ``recent`` to reuse an already-loaded ``load_recent_runs`` list."""
    entries = []
    if recent is None:
        recent = load_recent_runs(paths, n=1200)
    for r in recent:
        task = r.get("task", "")
        if task in _FEED_SKIP:
            continue
        t = r.get("finished") or r.get("started") or 0
        status = r.get("status", "")
        results = r.get("results") or []

        if not results:
            if status in ("running", "failed", "interrupted", "skipped"):
                verb = {"running": "running", "failed": "failed",
                        "interrupted": "interrupted", "skipped": "skipped"}[status]
                entries.append({"time": t, "task": task, "status": status,
                                "text": f"task {verb}" + (f": {r['error']}" if r.get("error") else "")})
            continue

        run_has_entry = False
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
                run_has_entry = True
            if not ok:
                err = _clean((a.get("stderr") or a.get("error") or ""))
                lines = [l for l in err.splitlines() if _clean(l)
                         and not any(n in l for n in _FEED_NOISE)]
                if lines:
                    entries.append({"time": t, "task": task, "status": "fail",
                                    "text": lines[-1][:240]})
                    run_has_entry = True

        # Fallback: every run gets at least one entry so nothing is invisible.
        if not run_has_entry:
            hint = _run_hint(r)
            if status == "ok":
                dur = ""
                s, f = r.get("started"), r.get("finished")
                if s and f:
                    dur = f" ({f - s:.1f}s)"
                entries.append({"time": t, "task": task, "status": "ok",
                                "text": f"completed{hint}{dur}"})
            elif status in ("failed", "interrupted"):
                entries.append({"time": t, "task": task, "status": status,
                                "text": f"{status}{hint}" + (
                                    f": {r['error']}" if r.get("error") else "")})
            elif status == "running":
                entries.append({"time": t, "task": task, "status": "running",
                                "text": f"running{hint}"})

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
    # Create the root domain as a subdomain stub so it appears in the
    # Subdomains panel and can be spidered/scoped like any other host.
    os.makedirs(os.path.join(targets_dir(), domain, domain, "assets"), exist_ok=True)
    init_rate_config_for_domain(domain)
    _drop_queue(domain)  # fires bugbounty-spider-init (state + initial + probes)
    return 200, {"ok": True, "domain": domain}


def import_targets(data: dict):
    content = data.get("content", "")
    program = (data.get("program") or "").strip()
    status = data.get("status", "active")
    if status not in TARGET_STATUSES:
        status = "active"

    lines = [l.strip() for l in content.splitlines()]
    domains = [l for l in lines if l and not l.startswith("#")]

    added, skipped, errors = [], [], []
    for raw in domains:
        domain = normalize_domain(raw)
        if not valid_domain(domain):
            errors.append(raw)
            continue
        if os.path.isdir(os.path.join(targets_dir(), domain)):
            skipped.append(domain)
            continue
        meta = {"domain": domain, "program": program, "status": status,
                "tags": [], "scope_in": [domain], "scope_out": [], "notes": ""}
        _write_meta(domain, meta)
        os.makedirs(os.path.join(targets_dir(), domain, domain, "assets"), exist_ok=True)
        init_rate_config_for_domain(domain)
        _drop_queue(domain)
        added.append(domain)

    return 200, {"ok": True, "added": added, "skipped": skipped, "errors": errors}


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


def run_task_for_target(domain: str, task: str):
    domain = normalize_domain(domain)
    if not os.path.isdir(os.path.join(targets_dir(), domain)):
        return 404, {"ok": False, "errors": ["target not found"]}
    if task == "subfinder":
        try:
            queue_dir = os.path.join(_root(), "bugbounty/targets/subfinder_queue")
            os.makedirs(queue_dir, exist_ok=True)
            open(os.path.join(queue_dir, domain), "w").close()
            return 200, {"ok": True, "domain": domain, "message": "subfinder triggered"}
        except Exception as e:
            return 500, {"ok": False, "errors": [str(e)]}
    return 400, {"ok": False, "errors": ["unknown task"]}


def run_task_for_sub(domain: str, host: str, task: str):
    domain = normalize_domain(domain)
    host = normalize_domain(host)
    try:
        subprocess.Popen([sys.executable, "bugbounty/scripts/trigger_sub_task.py", task, host, domain], cwd=_root())
        return 200, {"ok": True, "domain": domain, "host": host, "message": f"{task} triggered"}
    except Exception as e:
        return 500, {"ok": False, "errors": [str(e)]}


def open_explorer(domain: str, host: str):
    target_path = os.path.join(targets_dir(), domain, host)
    try:
        # Try xdg-open for Linux, explorer for Win, open for Mac
        if os.name == "nt":
            subprocess.Popen(["explorer", target_path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target_path])
        else:
            subprocess.Popen(["xdg-open", target_path])
        return 200, {"ok": True}
    except Exception as e:
        return 500, {"ok": False, "errors": [str(e)]}


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


def _runs_cleared_path(paths: Paths) -> str:
    return os.path.join(paths.state_dir, "bb_runs_cleared.json")


def load_cleared_before(paths: Paths) -> float:
    d = _load_json(_runs_cleared_path(paths))
    if isinstance(d, dict):
        return float(d.get("cleared_before", 0) or 0)
    return 0.0


def save_cleared_before(paths: Paths, epoch: float) -> None:
    p = _runs_cleared_path(paths)
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"cleared_before": epoch}, fh)
    except OSError:
        pass


def restart_dashboard_service() -> None:
    import subprocess
    def _do():
        time.sleep(0.5)
        subprocess.run(["systemctl", "--user", "restart", "agentc-bugbounty"],
                       capture_output=True)
    threading.Thread(target=_do, daemon=True).start()


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


def targets_rows_json(targets) -> list:
    """Compact rows: [domain, status, program, tags[], n_sub, n_assets,
    requested, discovered, created_epoch]. Badges/chips/action buttons render
    client-side (TargetsVT)."""
    return [[t["domain"], t["status"], t["program"] or "", list(t["tags"] or []),
             t["n_sub"], t["n_assets"], t["requested"], t["discovered"],
             _ts(t["created_at"]) or 0] for t in targets]


def targets_version(targets) -> str:
    return (f"tg:{len(targets)}:{sum(t['n_assets'] for t in targets)}:"
            f"{sum(t['n_sub'] for t in targets)}:{sum(t['requested'] for t in targets)}")


def render_targets_panel(targets=None) -> str:
    # Body is a client-side virtualized table fed by /api/targets.
    headers = ["domain", "status", "program", "tags", "subs", "assets",
               "requested", "discovered", "created", ""]
    buttons = ('<button class="add" data-act="add-target">+ add target</button>'
               '<button class="mini" data-act="import-targets" style="margin-left:6px">import</button>'
               '<input id="tgtq" class="filter" placeholder="search…" '
               'spellcheck="false" autocomplete="off">')
    return panel("targets", "Targets", len(targets or []),
                 table("tbl-targets", headers, [], None),
                 head_buttons=buttons, filter_for=False)


def subs_rows_json(subs) -> list:
    """Compact rows: [target, hostname, n_assets, html, scripts, css, img,
    critical, last_epoch]."""
    out = []
    for s in subs:
        c = s["counts"]
        out.append([s["target"], s["hostname"], s["n_assets"], c["html"],
                    c["scripts"], c["stylesheets"], c["images"], c["critical"],
                    s["last"]])
    return out


def subs_version(subs) -> str:
    newest = max((s["last"] for s in subs), default=0)
    return f"sb:{len(subs)}:{sum(s['n_assets'] for s in subs)}:{int(newest)}"


def render_subdomains_panel(subs=None) -> str:
    # Body is a client-side virtualized table fed by /api/subdomains.
    headers = ["target", "hostname", "assets", "html", "scripts", "css",
               "img", "critical", "last seen"]
    head = ('<span class="filterbar" id="subfilter" style="display:none">'
            'filtered: <b id="subfilterval"></b>'
            '<button class="mini" id="subfilterclear">&#10005; clear</button>'
            '</span>'
            '<input id="subq" class="filter" placeholder="search…" '
            'spellcheck="false" autocomplete="off">')
    return panel("subdomains", "Subdomains", len(subs or []),
                 table("tbl-subdomains", headers, [], None),
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
    headers = ["target", "fetched", "type", "found by", "code", "ctype",
               "size", "duration", "url", "raw"]
    head = (_asset_selector(targets)
            + '<input id="assetq" class="filter" placeholder="search…" '
              'spellcheck="false" autocomplete="off">')
    return panel("assets", "Assets", len(assets),
                 table("tbl-assets", headers, [], None),
                 head_buttons=head, filter_for=False)


def render_critical_panel(crit=None) -> str:
    # Virtualized grid fed by /api/critical — critical findings across ALL targets.
    headers = ["target", "host", "matched", "file", "size", "detected", ""]
    head = ('<input id="critq" class="filter" placeholder="search…" '
            'spellcheck="false" autocomplete="off">')
    return panel("critical", "Critical findings", len(crit or []),
                 table("tbl-critical", headers, [], None),
                 head_buttons=head, filter_for=False)


def _scan_request_rates() -> dict:
    """Completed-request throughput from completed/* file mtimes: req/s over the
    last 3s ('now'), and per-second averages over the last 1 min and 10 min."""
    now = time.time()
    cut3, cut60, cut600 = now - 3.0, now - 60.0, now - 600.0
    comp = os.path.join(requests_dir(), "completed")
    n3 = n60 = n600 = 0
    try:
        for st_dir in os.listdir(comp):
            spath = os.path.join(comp, st_dir)
            if not os.path.isdir(spath):
                continue
            try:
                names = os.listdir(spath)
            except OSError:
                continue
            for fname in names:
                if not fname.endswith(".json"):
                    continue
                try:
                    mt = os.path.getmtime(os.path.join(spath, fname))
                except OSError:
                    continue
                if mt < cut600:
                    continue
                n600 += 1
                if mt >= cut60:
                    n60 += 1
                if mt >= cut3:
                    n3 += 1
    except OSError:
        pass
    return {"now": round(n3 / 3.0, 1), "m1": round(n60 / 60.0, 2),
            "m10": round(n600 / 600.0, 2)}


def request_rates() -> dict:
    return _cached("req_rates", 2.0, _scan_request_rates)


def request_counts_html(summary, qtotals) -> str:
    """Clean queue summary + throughput for the consolidated Requests header."""
    r = request_rates()
    return (f'<span class="badge run" title="queued, awaiting a rate slot">'
            f'{qtotals.get("pending", 0)} pending</span> '
            f'<span class="badge ok" title="rate slot granted, awaiting send">'
            f'{qtotals.get("ready", 0)} ready</span> '
            f'<span class="badge mut" title="sent / fetched">'
            f'{summary["completed_total"]} completed</span> '
            f'<span class="count" title="completed req/s: now (3s) · avg last 1m · avg last 10m">'
            f'&#9889; {r["now"]}/s now &middot; {r["m1"]}/s 1m &middot; {r["m10"]}/s 10m</span>')


def render_requests_panel(summary, queue=None, qtotals=None,
                          paused=False, metrics=None) -> str:
    """Consolidated Requests + Queue panel: one virtualized grid showing both
    queued (pending/ready) and completed requests, with a clean queue-count
    summary, pause, and per-item delete (queued rows only)."""
    headers = ["", "status", "domain", "url", "source", "when"]
    qtotals = qtotals or {}
    pause_cls = "bad" if paused else ""
    pause_lbl = "Resume queue" if paused else "Pause queue"
    total = qtotals.get("pending", 0) + qtotals.get("ready", 0) + summary["completed_total"]
    head = (f'<span id="req-counts" class="count">{request_counts_html(summary, qtotals)}</span> '
            f'<button class="mini {pause_cls}" id="q-pause">{pause_lbl}</button> '
            f'<button class="mini" id="reqdone" title="show / hide completed requests (queued only by default)">show completed</button> '
            f'<button class="mini" id="q-all" title="select/clear all queued (deletable) requests in view">All</button> '
            f'<button class="mini bad" id="q-del" disabled>Delete Selected</button> '
            '<input id="reqq" class="filter" placeholder="search…" '
            'spellcheck="false" autocomplete="off">')
    return panel("requests", "Requests", total,
                 table("tbl-requests", headers, [], None),
                 head_buttons=head, filter_for=False)


def requests_grid_rows(queue_items, completed) -> list:
    """Unified rows for the consolidated grid:
    [key, kind, status, domain, url, source, when_epoch].
      * queued: key=queue-id, kind/status='pending'|'ready', deletable
      * done:   key=domain|url|when, kind='done', status=HTTP code"""
    rows = []
    for it in queue_items:
        src = _source_type_label(it.get("source_type", "")) or it.get("source_type", "") or "—"
        rows.append([it["id"], it["qstatus"], it["qstatus"], it.get("domain", ""),
                     it.get("url", "") or "", src,
                     _ts(it.get("created_at", "")) or it.get("mtime", 0)])
    for c in completed:
        # request_rows_json(completed) → [status, domain, url, source, when]
        rows.append([f'{c[1]}|{c[2]}|{c[4]}', "done", c[0], c[1], c[2], c[3], c[4]])
    return rows


def requests_grid_version(summary, queue, qtotals) -> str:
    return f"{requests_version(summary)}|{queue_version(queue, qtotals)}"


def _activity_runs(paths, runs) -> list:
    """Recent bugbounty runs within the activity window (24h, after last clear)."""
    now = time.time()
    floor = max(load_cleared_before(paths), now - 86400)
    return [r for r in runs
            if (r.get("started") or r.get("finished") or 0) > floor]


def activity_rows_json(paths, runs) -> list:
    """Compact rows: [task, status, started_epoch, finished_epoch, trigger, run_id]."""
    rows = []
    for r in _activity_runs(paths, runs):
        rows.append([
            r.get("task", ""), r.get("status", ""),
            r.get("started", 0) or 0, r.get("finished", 0) or 0,
            r.get("trigger", ""), r.get("id", ""),
        ])
    return rows


def activity_version(paths, runs) -> str:
    rs = _activity_runs(paths, runs)
    newest = max((r.get("finished") or r.get("started") or 0 for r in rs), default=0)
    return f"act:{len(rs)}:{int(newest)}"


def render_activity_panel(paths=None, eng=None, rate=None, pend_by_dom=None, runs=None) -> str:
    # Body is a client-side virtualized table fed by /api/activity.
    headers = ["task", "status", "started", "duration", "trigger"]
    n = len(_activity_runs(paths, runs or [])) if paths else 0
    head = ('<button class="mini" data-act="clear-runs">Clear</button> '
            '<input id="actq" class="filter" placeholder="search…" '
            'spellcheck="false" autocomplete="off">')
    return panel("activity", "Activity", n,
                 table("tbl-activity", headers, [], None),
                 head_buttons=head, filter_for=False)


_FEED_LEVEL = {'ok': 'info', 'running': 'info', 'failed': 'error', 'fail': 'error',
               'interrupted': 'warning', 'skipped': 'debug'}

def feed_rows_json(feed) -> list:
    """Compact rows: [time_epoch, task, status, text, level]."""
    return [[f["time"], f["task"], f["status"], (f["text"] or "")[:200],
             _FEED_LEVEL.get(f["status"], "info")] for f in feed]


def feed_version(feed) -> str:
    newest = max((f["time"] for f in feed), default=0)
    return f"fd:{len(feed)}:{int(newest)}"


def render_feed_panel(feed=None) -> str:
    # Body is a client-side virtualized table fed by /api/feed; the level select
    # filters client-side via a VTable scope predicate.
    headers = ["time", "task", "", "activity"]
    head = ('<select id="feedlevel" class="logsel" title="minimum log level">'
            '<option value="debug">all levels</option>'
            '<option value="info">info+</option>'
            '<option value="warning">warning+</option>'
            '<option value="error">errors only</option>'
            '</select>'
            '<input id="feedq" class="filter" placeholder="search…" '
            'spellcheck="false" autocomplete="off">')
    return panel("feed", "Activity feed", len(feed or []),
                 table("tbl-feed", headers, [], None),
                 head_buttons=head, filter_for=False)


def _rate_last(rate, d):
    slot = rate.get(d, 0)
    return slot.get("last", 0) if isinstance(slot, dict) else (slot or 0)


def rate_rows_json(rate, pend_by_dom) -> list:
    """Compact rows for the virtualized rate table: [domain, queued, last_epoch].
    The client renders 'last slot' and the live 'next ready' countdown."""
    domains = set(list(rate.keys()) + list(pend_by_dom.keys()))
    return [[d, pend_by_dom.get(d, 0), _rate_last(rate, d)] for d in domains]


def rate_version(rate, pend_by_dom) -> str:
    domains = set(list(rate.keys()) + list(pend_by_dom.keys()))
    return f"rt:{len(domains)}:{sum(pend_by_dom.values())}"


def render_rate_panel(rate=None, pend_by_dom=None) -> str:
    # Body is a client-side virtualized table fed by /api/rate (no row cap —
    # the rate map can hold thousands of domains; the client windows them).
    headers = ["domain", "queued", "last slot", "next ready"]
    n = len(set(list((rate or {}).keys()) + list((pend_by_dom or {}).keys())))
    head = ('<input id="rateq" class="filter" placeholder="search…" '
            'spellcheck="false" autocomplete="off">')
    return panel("rate", "Rate limits", n,
                 table("tbl-rate", headers, [], None),
                 head_buttons=head, filter_for=False)


# --------------------------------------------------------------------------- #
# Queue management
# --------------------------------------------------------------------------- #
def _paused_path() -> str:
    return os.path.join(requests_dir(), "PAUSED")


def is_paused() -> bool:
    return os.path.exists(_paused_path())


def set_pause(paused: bool) -> None:
    p = _paused_path()
    if paused:
        open(p, "w").close()
    else:
        try:
            os.remove(p)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Reactive-task automation toggles (same file the engine's gate reads, so
# changes take effect immediately without an engine restart).
# --------------------------------------------------------------------------- #
def _reactions_path() -> str:
    return os.path.join(_root(), "state", "reactive_tasks.json")


def load_reactions_state() -> dict:
    cfg = _load_json(_reactions_path()) or {}
    return {"paused_all": bool(cfg.get("paused_all", False)),
            "paused_tasks": list(cfg.get("paused_tasks", []) or [])}


def save_reactions_state(cfg: dict) -> None:
    p = _reactions_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"paused_all": bool(cfg.get("paused_all", False)),
                   "paused_tasks": sorted(set(cfg.get("paused_tasks", []) or []))},
                  fh, indent=2)


def load_reactive_tasks() -> list:
    """Event/file-triggered tasks (the 'reactions') + their paused state, read
    from configs/tasks/*.json. These are what auto-run on a target add, a new
    request, etc."""
    state = load_reactions_state()
    paused_all = state["paused_all"]
    paused = set(state["paused_tasks"])
    tdir = os.path.join(_root(), "configs", "tasks")
    out = []
    try:
        names = sorted(os.listdir(tdir))
    except OSError:
        names = []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        d = _load_json(os.path.join(tdir, fn)) or {}
        trig = d.get("trigger", {}) or {}
        if trig.get("type") not in ("event", "file"):
            continue
        name = d.get("name", fn[:-5])
        out.append({
            "name": name,
            "trigger": trig.get("type"),
            "src": trig.get("event") or trig.get("path") or "",
            "desc": d.get("description", ""),
            "enabled": bool(d.get("enabled", True)),
            "paused": paused_all or (name in paused),
        })
    return out


def queue_totals() -> dict:
    """Cheap pending/ready/total counts via ``listdir`` (no JSON parse).

    Used for panel badges/counts so they stay accurate even though only the
    newest ``cap`` items are actually parsed for display by ``load_queue``."""
    rd = requests_dir()
    out = {"pending": 0, "ready": 0}
    for qstatus in ("pending", "ready"):
        try:
            out[qstatus] = sum(1 for n in os.listdir(os.path.join(rd, qstatus))
                               if n.endswith(".json"))
        except OSError:
            pass
    out["total"] = out["pending"] + out["ready"]
    return out


def load_queue(cap: int = MAX_ROWS) -> list:
    """Load the newest ``cap`` pending+ready queue items (newest first).

    Only the newest ``cap`` files are JSON-parsed — every file is ``stat``-ed for
    its mtime (cheap), but a flood of thousands of queued requests must not turn
    each render/refresh into thousands of JSON reads. Full counts come from
    ``queue_totals()``."""
    rd = requests_dir()
    stated = []
    for qstatus in ("ready", "pending"):
        qdir = os.path.join(rd, qstatus)
        try:
            for fname in os.listdir(qdir):
                if not fname.endswith(".json"):
                    continue
                p = os.path.join(qdir, fname)
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    mtime = 0
                stated.append((mtime, qstatus, p, fname))
        except OSError:
            pass
    stated.sort(key=lambda x: x[0], reverse=True)
    items = []
    for mtime, qstatus, p, fname in stated[:cap]:
        d = _load_json(p) or {}
        if not d:
            continue
        items.append({
            "id": d.get("id", fname[:-5]),
            "qstatus": qstatus,
            "domain": d.get("domain", ""),
            "url": d.get("url", ""),
            "source_type": d.get("source_type", ""),
            "created_at": d.get("created_at", ""),
            "mtime": mtime,
        })
    return items


def delete_queue_items(ids: list) -> int:
    """Delete specific pending/ready queue items by request id."""
    id_set = set(ids)
    rd = requests_dir()
    deleted = 0
    for qstatus in ("pending", "ready"):
        qdir = os.path.join(rd, qstatus)
        try:
            for fname in os.listdir(qdir):
                if not fname.endswith(".json"):
                    continue
                p = os.path.join(qdir, fname)
                d = _load_json(p) or {}
                req_id = d.get("id", fname[:-5])
                if req_id in id_set or fname[:-5] in id_set:
                    try:
                        os.remove(p)
                        deleted += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return deleted


def queue_rows_json(items: list) -> list:
    """Compact rows for the virtualized queue table:
    [id, qstatus, domain, url, source_label, created_epoch]."""
    rows = []
    for item in items:
        src = _source_type_label(item.get("source_type", "")) or item.get("source_type", "") or "—"
        rows.append([
            item["id"], item["qstatus"], item.get("domain", ""),
            item.get("url", "") or "", src, _ts(item.get("created_at", "")) or item.get("mtime", 0),
        ])
    return rows


def queue_version(items: list, totals: dict) -> str:
    newest = int(items[0]["mtime"]) if items else 0
    return f"q:{totals.get('total', len(items))}:{newest}"


def render_queue_panel(items=None, paused: bool = False, metrics: dict = None,
                       totals: dict = None) -> str:
    # Body is a client-side virtualized table fed by /api/queue; only the head
    # controls (metrics / All / pause / delete / counts) render server-side.
    headers = ["", "status", "domain", "url", "source", "created"]
    totals = totals or {}
    pending_ct = totals.get("pending", 0)
    ready_ct = totals.get("ready", 0)
    total_ct = totals.get("total", pending_ct + ready_ct)
    pause_cls = "bad" if paused else ""
    pause_lbl = "Resume queue" if paused else "Pause queue"
    ct_html = (f'<span class="badge run" title="pending">{pending_ct}p</span> '
               f'<span class="badge ok" title="ready">{ready_ct}r</span>')
    metrics_html = _queue_metrics_html(metrics) if metrics else ""
    head = (
        f'<span id="queue-metrics">{metrics_html}</span> '
        f'<input id="queueq" class="filter" placeholder="search…" spellcheck="false" autocomplete="off"> '
        f'<button class="mini" id="q-all">All</button> '
        f'<button class="mini {pause_cls}" id="q-pause">{pause_lbl}</button> '
        f'<button class="mini bad" id="q-del" disabled>Delete Selected</button> '
        f'<span id="queue-counts">{ct_html}</span>'
    )
    return panel("queue", "Queue", total_ct,
                 table("tbl-queue", headers, [], None),
                 head_buttons=head, filter_for=False)


# --------------------------------------------------------------------------- #
# Rate config helpers
# --------------------------------------------------------------------------- #
def _rate_config_path() -> str:
    return os.path.join(requests_dir(), "rate_config.json")


def load_rate_config() -> dict:
    return _load_json(_rate_config_path()) or {}


def save_rate_config(cfg: dict) -> None:
    p = _rate_config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def init_rate_config_for_domain(domain: str, rps: int = 2) -> None:
    cfg = load_rate_config()
    if domain not in cfg:
        cfg[domain] = rps
        save_rate_config(cfg)


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


def _header_stats(targets, subs, assets, summary, eng, paused=False):
    alive = eng.get("active") or eng.get("running")
    eng_txt = "engine up" if alive else "engine down"
    eng_cls = "ok" if alive else "bad"
    pause_badge = '<span class="badge bad">QUEUE PAUSED</span>' if paused else ''
    return (
        f'<span class="hi"><b>{len(targets)}</b> targets</span>'
        f'<span class="hi"><b>{len(subs)}</b> subdomains</span>'
        f'<span class="hi"><b>{len(assets)}</b> assets</span>'
        f'<span class="hi">pending <b>{summary["pending"]}</b></span>'
        f'<span class="hi">ready <b>{summary["ready"]}</b></span>'
        f'<span class="hi">done <b>{summary["completed_total"]}</b></span>'
        f'<span class="badge {eng_cls}">{eng_txt}</span>'
        + pause_badge
    )


def render_page(paths: Paths) -> str:
    targets = load_targets()
    subs = load_subdomains(targets)
    assets = load_assets()
    summary = load_request_summary()
    rate = load_rate_state()
    pend_by_dom = load_pending_by_domain()
    recent_runs = load_recent_runs(paths, n=1200)
    runs = load_bb_runs(paths, recent=recent_runs)
    feed = load_feed(paths, recent=recent_runs)
    eng = engine_status()
    paused = is_paused()
    queue = load_queue()
    qtotals = queue_totals()
    metrics = load_request_metrics()
    crit = load_critical_assets()

    # Activity + Feed consolidated into the single Activity panel (run rows;
    # click a row for its feed lines). Requests + Queue consolidated into the
    # single Requests panel (request data + queue summary + per-item delete).
    panels = (
        render_targets_panel(targets)
        + render_subdomains_panel(subs)
        + render_assets_panel(assets, targets)
        + render_critical_panel(crit)
        + render_requests_panel(summary, queue, qtotals, paused, metrics)
        + render_activity_panel(paths, eng, rate, pend_by_dom, runs)
        + render_rate_panel(rate, pend_by_dom)
    )
    stats = _header_stats(targets, subs, assets, summary, eng, paused)
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
    html = html.replace("__REQVER__", json.dumps(requests_grid_version(summary, queue, qtotals)))
    html = html.replace("__RATEVER__", json.dumps(rate_version(rate, pend_by_dom)))
    html = html.replace("__FEEDVER__", json.dumps(feed_version(feed)))
    html = html.replace("__ACTVER__", json.dumps(activity_version(paths, runs)))
    html = html.replace("__TGTVER__", json.dumps(targets_version(targets)))
    html = html.replace("__SUBVER__", json.dumps(subs_version(subs)))
    html = html.replace("__ISPAUSED__", "true" if paused else "false")
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

        def _sse(self):
            """Server-Sent Events stream: pushes engine events as they happen."""
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"event: hello\ndata: {}\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            q = _SSE.subscribe()
            try:
                while True:
                    try:
                        msg = q.get(timeout=20)
                    except Empty:
                        self.wfile.write(b": ping\n\n")   # heartbeat / dead-client detect
                        self.wfile.flush()
                        continue
                    self.wfile.write(("data: " + json.dumps(msg) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                _SSE.unsubscribe(q)

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
            if parts[:2] == ["api", "stream"]:
                self._sse()
                return
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
                tgts = load_targets()
                # `items` kept for the edit modal; `rows`/`version` feed TargetsVT.
                self._json(200, {"version": targets_version(tgts),
                                 "rows": targets_rows_json(tgts),
                                 "items": tgts})
                return
            if parts[:2] == ["api", "subdomains"]:
                subs = load_subdomains(load_targets())
                self._json(200, {"version": subs_version(subs),
                                 "rows": subs_rows_json(subs)})
                return
            if parts[:2] == ["api", "reactions"]:
                st = load_reactions_state()
                self._json(200, {"paused_all": st["paused_all"],
                                 "tasks": load_reactive_tasks()})
                return
            if parts[:2] == ["api", "assets"]:
                assets = load_assets()
                self._json(200, {"version": assets_version(assets),
                                 "rows": asset_rows_json(assets)})
                return
            if parts[:2] == ["api", "asset-raw"]:
                aid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
                self._json(200, load_asset_raw(unquote(aid)))
                return
            if parts[:2] == ["api", "requests"]:
                summary = load_request_summary()
                completed = load_recent_completed(limit=5000)
                queue = load_queue()
                qtotals = queue_totals()
                self._json(200, {
                    "version": requests_grid_version(summary, queue, qtotals),
                    "rows": requests_grid_rows(queue, request_rows_json(completed)),
                    "counts_html": request_counts_html(summary, qtotals),
                    "paused": is_paused()})
                return
            if parts[:2] == ["api", "queue"]:
                items = load_queue()
                qtotals = queue_totals()
                self._json(200, {"version": queue_version(items, qtotals),
                                 "rows": queue_rows_json(items),
                                 "paused": is_paused()})
                return
            if parts[:2] == ["api", "rate"]:
                rate = load_rate_state()
                pend_by_dom = load_pending_by_domain()
                self._json(200, {"version": rate_version(rate, pend_by_dom),
                                 "rows": rate_rows_json(rate, pend_by_dom)})
                return
            if parts[:2] == ["api", "feed"]:
                feed = load_feed(paths)
                self._json(200, {"version": feed_version(feed),
                                 "rows": feed_rows_json(feed)})
                return
            if parts[:2] == ["api", "activity"]:
                runs = load_bb_runs(paths)
                self._json(200, {"version": activity_version(paths, runs),
                                 "rows": activity_rows_json(paths, runs)})
                return
            if parts[:2] == ["api", "refresh"]:
                tgts = load_targets()
                subs_list = load_subdomains(tgts)
                assets = load_assets()
                summary = load_request_summary()
                rate = load_rate_state()
                pend_by_dom = load_pending_by_domain()
                recent_runs = load_recent_runs(paths, n=1200)
                runs = load_bb_runs(paths, recent=recent_runs)
                feed = load_feed(paths, recent=recent_runs)
                eng = engine_status()
                paused = is_paused()
                queue = load_queue()
                qtotals = queue_totals()

                self._json(200, {
                    "gen_epoch": int(time.time()),
                    "is_paused": paused,
                    "assets_ver": assets_version(assets),
                    "req_ver": requests_grid_version(summary, queue, qtotals),
                    "req_counts_html": request_counts_html(summary, qtotals),
                    "rate_ver": rate_version(rate, pend_by_dom),
                    "feed_ver": feed_version(feed),
                    "activity_ver": activity_version(paths, runs),
                    "tgt_ver": targets_version(tgts),
                    "sub_ver": subs_version(subs_list),
                    "stats": _header_stats(tgts, subs_list, assets, summary, eng, paused),
                })
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
                # /api/targets/<domain>/task/<name>  → run a custom task
                if len(parts) >= 5 and parts[3] == "task":
                    code, res = run_task_for_target(unquote(parts[2]), unquote(parts[4]))
                    self._json(code, res)
                    return
                # /api/targets/<domain>/sub/<host>/task/<name>  → run a sub task
                if len(parts) >= 7 and parts[3] == "sub" and parts[5] == "task":
                    code, res = run_task_for_sub(unquote(parts[2]), unquote(parts[4]), unquote(parts[6]))
                    self._json(code, res)
                    return
                # /api/targets/<domain>/sub/<host>/explore
                if len(parts) >= 6 and parts[3] == "sub" and parts[5] == "explore":
                    code, res = open_explorer(unquote(parts[2]), unquote(parts[4]))
                    self._json(code, res)
                    return
                if len(parts) == 3 and parts[2] == "import":
                    code, res = import_targets(data)
                    self._json(code, res)
                    return
                if len(parts) >= 4 and parts[3] == "probe":
                    code, res = reprobe_target(unquote(parts[2]))
                else:
                    code, res = add_target(data)
                self._json(code, res)
                return
            if parts[:2] == ["api", "runs"] and len(parts) >= 3 and parts[2] == "clear":
                save_cleared_before(paths, time.time())
                self._json(200, {"ok": True})
                return
            if parts[:2] == ["api", "dashboard"]:
                data = self._body() or {}
                if data.get("action") == "restart":
                    restart_dashboard_service()
                    self._json(200, {"ok": True, "message": "restarting"})
                else:
                    self._json(400, {"ok": False, "errors": ["unknown action"]})
                return
            if parts[:3] == ["api", "queue", "pause"]:
                data = self._body() or {}
                paused = bool(data.get("pause", True))
                set_pause(paused)
                self._json(200, {"ok": True, "paused": paused})
                return
            if parts[:2] == ["api", "reactions"]:
                data = self._body() or {}
                action = data.get("action", "")
                task = data.get("task", "")
                st = load_reactions_state()
                if action in ("pause-all", "resume-all"):
                    st["paused_all"] = (action == "pause-all")
                elif action in ("pause", "resume") and task:
                    s = set(st["paused_tasks"])
                    s.add(task) if action == "pause" else s.discard(task)
                    st["paused_tasks"] = sorted(s)
                else:
                    self._json(400, {"ok": False, "errors": ["bad action/task"]})
                    return
                save_reactions_state(st)
                self._json(200, {"ok": True, "paused_all": st["paused_all"],
                                 "tasks": load_reactive_tasks()})
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
            if parts[:2] == ["api", "queue"]:
                data = self._body() or {}
                ids = data.get("ids", [])
                if not isinstance(ids, list):
                    self._json(400, {"ok": False, "errors": ["ids must be a list"]})
                    return
                deleted = delete_queue_items(ids)
                self._json(200, {"ok": True, "deleted": deleted})
                return
            self._json(404, {"errors": ["not found"]})

    return Handler


def serve(paths: Paths, host="127.0.0.1", port=8766, quiet=False):
    threading.Thread(target=_event_watcher, args=(paths,), daemon=True).start()
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
  /* subdomain filter banner */
  .filterbar { display:inline-flex; align-items:center; gap:5px; padding:0 6px;
    height:16px; border-radius:8px; background:#16243d; color:#58a6ff;
    font-size:10px; font-weight:600; }
  .filterbar b { color:#e6edf3; }
  .filterbar .mini { height:14px; line-height:12px; }
  /* per-column resize grips on virtualized grids */
  table.dt th { position: relative; }
  .cgrip { position:absolute; top:0; right:0; width:7px; height:100%;
    cursor:col-resize; user-select:none; z-index:2; }
  .cgrip:hover, .cgrip:active { background:#58a6ff66; }
  table.dt td { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  /* automation (reactive-task) modal */
  .rsub { color:#8b949e; font-size:11px; padding:0 2px 8px; line-height:1.4; }
  .rrow { display:grid; grid-template-columns:48px 1fr auto; align-items:center;
    gap:8px; padding:6px 2px; border-top:1px solid #21262d; }
  .rrow.rdis { opacity:.5; }
  .rrow .rname { color:#e6edf3; font-weight:600; font-size:12px; }
  .rrow .rmeta { color:#6e7681; font-size:10px; font-family:monospace; }
  .rrow .rdesc { grid-column:2/4; color:#8b949e; font-size:10px; margin-top:2px; }
  .toggle { grid-row:1; width:44px; height:20px; border-radius:10px; border:none;
    cursor:pointer; font-size:9px; font-weight:700; color:#fff; background:#6e2230; }
  .toggle.on { background:#1f6f3f; }
  .toggle:disabled { cursor:not-allowed; }
  /* virtualized tables: fixed row height so the spacer math is exact */
  #tbl-assets tbody tr.vrow, #tbl-requests tbody tr.vrow { height:18px; }
  #tbl-assets tbody tr.vrow td, #tbl-requests tbody tr.vrow td {
    height:18px; box-sizing:border-box; overflow:hidden; }
  #tbl-assets tbody tr:nth-child(odd) td,
  #tbl-requests tbody tr:nth-child(odd) td { background:transparent; }
  #tbl-assets tbody tr.vrow.alt td,
  #tbl-requests tbody tr.vrow.alt td { background:rgba(255,255,255,.015); }
  #tbl-assets tbody tr.vsp td, #tbl-requests tbody tr.vsp td {
    padding:0; border:0; background:transparent; }
  #tbl-assets tbody tr.vrow:hover td, #tbl-requests tbody tr.vrow:hover td {
    background:#161b22; }
  #panel-queue { width: 100%; }
  #tbl-queue td:first-child { width: 24px; text-align: center; }
</style>
</head>
<body>
<header>
  <span class="brand">agentC</span>
  <span class="hi mode">bugbounty</span>
  <span id="hstats">__STATS__</span>
  <span class="spacer"></span>
  <span class="engctl" id="engctl">
    <button class="eng eon" id="eng-start" title="start engine">start</button>
    <button class="eng eoff" id="eng-stop" title="stop engine">stop</button>
    <button class="eng" id="eng-restart" title="restart engine">restart</button>
  </span>
  <span class="svcctl" id="svcctl">
    <button class="eng" id="svc-restart-bb" title="restart bugbounty dashboard service">restart dashboard</button>
  </span>
  <label class="pause"><input type="checkbox" id="pause"> pause</label>
  <span class="hi">upd <span id="ago">0s</span></span>
  <button id="reactbtn" title="automation: turn event-reaction tasks (auto enumerate/spider) on or off">&#9889; automation</button>
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

<!-- import targets modal -->
<div class="overlay" id="ioverlay">
  <div class="modal">
    <h3>Import targets</h3>
    <div class="mbody">
      <div class="errs" id="ierrs"></div>
      <div class="frow"><label>domains file</label>
        <div style="display:flex;gap:6px;align-items:center">
          <button class="mini" id="ifile-btn" type="button">Choose file&hellip;</button>
          <span id="ifile-name" style="color:#8b949e;font-size:12px">no file chosen</span>
        </div>
        <input type="file" id="ifile-input" accept=".txt,.csv,.list,text/plain" style="display:none">
        <span class="hint">plain text, one domain per line; blank lines and # comments ignored</span>
      </div>
      <div class="frow"><label for="i_domains">domains</label>
        <textarea id="i_domains" class="mono" rows="8" placeholder="example.com&#10;another.com&#10;# comment lines ignored"></textarea>
      </div>
      <div class="frow"><label for="i_program">program</label>
        <input type="text" id="i_program" placeholder="HackerOne / Bugcrowd program name (applied to all)" spellcheck="false"></div>
      <div class="frow"><label for="i_status">status</label>
        <select id="i_status"></select></div>
      <div id="iresults" style="display:none;margin-top:10px;padding:8px 10px;background:#161b22;border:1px solid #30363d;border-radius:4px;font-size:12px;line-height:1.7"></div>
    </div>
    <div class="mfoot">
      <button class="btn-cancel" id="icancel">Cancel</button>
      <button class="btn-save" id="isave">Import</button>
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

<!-- automation: reactive-task toggles -->
<div class="overlay" id="aroverlay">
  <div class="modal" style="width:560px">
    <h3>Automation &mdash; event-reaction tasks</h3>
    <div class="rsub">Off = the task won't auto-run when its event fires (e.g. adding a target won't auto-enumerate). Ad-hoc runs and scheduled tasks are unaffected. Takes effect immediately.</div>
    <div class="mbody" id="rlist"></div>
    <div class="mfoot">
      <button class="btn-cancel bad" id="rpauseall">Disable all</button>
      <button class="btn-cancel" id="rresumeall">Enable all</button>
      <span style="flex:1"></span>
      <button class="btn-cancel" id="rclose">Close</button>
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

<!-- raw request / response viewer -->
<div class="overlay" id="rawoverlay">
  <div class="modal wide">
    <h3 id="rawdtitle">Request / Response</h3>
    <div class="mbody" style="padding:0;overflow-y:auto;max-height:75vh">
      <div class="rd-sec">Request</div>
      <pre class="rd-out" id="rawreq" style="max-height:28vh;overflow-y:auto">loading…</pre>
      <div class="rd-sec">Response headers</div>
      <pre class="rd-out" id="rawresp" style="max-height:20vh;overflow-y:auto"></pre>
      <div class="rd-sec">Response body <span id="rawbodylabel" style="font-weight:normal;color:#6e7681"></span></div>
      <pre class="rd-out" id="rawbody" style="max-height:30vh;overflow-y:auto"></pre>
    </div>
    <div class="mfoot"><button class="btn-cancel" id="rawclose">Close</button></div>
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

<!-- context menus -->
<div class="ctxmenu" id="ctx-target">
  <div class="ci" data-act="enum-subs">Enumerate Subdomains</div>
</div>
<div class="ctxmenu" id="ctx-sub">
  <div class="ci" data-act="sub-all">Run All Spiders</div>
  <div class="ci" data-act="sub-dom">Run DOM Spider</div>
  <div class="ci" data-act="sub-script">Run Script Spider</div>
  <div class="ci" data-act="sub-critical">Run Critical Asset Scan</div>
  <div class="ci" data-act="sub-explore">Explore Folder</div>
</div>
<div class="toast" id="toast"></div>

<script>
var REFRESH=__REFRESH__, GEN=__GENEPOCH__, STALE=__STALE__, ENGINE_ALIVE=__ENGINEALIVE__, SSE_LIVE=false;
var STATUSES=__STATUSES__, ISPAUSED=__ISPAUSED__;
var modalOpen=false, confirmOpen=false, dragging=false, queueSelecting=false, EMODE='add', EDOMAIN='';
var ctxOpen=false, ctxTarget=null;

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
var VIRTUAL_TABLES={'tbl-assets':1, 'tbl-requests':1, 'tbl-rate':1, 'tbl-feed':1, 'tbl-activity':1, 'tbl-targets':1, 'tbl-subdomains':1};  // every grid is virtualized
document.querySelectorAll('table.dt').forEach(function(tbl){
  if(VIRTUAL_TABLES[tbl.id]) return;  // virtual tables manage their own header sort
  var ths=tbl.tHead.rows[0].cells;
  for(var i=0;i<ths.length;i++){ (function(idx){
    ths[idx].addEventListener('click', function(){
      var cur=L('sort:'+tbl.id)||{}; var dir=(cur.idx===idx && cur.dir==='asc')?'desc':'asc';
      setSort(tbl, idx, dir);
    });
  })(i); }
  var st=L('sort:'+tbl.id);
  if(st && typeof st.idx==='number') setSort(tbl, st.idx, st.dir, true);
  else if(tbl.id==='tbl-feed') setSort(tbl, 0, 'desc');
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

/* ============================================================= *
 *  Virtualized tables (assets, requests) + cross-panel selection *
 * ============================================================= */
var ASSETSVER=__ASSETSVER__, REQVER=__REQVER__, RATEVER=__RATEVER__, FEEDVER=__FEEDVER__, ACTVER=__ACTVER__, TGTVER=__TGTVER__, SUBVER=__SUBVER__;

/* client-side mirrors of the server formatters so look/feel matches */
function fmtSize(n){ n=Number(n); if(!isFinite(n)) return '—';
  var u=['B','K','M','G'], i=0; while(n>=1024 && i<3){ n/=1024; i++; }
  return i===0 ? Math.round(n)+'B' : n.toFixed(1)+u[i]; }
function pad2(x){ return (x<10?'0':'')+x; }
function fmtTs(ts){ ts=Number(ts); if(!ts) return '—';
  var d=new Date(ts*1000);
  return pad2(d.getMonth()+1)+'-'+pad2(d.getDate())+' '+pad2(d.getHours())+':'+pad2(d.getMinutes())+':'+pad2(d.getSeconds()); }
function httpBadge(code){ var s=String(code==null?'':code), cls='mut';
  if(s.charAt(0)==='2') cls='ok'; else if(s.charAt(0)==='3') cls='run';
  else if(s.charAt(0)==='4'||s.charAt(0)==='5') cls='bad';
  return '<span class="badge '+cls+'">'+esc(s||'?')+'</span>'; }
function isControl(el){ return !!(el && el.closest && el.closest('button,a,input,select,textarea,label,.act')); }

/* A virtual table: only the rows in the viewport (+buffer) live in the DOM.
   It owns sorting, text filtering, an optional scope predicate, selection
   and the spacer math. Data is an array of compact row-arrays. */
/* Per-column resizing for a .dt table: a drag grip on each header's right edge.
   Uses a <colgroup> + table-layout:fixed so widths stick across VTable's tbody
   re-renders, and persists per-table/column to localStorage. */
function colResize(tbl, key){
  if(!tbl || !tbl.tHead || !tbl.tHead.rows[0]) return;
  var ths=tbl.tHead.rows[0].cells, n=ths.length;
  var cg=tbl.querySelector('colgroup');
  if(!cg){ cg=document.createElement('colgroup');
    for(var i=0;i<n;i++) cg.appendChild(document.createElement('col'));
    tbl.insertBefore(cg, tbl.firstChild); }
  var cols=cg.children, saved=L('colw:'+key)||{};
  function applyAll(){ for(var i=0;i<n;i++){ if(saved[i]!=null) cols[i].style.width=saved[i]+'px'; } }
  // Seed any unsaved widths from the current auto-layout, then lock to fixed.
  requestAnimationFrame(function(){
    for(var i=0;i<n;i++){ if(saved[i]==null && ths[i].offsetWidth) saved[i]=ths[i].offsetWidth; }
    tbl.style.tableLayout='fixed'; applyAll();
  });
  for(var i=0;i<n;i++){ (function(idx){
    var th=ths[idx];
    if(th.querySelector('.cgrip')) return;
    th.style.position='relative';
    var g=document.createElement('span'); g.className='cgrip'; th.appendChild(g);
    g.addEventListener('click', function(ev){ ev.stopPropagation(); });   // don't sort
    g.addEventListener('mousedown', function(ev){
      ev.preventDefault(); ev.stopPropagation();
      var startX=ev.pageX, startW=(cols[idx].offsetWidth||ths[idx].offsetWidth);
      if(!tbl.style.tableLayout){ tbl.style.tableLayout='fixed'; applyAll(); }
      document.body.style.userSelect='none'; document.body.style.cursor='col-resize';
      function mm(e){ var w=Math.max(36, startW+(e.pageX-startX));
        saved[idx]=w; cols[idx].style.width=w+'px'; }
      function mu(){ document.removeEventListener('mousemove',mm); document.removeEventListener('mouseup',mu);
        document.body.style.userSelect=''; document.body.style.cursor=''; S('colw:'+key, saved); }
      document.addEventListener('mousemove',mm); document.addEventListener('mouseup',mu);
    });
  })(i); }
}

function VTable(opt){
  var tbl=document.getElementById(opt.tableId); if(!tbl) return null;
  var scroll=document.getElementById(opt.scrollId), tb=tbl.tBodies[0], ncol=opt.cols.length;
  var ths=tbl.tHead.rows[0].cells;
  var ss=L('vsort:'+opt.tableId)||opt.sort||{i:-1,dir:'asc'};
  var selectable=(opt.select!==false);
  var V={rows:[], view:[], sort:{i:ss.i, dir:ss.dir}, q:'', scope:null,
         sel:(selectable?L('vsel:'+opt.tableId):null), checked:{}, rowh:18, measured:false};
  function markHdr(){ for(var k=0;k<ths.length;k++) ths[k].classList.remove('asc','desc');
    if(V.sort.i>=0 && ths[V.sort.i]) ths[V.sort.i].classList.add(V.sort.dir); }
  function rebuild(){
    var out=[], q=V.q, sc=V.scope, i;
    for(i=0;i<V.rows.length;i++){ var r=V.rows[i];
      if(sc && !sc(r)) continue;
      if(q && r._s.indexOf(q)<0) continue;
      out.push(r); }
    if(V.sort.i>=0 && opt.cols[V.sort.i] && opt.cols[V.sort.i].get){ var c=opt.cols[V.sort.i], dir=(V.sort.dir==='desc')?-1:1;
      out.sort(function(a,b){ var x=c.get(a), y=c.get(b), rr;
        if(c.num){ rr=(Number(x)||0)-(Number(y)||0); } else { rr=String(x).localeCompare(String(y)); }
        return dir*rr; }); }
    V.view=out; render();
  }
  function render(){
    var total=V.view.length;
    if(total===0){ tb.innerHTML='<tr class="empty"><td colspan="'+ncol+'">— none —</td></tr>';
      if(opt.onCount) opt.onCount(0); return; }
    var h=scroll.clientHeight||300;
    var start=Math.max(0, Math.floor(scroll.scrollTop/V.rowh)-8);
    var end=Math.min(total, start+Math.ceil(h/V.rowh)+16);
    var html='', i, c;
    if(start>0) html+='<tr class="vsp"><td colspan="'+ncol+'" style="height:'+(start*V.rowh)+'px"></td></tr>';
    for(i=start;i<end;i++){ var r=V.view[i], key=opt.key(r), seld=(V.sel!=null && key===V.sel);
      var extra=opt.rowAttrs?opt.rowAttrs(r):'';
      var xcls=opt.rowClass?(' '+opt.rowClass(r)):'';
      html+='<tr class="vrow'+((i&1)?' alt':'')+(seld?' selected':'')+xcls+'" data-key="'+esc(key)+'"'+extra+' tabindex="0">';
      for(c=0;c<ncol;c++){ var col=opt.cols[c];
        if(col.chk){ html+='<td><input type="checkbox" class="vchk"'+(V.checked[key]?' checked':'')+'></td>'; continue; }
        html+='<td>'+(col.render?col.render(r):esc(col.get(r)))+'</td>'; }
      html+='</tr>'; }
    if(end<total) html+='<tr class="vsp"><td colspan="'+ncol+'" style="height:'+((total-end)*V.rowh)+'px"></td></tr>';
    tb.innerHTML=html;
    if(!V.measured){ var s=tb.querySelector('tr.vrow'); if(s){ var rh=s.offsetHeight;
      V.measured=true; if(rh && Math.abs(rh-V.rowh)>=1){ V.rowh=rh; return render(); } } }
    if(opt.onCount) opt.onCount(total);
  }
  for(var i=0;i<ths.length;i++){ (function(idx){ ths[idx].style.cursor='pointer';
    ths[idx].addEventListener('click', function(){
      V.sort=(V.sort.i===idx && V.sort.dir==='asc')?{i:idx,dir:'desc'}:{i:idx,dir:'asc'};
      S('vsort:'+opt.tableId, V.sort); markHdr(); rebuild();
    }); })(i); }
  markHdr();
  colResize(tbl, opt.tableId);
  var raf=0;
  scroll.addEventListener('scroll', function(){ if(raf) return;
    raf=requestAnimationFrame(function(){ raf=0; render(); }); });
  tb.addEventListener('click', function(ev){
    if(isControl(ev.target)) return;             // clicking a link/control never toggles
    var tr=ev.target.closest('tr.vrow'); if(!tr) return;
    var key=tr.getAttribute('data-key');
    if(opt.onRowClick && opt.onRowClick(key, tr, ev)===true) return;  // consumed (e.g. opened a modal)
    if(!selectable) return;
    V.sel=(V.sel===key)?null:key; S('vsel:'+opt.tableId, V.sel); render();
    if(opt.onSelect) opt.onSelect(V.sel, tr);
  });
  if(opt.checkbox){
    tb.addEventListener('change', function(ev){
      if(!ev.target.classList || !ev.target.classList.contains('vchk')) return;
      var tr=ev.target.closest('tr.vrow'); if(!tr) return;
      var key=tr.getAttribute('data-key');
      if(ev.target.checked) V.checked[key]=1; else delete V.checked[key];
      if(opt.onCheck) opt.onCheck(V);
    });
  }
  V.setRows=function(rows){ V.rows=rows||[];
    for(var k=0;k<V.rows.length;k++){ V.rows[k]._s=opt.search(V.rows[k]).toLowerCase(); }
    rebuild(); };
  V.setScope=function(fn){ V.scope=fn; rebuild(); };
  V.setQuery=function(q){ V.q=(q||'').toLowerCase(); rebuild(); };
  V.rerender=render;
  // checkbox helpers (queue)
  V.checkedKeys=function(){ return Object.keys(V.checked); };
  V.clearChecked=function(){ V.checked={}; render(); if(opt.onCheck) opt.onCheck(V); };
  V.clearSel=function(){ V.sel=null; S('vsel:'+opt.tableId, null); render(); };
  // Select-all / none over the *currently filtered view*, restricted to rows
  // that are actually checkable (the chk column's chkIf, if any).
  function _chkCol(){ for(var j=0;j<opt.cols.length;j++){ if(opt.cols[j].chk) return opt.cols[j]; } return null; }
  V.toggleAll=function(){ var col=_chkCol();
    var keys=V.view.filter(function(r){ return !col||!col.chkIf||col.chkIf(r); }).map(opt.key);
    var allOn=keys.length>0 && keys.every(function(k){ return V.checked[k]; });
    V.checked={}; if(!allOn) keys.forEach(function(k){ V.checked[k]=1; });
    render(); if(opt.onCheck) opt.onCheck(V); };
  return V;
}

/* Load rows for a virtual table, caching by version in sessionStorage so an
   unchanged dataset is served instantly on the 5s auto-refresh (no fetch). */
function loadVT(vt, url, version, cacheKey){
  if(!vt) return;
  var cached=null; try{ cached=JSON.parse(sessionStorage.getItem(cacheKey)||'null'); }catch(e){}
  if(cached && cached.version===version){ vt.setRows(cached.rows||[]); return; }
  fetch(url).then(function(r){ return r.json(); }).then(function(d){
    try{ sessionStorage.setItem(cacheKey, JSON.stringify(d)); }catch(e){}
    vt.setRows(d.rows||[]);
  }).catch(function(){ if(cached) vt.setRows(cached.rows||[]); });
}

/* asset row: [target,fetched,type,found_by,status,ctype,size,duration,url,relpath,asset_id,hostname]
   Indices 9-11 are not display columns: relpath (viewer), asset_id (raw modal), hostname (scope). */
var AssetsVT=VTable({ tableId:'tbl-assets', scrollId:'scroll-assets',
  cols:[
    {h:'target',   get:function(r){return r[0];}},
    {h:'fetched',  num:true, get:function(r){return r[1];}, render:function(r){return fmtTs(r[1]);}},
    {h:'type',     get:function(r){return r[2];}},
    {h:'found by', get:function(r){return r[3];}, render:function(r){return esc(r[3]||'—');}},
    {h:'code',     num:true, get:function(r){return r[4];}, render:function(r){return (r[4]===''||r[4]==null)?'—':httpBadge(r[4]);}},
    {h:'ctype',    get:function(r){return r[5];}, render:function(r){return esc(r[5]||'—');}},
    {h:'size',     num:true, get:function(r){return r[6];}, render:function(r){return fmtSize(r[6]);}},
    {h:'duration', get:function(r){return r[7];}, render:function(r){return r[7]||'—';}},
    {h:'url',      get:function(r){return r[8];}, render:function(r){return '<a class="olink" data-asset="'+esc(r[9])+'" title="'+esc(r[8]||'')+'">'+esc(r[8]||'—')+'</a>';}},
    {h:'raw',      get:function(r){return r[10];}, render:function(r){return r[10]?'<button class="mini" data-rawid="'+esc(r[10])+'">raw</button>':'—';}}
  ],
  key:function(r){return r[9];},
  search:function(r){return [r[0],r[2],r[3],r[5],r[8],r[11]].join(' ');},
  sort:{i:1,dir:'desc'},
  onCount:function(n){ var c=document.getElementById('count-assets'); if(c) c.textContent=n; }
});

/* unified request row: [key, kind, status, domain, url, source, when_epoch]
   kind ∈ {pending, ready, done}. Queued rows (pending/ready) are checkbox-
   selectable for deletion; completed (done) rows are not. */
function updateQueueDelBtn(V){
  var n=V?V.checkedKeys().length:0, b=document.getElementById('q-del');
  if(b){ b.disabled=(n===0); b.textContent=n>0?'Delete Selected ('+n+')':'Delete Selected'; }
}
var ReqVT=VTable({ tableId:'tbl-requests', scrollId:'scroll-requests', select:false, checkbox:true,
  cols:[
    {chk:true, chkIf:function(r){return r[1]!=='done';}},
    {h:'status', get:function(r){return r[2];}, render:function(r){
        if(r[1]==='done') return httpBadge(r[2]);
        return r[1]==='ready'?'<span class="badge ok">ready</span>':'<span class="badge run">pending</span>'; }},
    {h:'domain', get:function(r){return r[3];}},
    {h:'url',    get:function(r){return r[4];}, render:function(r){return '<span title="'+esc(r[4])+'">'+esc(String(r[4]).slice(0,80))+'</span>';}},
    {h:'source', get:function(r){return r[5]||'—';}},
    {h:'when',   num:true, get:function(r){return r[6];}, render:function(r){return fmtTs(r[6]);}}
  ],
  key:function(r){return r[0];},
  search:function(r){return [r[1],r[3],r[4],r[5]].join(' ');},
  sort:{i:5,dir:'desc'},
  onCheck:updateQueueDelBtn,
  onCount:function(n){ var c=document.getElementById('count-requests'); if(c) c.textContent=n; }
});
/* Requests grid hides completed (done) rows by default — show only the live
   queue (pending/ready) until the user opts in via the 'show completed' button. */
var reqShowDone = (L('reqshowdone')===true);
function reqScope(){ return reqShowDone ? null : function(r){ return r[1]!=='done'; }; }
if(ReqVT) ReqVT.setScope(reqScope());

/* rate row: [domain, queued, last_epoch] */
var RateVT=VTable({ tableId:'tbl-rate', scrollId:'scroll-rate', select:false,
  cols:[
    {h:'domain',   get:function(r){return r[0];}},
    {h:'queued',   num:true, get:function(r){return r[1];}},
    {h:'last slot',num:true, get:function(r){return r[2];}, render:function(r){return r[2]?fmtTs(r[2]):'—';}},
    {h:'next ready', get:function(r){return r[2];}, render:function(r){
        var last=Number(r[2])||0, now=Date.now()/1000;
        if(r[1] && last>now) return '<span class="runtime">in '+(last-now).toFixed(1)+'s</span>';
        return r[1]?'ready':'—'; }}
  ],
  key:function(r){return r[0];},
  search:function(r){return r[0];},
  sort:{i:1,dir:'desc'},
  onCount:function(n){ var c=document.getElementById('count-rate'); if(c) c.textContent=n; }
});

/* feed row: [time_epoch, task, status, text, level] */
var FEED_LEVEL_ORD={debug:0,info:1,warning:2,error:3};
var feedMinLevel=FEED_LEVEL_ORD[L('feedlevel')]!=null?L('feedlevel'):'debug';
function feedScope(){
  var minv=FEED_LEVEL_ORD[feedMinLevel]||0;
  return function(r){ var lv=FEED_LEVEL_ORD[r[4]]; return lv==null || lv>=minv; };
}
var FeedVT=VTable({ tableId:'tbl-feed', scrollId:'scroll-feed', select:false,
  cols:[
    {h:'time', num:true, get:function(r){return r[0];}, render:function(r){return fmtTs(r[0]);}},
    {h:'task', get:function(r){return r[1];}, render:function(r){return '<span class="badge mut">'+esc(r[1])+'</span>';}},
    {h:'',     get:function(r){return r[2];}, render:function(r){
        var cls={ok:'ok',fail:'bad',running:'run',interrupted:'warn',skipped:'mut'}[r[2]]||'mut';
        return '<span class="badge '+cls+'">●</span>'; }},
    {h:'activity', get:function(r){return r[3];}, render:function(r){return '<span title="'+esc(r[3])+'">'+esc(r[3])+'</span>';}}
  ],
  key:function(r){return r[0]+'|'+r[1]+'|'+r[3];},
  search:function(r){return [r[1],r[3]].join(' ');},
  sort:{i:0,dir:'desc'},
  onCount:function(n){ var c=document.getElementById('count-feed'); if(c) c.textContent=n; }
});

/* activity row: [task, status, started_epoch, finished_epoch, trigger, run_id] */
var ActivityVT=VTable({ tableId:'tbl-activity', scrollId:'scroll-activity', select:false,
  cols:[
    {h:'task',   get:function(r){return r[0];}},
    {h:'status', get:function(r){return r[1];}, render:function(r){
        var cls={success:'ok',failed:'bad',running:'run',interrupted:'warn',skipped:'mut'}[r[1]]||'mut';
        return '<span class="badge '+cls+'">'+esc(r[1]||'?')+'</span>'; }},
    {h:'started',num:true, get:function(r){return r[2];}, render:function(r){return fmtTs(r[2]);}},
    {h:'duration', num:true, get:function(r){return (r[3]||Date.now()/1000)-r[2];}, render:function(r){
        if(r[3]) return ((r[3]-r[2]).toFixed(1))+'s';
        return r[2]?'<span class="runtime" data-since="'+r[2]+'">…</span>':'—'; }},
    {h:'trigger', get:function(r){return r[4];}}
  ],
  key:function(r){return r[5];},
  search:function(r){return [r[0],r[1],r[4]].join(' ');},
  sort:{i:2,dir:'desc'},
  rowAttrs:function(r){return ' data-run="'+esc(r[5])+'"';},
  onRowClick:function(key,tr){ if(key){ openRunDetail(key); return true; } },
  onCount:function(n){ var c=document.getElementById('count-activity'); if(c) c.textContent=n; }
});

function statusBadge(s){ var cls={active:'ok',paused:'warn',archived:'mut'}[s]||'mut';
  return '<span class="badge '+cls+'">'+esc(s||'?')+'</span>'; }
function tagChips(tags){ if(!tags||!tags.length) return '—';
  return tags.map(function(t){ return '<span class="badge mut">'+esc(t)+'</span>'; }).join(' '); }

/* target row: [domain, status, program, tags[], n_sub, n_assets, requested, discovered, created_epoch] */
var TargetsVT=VTable({ tableId:'tbl-targets', scrollId:'scroll-targets',
  cols:[
    {h:'domain',     get:function(r){return r[0];}},
    {h:'status',     get:function(r){return r[1];}, render:function(r){return statusBadge(r[1]);}},
    {h:'program',    get:function(r){return r[2];}, render:function(r){return esc(r[2]||'—');}},
    {h:'tags',       get:function(r){return (r[3]||[]).join(' ');}, render:function(r){return tagChips(r[3]);}},
    {h:'subs',       num:true, get:function(r){return r[4];}},
    {h:'assets',     num:true, get:function(r){return r[5];}},
    {h:'requested',  num:true, get:function(r){return r[6];}},
    {h:'discovered', num:true, get:function(r){return r[7];}},
    {h:'created',    num:true, get:function(r){return r[8];}, render:function(r){return fmtTs(r[8]);}},
    {h:'', get:function(){return '';}, render:function(r){ var d=esc(r[0]);
        return '<span class="act" data-domain="'+d+'">'
          +'<button class="mini" data-act="edit" data-domain="'+d+'">edit</button>'
          +'<button class="mini" data-act="probe" data-domain="'+d+'">re-probe</button>'
          +'<button class="mini bad" data-act="del" data-domain="'+d+'">del</button></span>'; }}
  ],
  key:function(r){return r[0];},
  search:function(r){return [r[0],r[1],r[2],(r[3]||[]).join(' ')].join(' ');},
  sort:{i:0,dir:'asc'},
  rowAttrs:function(r){return ' data-kind="target" data-domain="'+esc(r[0])+'"';},
  onSelect:function(key){ selectTarget(key||null); },
  onCount:function(n){ var c=document.getElementById('count-targets'); if(c) c.textContent=n; }
});

/* subdomain row: [target, hostname, n_assets, html, scripts, css, img, critical, last_epoch] */
var SubsVT=VTable({ tableId:'tbl-subdomains', scrollId:'scroll-subdomains',
  cols:[
    {h:'target',   get:function(r){return r[0];}},
    {h:'hostname', get:function(r){return r[1];}},
    {h:'assets',   num:true, get:function(r){return r[2];}},
    {h:'html',     num:true, get:function(r){return r[3];}},
    {h:'scripts',  num:true, get:function(r){return r[4];}},
    {h:'css',      num:true, get:function(r){return r[5];}},
    {h:'img',      num:true, get:function(r){return r[6];}},
    {h:'critical', num:true, get:function(r){return r[7];}},
    {h:'last seen',num:true, get:function(r){return r[8];}, render:function(r){return fmtTs(r[8]);}}
  ],
  key:function(r){return r[0]+'||'+r[1];},
  search:function(r){return [r[0],r[1]].join(' ');},
  sort:{i:8,dir:'desc'},
  rowAttrs:function(r){return ' data-kind="sub" data-target="'+esc(r[0])+'" data-host="'+esc(r[1])+'"';},
  onSelect:function(key){
    if(key){ var p=key.split('||'); S('sel:sub', key); setAssetScope(p[0], p[1]); }
    else { S('sel:sub', null); var tg=L('sel:target'); setAssetScope(tg||null, tg?'*':null); }
  },
  onCount:function(n){ var c=document.getElementById('count-subdomains'); if(c) c.textContent=n; }
});

/* ---- cross-panel selection: targets ⇒ subdomain filter ⇒ asset scope ---- */
function clearSel(tbl){ if(!tbl) return;
  [].forEach.call(tbl.querySelectorAll('tr.selected'), function(r){ r.classList.remove('selected'); }); }
function findRow(tbl, attrs){ if(!tbl||!tbl.tBodies[0]) return null; var rs=tbl.tBodies[0].rows;
  outer: for(var i=0;i<rs.length;i++){ for(var k in attrs){ if(rs[i].getAttribute(k)!==attrs[k]) continue outer; } return rs[i]; }
  return null; }

function assetScopeFn(target, host){
  if(!target || target==='all') return null;
  return function(r){ return r[0]===target && (host==='*'||!host||r[11]===host); };
}
function setAssetScope(target, host){
  var sel=document.getElementById('assetsel');
  var v=(!target||target==='all')?'all':(target+'||'+(host||'*'));
  if(sel){
    // Sync the dropdown for display only. A subdomain discovered *after* the
    // page first rendered has a row in the Subdomains panel but no <option>
    // here yet; inject one so we never fall back to target||* and silently
    // drop the host filter (the old bug). The real (target,host) always
    // drives the scope below — never the dropdown's post-fallback value.
    var ok=false; for(var i=0;i<sel.options.length;i++){ if(sel.options[i].value===v){ ok=true; break; } }
    if(!ok && v!=='all'){
      var o=document.createElement('option'); o.value=v;
      var pp=v.split('||'); o.textContent='  '+(pp[1]==='*'?pp[0]+' (all)':pp[1]);
      sel.appendChild(o);
    }
    sel.value=v;
  }
  S('assetscope', v);
  if(AssetsVT){ var p=v.split('||'); AssetsVT.setScope(v==='all'?null:assetScopeFn(p[0],p[1])); }
}

/* subdomain filter: scope SubsVT to the selected target; #subq search composes
   on top via SubsVT.setQuery (both applied by the VTable). */
var subFilter=null;
function applySubView(){
  if(SubsVT) SubsVT.setScope(subFilter?function(r){return r[0]===subFilter;}:null);
  var bar=document.getElementById('subfilter');
  if(bar){ if(subFilter){ document.getElementById('subfilterval').textContent=subFilter; bar.style.display=''; }
    else bar.style.display='none'; }
}
function setSubFilter(target){ subFilter=target||null; S('subfilter', subFilter); applySubView(); }

function selectTarget(domain){
  S('sel:target', domain);
  if(domain){ setSubFilter(domain); setAssetScope(domain,'*'); }
  else { setSubFilter(null); setAssetScope(null,null); }
}

/* controls: asset scope dropdown + the three search boxes */
(function(){
  var asel=document.getElementById('assetsel');
  if(asel){
    asel.addEventListener('change', function(){
      var v=asel.value, p=v.split('||'); S('assetscope', v);
      if(TargetsVT) TargetsVT.clearSel(); if(SubsVT) SubsVT.clearSel();
      S('sel:target', null); S('sel:sub', null); setSubFilter(null);
      if(AssetsVT) AssetsVT.setScope(v==='all'?null:assetScopeFn(p[0],p[1]));
    });
    asel.addEventListener('dblclick', function(ev){ ev.stopPropagation(); });
  }
  var aq=document.getElementById('assetq');
  if(aq){ var sq=L('assetq'); if(sq) aq.value=sq;
    aq.addEventListener('input', function(){ S('assetq', aq.value); if(AssetsVT) AssetsVT.setQuery(aq.value); });
    aq.addEventListener('dblclick', function(ev){ ev.stopPropagation(); }); }
  var rq=document.getElementById('reqq');
  if(rq){ var rsq=L('reqq'); if(rsq) rq.value=rsq;
    rq.addEventListener('input', function(){ S('reqq', rq.value); if(ReqVT) ReqVT.setQuery(rq.value); });
    rq.addEventListener('dblclick', function(ev){ ev.stopPropagation(); }); }
  var rateq=document.getElementById('rateq');
  if(rateq){ var rtq=L('rateq'); if(rtq) rateq.value=rtq;
    rateq.addEventListener('input', function(){ S('rateq', rateq.value); if(RateVT) RateVT.setQuery(rateq.value); });
    rateq.addEventListener('dblclick', function(ev){ ev.stopPropagation(); }); }
  var feedq=document.getElementById('feedq');
  if(feedq){ var fqv=L('feedq'); if(fqv) feedq.value=fqv;
    feedq.addEventListener('input', function(){ S('feedq', feedq.value); if(FeedVT) FeedVT.setQuery(feedq.value); });
    feedq.addEventListener('dblclick', function(ev){ ev.stopPropagation(); }); }
  var actq=document.getElementById('actq');
  if(actq){ var aqv=L('actq'); if(aqv) actq.value=aqv;
    actq.addEventListener('input', function(){ S('actq', actq.value); if(ActivityVT) ActivityVT.setQuery(actq.value); });
    actq.addEventListener('dblclick', function(ev){ ev.stopPropagation(); }); }
  var subq=document.getElementById('subq');
  if(subq){ var ssv=L('subq'); if(ssv) subq.value=ssv;
    subq.addEventListener('input', function(){ S('subq', subq.value); if(SubsVT) SubsVT.setQuery(subq.value); });
    subq.addEventListener('dblclick', function(ev){ ev.stopPropagation(); }); }
  var tgtq=document.getElementById('tgtq');
  if(tgtq){ var tqv=L('tgtq'); if(tqv) tgtq.value=tqv;
    tgtq.addEventListener('input', function(){ S('tgtq', tgtq.value); if(TargetsVT) TargetsVT.setQuery(tgtq.value); });
    tgtq.addEventListener('dblclick', function(ev){ ev.stopPropagation(); }); }
  var sfc=document.getElementById('subfilterclear');
  if(sfc) sfc.addEventListener('click', function(ev){ ev.stopPropagation();
    if(TargetsVT) TargetsVT.clearSel(); selectTarget(null); });
})();

/* feed level filter — drives the FeedVT scope predicate */
(function(){
  var sel=document.getElementById('feedlevel'); if(!sel) return;
  sel.value=feedMinLevel;
  if(FeedVT) FeedVT.setScope(feedScope());
  sel.addEventListener('change',function(){
    feedMinLevel=sel.value; S('feedlevel',sel.value);
    if(FeedVT) FeedVT.setScope(feedScope());
  });
  sel.addEventListener('dblclick',function(ev){ ev.stopPropagation(); });
})();

/* restore persisted scope. TargetsVT/SubsVT restore their own row highlight
   from vsel:* in VTable init; here we just re-apply the cross-panel scope. */
(function(){
  var tg=L('sel:target');
  subFilter = tg || L('subfilter') || null;
  applySubView();
  var sb=L('sel:sub');
  if(sb){ var pp=sb.split('||'); setAssetScope(pp[0], pp[1]); }
  else if(tg){ setAssetScope(tg,'*'); }
  else { var as=L('assetscope'); if(as && as!=='all'){ var q=as.split('||'); setAssetScope(q[0],q[1]); } else setAssetScope(null,null); }
})();

loadVT(AssetsVT, '/api/assets', ASSETSVER, 'agentcbb:assets');
loadVT(ReqVT, '/api/requests', REQVER, 'agentcbb:requests');
loadVT(RateVT, '/api/rate', RATEVER, 'agentcbb:rate');
loadVT(FeedVT, '/api/feed', FEEDVER, 'agentcbb:feed');
loadVT(ActivityVT, '/api/activity', ACTVER, 'agentcbb:activity');
loadVT(TargetsVT, '/api/targets', TGTVER, 'agentcbb:targets');
loadVT(SubsVT, '/api/subdomains', SUBVER, 'agentcbb:subdomains');

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

/* ---- automation: event-reaction task toggles ---- */
(function(){
  var aro=document.getElementById('aroverlay'), rlist=document.getElementById('rlist');
  if(!aro) return;
  function closeR(){ aro.style.display='none'; modalOpen=false; schedule(); }
  function render(data){
    var tasks=(data&&data.tasks)||[];
    if(!tasks.length){ rlist.innerHTML='<div class="rsub">no event/file-triggered tasks</div>'; return; }
    rlist.innerHTML=tasks.map(function(t){
      var on=!t.paused;
      return '<div class="rrow'+(t.enabled?'':' rdis')+'">'
        +'<button class="toggle'+(on?' on':'')+'" data-task="'+esc(t.name)+'" data-on="'+(on?1:0)+'" '
          +(t.enabled?'':'disabled title="task is disabled in its config"')+'>'+(on?'ON':'OFF')+'</button>'
        +'<span class="rname">'+esc(t.name)+'</span>'
        +'<span class="rmeta">'+esc(t.trigger)+': '+esc(t.src)+'</span>'
        +(t.desc?'<div class="rdesc">'+esc(t.desc)+'</div>':'')
        +'</div>';
    }).join('');
  }
  function load(){ fetch('/api/reactions').then(function(r){return r.json();}).then(render)
    .catch(function(){ rlist.innerHTML='<div class="rsub">failed to load</div>'; }); }
  function post(body){ return fetch('/api/reactions',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(r){return r.json();}).then(render); }
  document.getElementById('reactbtn').addEventListener('click', function(){
    aro.style.display='flex'; modalOpen=true; if(window._t) clearTimeout(window._t); load(); });
  document.getElementById('rclose').addEventListener('click', closeR);
  document.getElementById('rpauseall').addEventListener('click', function(){ post({action:'pause-all'}); });
  document.getElementById('rresumeall').addEventListener('click', function(){ post({action:'resume-all'}); });
  rlist.addEventListener('click', function(ev){
    var b=ev.target.closest('button.toggle'); if(!b||b.disabled) return;
    var task=b.getAttribute('data-task'), on=b.getAttribute('data-on')==='1';
    post({action: on?'pause':'resume', task:task})
      .then(function(){ toast((on?'Disabled ':'Enabled ')+task); });
  });
  aro.addEventListener('mousedown', function(ev){ if(ev.target===aro) closeR(); });
})();

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

/* ---- import targets modal ---- */
var ioverlay=document.getElementById('ioverlay'), ierrs=document.getElementById('ierrs'),
    iresults=document.getElementById('iresults');
(function(){
  var sel=document.getElementById('i_status');
  STATUSES.forEach(function(s){ var o=document.createElement('option'); o.value=s; o.textContent=s; sel.appendChild(o); });
})();
function openImport(){
  ierrs.style.display='none'; iresults.style.display='none';
  document.getElementById('i_domains').value='';
  document.getElementById('i_program').value='';
  document.getElementById('i_status').value='active';
  document.getElementById('ifile-name').textContent='no file chosen';
  document.getElementById('ifile-input').value='';
  ioverlay.style.display='flex'; modalOpen=true; schedule();
}
function closeImport(){ ioverlay.style.display='none'; modalOpen=false; schedule(); }
document.getElementById('ifile-btn').addEventListener('click', function(){
  document.getElementById('ifile-input').click();
});
document.getElementById('ifile-input').addEventListener('change', function(){
  var f=this.files[0]; if(!f) return;
  document.getElementById('ifile-name').textContent=f.name;
  var reader=new FileReader();
  reader.onload=function(ev){ document.getElementById('i_domains').value=ev.target.result; };
  reader.readAsText(f);
});
document.getElementById('isave').addEventListener('click', function(){
  var content=document.getElementById('i_domains').value.trim();
  if(!content){ ierrs.textContent='Paste or choose a file with domains.'; ierrs.style.display='block'; return; }
  ierrs.style.display='none'; iresults.style.display='none';
  var payload={content:content, program:document.getElementById('i_program').value,
               status:document.getElementById('i_status').value};
  fetch('/api/targets/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(function(r){ return r.json(); })
    .then(function(res){
      var html='';
      if(res.added&&res.added.length) html+='<span style="color:#3fb950">&#10003; Added '+res.added.length+':</span> '+res.added.map(function(d){return escd(d);}).join(', ')+'<br>';
      if(res.skipped&&res.skipped.length) html+='<span style="color:#8b949e">&#8594; Skipped '+res.skipped.length+' (already exist):</span> '+res.skipped.map(function(d){return escd(d);}).join(', ')+'<br>';
      if(res.errors&&res.errors.length) html+='<span style="color:#f85149">&#10007; Invalid '+res.errors.length+':</span> '+res.errors.map(function(d){return escd(d);}).join(', ')+'<br>';
      if(!html) html='<span style="color:#8b949e">Nothing to import.</span>';
      iresults.innerHTML=html; iresults.style.display='block';
      if(res.added&&res.added.length){ document.getElementById('i_domains').value=''; setTimeout(function(){ location.reload(); }, 1200); }
    }).catch(function(e){ ierrs.textContent=String(e); ierrs.style.display='block'; });
});
document.getElementById('icancel').addEventListener('click', closeImport);
ioverlay.addEventListener('mousedown', function(ev){ if(ev.target===ioverlay) closeImport(); });

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
    else if(act==='import-targets') openImport();
    else if(act==='edit') openEdit(domain);
    else if(act==='del') delTarget(domain);
    else if(act==='probe') reprobe(domain);
    else if(act==='clear-runs'){
      fetch('/api/runs/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
        .then(function(r){ return r.json(); })
        .then(function(res){ if(res.ok){ toast('Run history cleared'); setTimeout(function(){ location.reload(); },400); }
          else { toast('Clear failed'); } });
    }
    return;
  }
  var rb=ev.target.closest('button[data-rawid]');
  if(rb){ openAssetRaw(rb.getAttribute('data-rawid')); return; }
  var al=ev.target.closest('a.olink[data-asset]');
  if(al){ ev.preventDefault(); openAsset(al.getAttribute('data-asset')); return; }
  // Row selection (targets/subdomains) and run-detail (activity) are handled by
  // their VTables' onSelect/onRowClick; the delegation above covers data-act
  // buttons, raw-request buttons, and asset links inside virtual rows.
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

/* ---- raw request / response viewer ---- */
var rawoverlay=document.getElementById('rawoverlay');
function statusText(code){ var t={200:'OK',201:'Created',204:'No Content',
  301:'Moved Permanently',302:'Found',304:'Not Modified',400:'Bad Request',
  401:'Unauthorized',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',
  429:'Too Many Requests',500:'Internal Server Error',502:'Bad Gateway',
  503:'Service Unavailable'}; return t[code]||''; }
function openAssetRaw(id){
  if(!id) return; modalOpen=true; schedule();
  document.getElementById('rawdtitle').textContent='Request / Response — '+id;
  document.getElementById('rawreq').textContent='loading…';
  document.getElementById('rawresp').textContent='';
  document.getElementById('rawbody').textContent='';
  document.getElementById('rawbodylabel').textContent='';
  rawoverlay.style.display='flex';
  fetch('/api/asset-raw?id='+encodeURIComponent(id))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d.error){ document.getElementById('rawreq').textContent='Not available: '+d.error; return; }
      var req=d.request||{}, resp=d.response||d;
      // Format raw request
      var rl=[(req.method||'GET')+' '+(req.url||'')+' HTTP/1.1'];
      var rh=req.headers||{}; Object.keys(rh).forEach(function(k){ rl.push(k+': '+rh[k]); });
      if(req.body) rl.push('',req.body);
      document.getElementById('rawreq').textContent=rl.join('\n');
      // Format response status + headers
      var sl=['HTTP/1.1 '+(resp.status_code||'')+' '+statusText(resp.status_code||0)];
      var sh=resp.headers||{}; Object.keys(sh).forEach(function(k){ sl.push(k+': '+sh[k]); });
      document.getElementById('rawresp').textContent=sl.join('\n');
      // Response body
      var bt=d._body_text||resp.body_preview||null;
      document.getElementById('rawbody').textContent=bt||'(no body stored)';
      if(bt) document.getElementById('rawbodylabel').textContent='('+bt.length+' chars shown)';
    })
    .catch(function(e){ document.getElementById('rawreq').textContent='failed: '+e; });
}
function closeAssetRaw(){ rawoverlay.style.display='none'; modalOpen=false; schedule(); }
document.getElementById('rawclose').addEventListener('click', closeAssetRaw);
rawoverlay.addEventListener('mousedown', function(ev){ if(ev.target===rawoverlay) closeAssetRaw(); });

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

/* ---- auto-refresh (AJAX — no full page reload) ---- */
var pause=document.getElementById('pause'); pause.checked=!!L('paused');
var _refreshing=false;

/* Track any user activity so we can defer refreshes during interaction */
var lastActivity=0;
document.addEventListener('mousedown', function(){ lastActivity=Date.now(); }, true);
document.addEventListener('keydown', function(){ lastActivity=Date.now(); }, true);
document.addEventListener('input', function(){ lastActivity=Date.now(); }, true);

function isRecentlyActive(){ return (Date.now()-lastActivity)<1500; }

function schedule(){
  if(window._t) clearTimeout(window._t);
  if(pause.checked||modalOpen||confirmOpen||dragging) return;
  /* When SSE is live, events drive refreshes; the timer is just a slow fallback. */
  window._t=setTimeout(doRefresh, (SSE_LIVE?20:REFRESH)*1000);
}

function doRefresh(){
  window._t=null;
  if(pause.checked||modalOpen||confirmOpen||dragging||isRecentlyActive()){
    window._t=setTimeout(doRefresh,1000); return;
  }
  if(_refreshing) return;
  _refreshing=true;
  fetch('/api/refresh')
    .then(function(r){ return r.json(); })
    .then(function(data){ _refreshing=false; applyRefresh(data); schedule(); })
    .catch(function(){ _refreshing=false; schedule(); });
}

function applyRefresh(data){
  /* header stats */
  var hs=document.getElementById('hstats');
  if(hs && data.stats) hs.innerHTML=data.stats;
  GEN=data.gen_epoch||GEN;
  /* pause state */
  if(data.is_paused!=null){
    ISPAUSED=data.is_paused;
    var qp=document.getElementById('q-pause');
    if(qp){ qp.className='mini'+(ISPAUSED?' bad':''); qp.textContent=ISPAUSED?'Resume queue':'Pause queue'; }
  }
  /* consolidated Requests panel: clean queue-count summary in the header */
  var rc=document.getElementById('req-counts');
  if(rc && data.req_counts_html!=null) rc.innerHTML=data.req_counts_html;
  /* panel bodies */
  var panels=data.panels||{};
  for(var pid in panels) applyPanelBody(pid, panels[pid]);
  /* virtual tables: only re-fetch when server version changed */
  if(data.assets_ver && data.assets_ver!==ASSETSVER){
    ASSETSVER=data.assets_ver; loadVT(AssetsVT,'/api/assets',ASSETSVER,'agentcbb:assets'); }
  if(data.req_ver && data.req_ver!==REQVER){
    REQVER=data.req_ver; loadVT(ReqVT,'/api/requests',REQVER,'agentcbb:requests'); }
  if(data.rate_ver && data.rate_ver!==RATEVER){
    RATEVER=data.rate_ver; loadVT(RateVT,'/api/rate',RATEVER,'agentcbb:rate'); }
  if(data.feed_ver && data.feed_ver!==FEEDVER){
    FEEDVER=data.feed_ver; loadVT(FeedVT,'/api/feed',FEEDVER,'agentcbb:feed'); }
  if(data.activity_ver && data.activity_ver!==ACTVER){
    ACTVER=data.activity_ver; loadVT(ActivityVT,'/api/activity',ACTVER,'agentcbb:activity'); }
  if(data.tgt_ver && data.tgt_ver!==TGTVER){
    TGTVER=data.tgt_ver; loadVT(TargetsVT,'/api/targets',TGTVER,'agentcbb:targets'); }
  if(data.sub_ver && data.sub_ver!==SUBVER){
    SUBVER=data.sub_ver; loadVT(SubsVT,'/api/subdomains',SUBVER,'agentcbb:subdomains'); }
}

function applyPanelBody(panelId, data){
  var panel=document.getElementById('panel-'+panelId); if(!panel) return;
  /* skip if user is typing in this panel */
  var ae=document.activeElement;
  if(ae && panel.contains(ae) && ae!==document.body &&
     (ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'||ae.tagName==='SELECT')) return;
  /* update count badge */
  var cc=panel.querySelector('#count-'+panelId);
  if(cc && data.count!=null) cc.textContent=data.count;
  /* locate table */
  var pbody=panel.querySelector('.pbody');
  var tbl=pbody && pbody.querySelector('table.dt');
  var tbody=tbl && tbl.querySelector('tbody');
  if(!tbody || data.tbody==null) return;
  /* preserve scroll, replace only tbody rows */
  var scrollTop=pbody?pbody.scrollTop:0;
  tbody.innerHTML=data.tbody;
  if(pbody) pbody.scrollTop=scrollTop;
  /* re-apply sort (re-orders the new rows using the saved preference) */
  if(!VIRTUAL_TABLES[tbl.id]){
    var saved=L('sort:'+tbl.id);
    if(saved && typeof saved.idx==='number'){
      sortTable(tbl, saved.idx, saved.dir);
      var ths=tbl.tHead.rows[0].cells;
      for(var i=0;i<ths.length;i++) ths[i].classList.remove('asc','desc');
      if(ths[saved.idx]) ths[saved.idx].classList.add(saved.dir||'asc');
    }
  }
  /* re-apply text filter */
  var fi=panel.querySelector('input.filter[data-t="'+tbl.id+'"]');
  if(fi && fi.value) applyFilter(tbl, fi.value);
  /* panel-specific post-update hooks */
  if(panelId==='subdomains') applySubView();
}

pause.addEventListener('change', function(){ S('paused', pause.checked); schedule(); });
document.getElementById('reload').addEventListener('click', function(){ location.reload(); });

/* ---- queue controls in the consolidated Requests panel (select-all / delete
       act on ReqVT.checked — only queued pending/ready rows are checkable) ---- */
(function(){
  var qAllBtn=document.getElementById('q-all');
  if(qAllBtn) qAllBtn.addEventListener('click', function(){ if(ReqVT) ReqVT.toggleAll(); });
  var rdone=document.getElementById('reqdone');
  function syncDone(){ if(rdone) rdone.textContent=reqShowDone?'hide completed':'show completed';
    if(rdone) rdone.className='mini'+(reqShowDone?' bad':''); }
  syncDone();
  if(rdone) rdone.addEventListener('click', function(){ reqShowDone=!reqShowDone;
    S('reqshowdone', reqShowDone); if(ReqVT) ReqVT.setScope(reqScope()); syncDone(); });
  var qpause=document.getElementById('q-pause');
  if(qpause) qpause.addEventListener('click', function(){
    qpause.disabled=true;
    fetch('/api/queue/pause',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pause:!ISPAUSED})})
      .then(function(r){ return r.json(); })
      .then(function(res){
        toast(res.paused?'Queue paused':'Queue resumed');
        ISPAUSED=res.paused;
        qpause.className='mini'+(ISPAUSED?' bad':'');
        qpause.textContent=ISPAUSED?'Resume queue':'Pause queue';
        qpause.disabled=false;
      })
      .catch(function(e){ toast('Pause failed: '+e); qpause.disabled=false; });
  });
  var qdel=document.getElementById('q-del');
  if(qdel) qdel.addEventListener('click', function(){
    var ids=ReqVT?ReqVT.checkedKeys():[];
    if(!ids.length) return;
    qdel.disabled=true;
    fetch('/api/queue',{method:'DELETE',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ids:ids})})
      .then(function(r){ return r.json(); })
      .then(function(res){
        toast('Deleted '+res.deleted+' queue items');
        if(ReqVT) ReqVT.clearChecked();
        REQVER=null;                               /* force re-fetch next refresh */
        if(window._t) clearTimeout(window._t);
        setTimeout(doRefresh, 300);
      })
      .catch(function(e){ toast('Delete failed: '+e); qdel.disabled=false; });
  });
})();

/* ---- context menus ---- */
(function(){
  var mTarget=document.getElementById('ctx-target'), mSub=document.getElementById('ctx-sub');
  function hide(){ [mTarget, mSub].forEach(function(m){ if(m) m.style.display='none'; }); ctxOpen=false; ctxTarget=null; schedule(); }
  document.addEventListener('contextmenu', function(ev){
    var tr=ev.target.closest('tbody tr'); if(!tr) return;
    var kind=tr.getAttribute('data-kind'), m=null;
    if(kind==='target') m=mTarget; else if(kind==='sub') m=mSub;
    if(!m) return;
    ev.preventDefault(); ctxTarget=tr;
    m.style.left=ev.pageX+'px'; m.style.top=ev.pageY+'px'; m.style.display='block';
    ctxOpen=true; if(window._t) clearTimeout(window._t);
  });
  document.addEventListener('click', function(ev){
    var ci=ev.target.closest('.ci'); if(ci && ctxTarget){
      var act=ci.getAttribute('data-act');
      if(act==='enum-subs'){
        var dom=ctxTarget.getAttribute('data-domain');
        fetch('/api/targets/'+encodeURIComponent(dom)+'/task/subfinder', {method:'POST'}).then(function(r){ return r.json(); })
          .then(function(res){ toast(res.ok?'Subfinder triggered':'Trigger failed'); });
      } else if(act.startsWith('sub-')){
        var dom=ctxTarget.getAttribute('data-target'), host=ctxTarget.getAttribute('data-host');
        if(act==='sub-explore'){
          fetch('/api/targets/'+encodeURIComponent(dom)+'/sub/'+encodeURIComponent(host)+'/explore', {method:'POST'});
        } else {
          var t={'sub-all':'all_spiders','sub-dom':'dom_spider','sub-script':'script_spider','sub-critical':'critical'}[act];
          fetch('/api/targets/'+encodeURIComponent(dom)+'/sub/'+encodeURIComponent(host)+'/task/'+t, {method:'POST'}).then(function(r){ return r.json(); })
            .then(function(res){ toast(res.ok?'Task triggered':'Trigger failed'); });
        }
      }
    }
    hide();
  });
  document.addEventListener('scroll', hide, true);
  window.addEventListener('blur', hide);
})();

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

/* ---- dashboard service restart ---- */
(function(){
  var btn=document.getElementById('svc-restart-bb'); if(!btn) return;
  btn.addEventListener('click',function(){
    btn.disabled=true; var old=btn.textContent; btn.textContent='…';
    fetch('/api/dashboard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'restart'})})
      .then(function(r){ return r.json(); })
      .then(function(res){
        if(res.ok){
          toast('Dashboard restarting…'); btn.textContent='wait…';
          var attempts=0, t=setInterval(function(){
            attempts++;
            if(attempts>30){ clearInterval(t); btn.disabled=false; btn.textContent=old; toast('Restart timed out'); return; }
            fetch('/api/engine').then(function(r){ if(r.ok){ clearInterval(t); toast('Dashboard restarted'); setTimeout(function(){ location.reload(); },300); } }).catch(function(){});
          },2000);
        } else {
          toast('Restart failed: '+(res.message||'error')); btn.disabled=false; btn.textContent=old;
        }
      }).catch(function(e){ toast('Restart error: '+e); btn.disabled=false; btn.textContent=old; });
  });
})();

document.addEventListener('keydown', function(ev){ if(ev.key==='Escape'){
  if(confirmOpen) closeConfirm();
  else if(rawoverlay && rawoverlay.style.display==='flex') closeAssetRaw();
  else if(aoverlay.style.display==='flex') closeAsset();
  else if(roverlay.style.display==='flex') closeRunDetail();
  else if(poverlay.style.display==='flex') closePanelsDlg();
  else if(ioverlay.style.display==='flex') closeImport();
  else if(modalOpen) closeModal();
} });
function tick(){ var age=Math.floor(Date.now()/1000)-GEN; var a=document.getElementById('ago'); if(a) a.textContent=age+'s'+(age>STALE?' STALE':''); }
tick(); setInterval(tick, 1000); schedule();

/* ---- realtime push: subscribe to engine events via SSE (falls back to the
       timer poll above if EventSource is unavailable or the stream drops) ---- */
(function(){
  if(!window.EventSource) return;
  var deb=null;
  function kick(){ if(deb) clearTimeout(deb);
    deb=setTimeout(function(){ deb=null;
      if(_refreshing||modalOpen||confirmOpen||dragging||isRecentlyActive()){ kick(); return; }
      doRefresh();
    }, 500); }
  var es;
  try{ es=new EventSource('/api/stream'); }catch(e){ return; }
  es.onopen=function(){ SSE_LIVE=true; };
  es.onmessage=function(){ kick(); };        // any engine event -> refresh shortly
  es.onerror=function(){ SSE_LIVE=false; };  // EventSource reconnects automatically
})();
</script>
</body>
</html>
"""

PAGE = PAGE.replace("__CSS__", _dashboard_css())


if __name__ == "__main__":
    main()
