#!/usr/bin/env python3
"""Concurrent batch crawler — replaces the per-request ``perform_http_request``
subprocess model.

The old design fired one Python subprocess per request (triggered one-file-at-a
-time by the file watcher). Spawning a fresh interpreter per HTTP request pegs
the CPU and caps throughput at a handful of req/s no matter how big the ready
queue is. This task instead runs on a short schedule, grabs a batch from
``requests/ready/`` and performs them **concurrently in one process** via a
thread pool (``urllib`` releases the GIL during socket I/O, so N threads give N
in-flight requests). A non-blocking flock keeps batches non-overlapping.

Per-request output is byte-for-byte the same as ``perform_http_request.py`` —
the downloaded ``*.body`` asset + ``*.json`` sidecar under
``targets/<domain>/<host>/assets/<type>/`` and the ``completed/<status>/<id>.json``
record — so the spiders / scanners / dashboard downstream are unaffected.
"""
import fcntl
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from pipeline_config import load_limits

REQ_DIR = "bugbounty/requests"
READY = os.path.join(REQ_DIR, "ready")
COMPLETED = os.path.join(REQ_DIR, "completed")
PAUSED = os.path.join(REQ_DIR, "PAUSED")
LOCK = os.path.join(REQ_DIR, "crawler.lock")
TARGETS = "bugbounty/targets"

_LIMITS = load_limits()
# A dedicated, shorter timeout: subfinder turns up many dead subdomains and the
# full per-request timeout on each would dominate throughput. Fail dead hosts
# fast; high concurrency soaks up the rest.
HTTP_TIMEOUT = float(_LIMITS.get("crawler_timeout", 5.0))
CONCURRENCY = int(_LIMITS.get("crawler_concurrency", 400))
BATCH = int(_LIMITS.get("crawler_batch", 1500))
# Cap socket ops globally (belt-and-suspenders with the per-request timeout).
socket.setdefaulttimeout(HTTP_TIMEOUT)


def _asset_type(content_type: str, url: str, source_type: str, status_code: int) -> str:
    ct = (content_type or "").lower()
    if source_type == "critical_probe" and status_code == 200:
        return "critical"
    if "text/html" in ct:
        return "html"
    if "javascript" in ct:
        return "scripts"
    if "css" in ct:
        return "stylesheets"
    if any(t in ct for t in ("image/", "png", "jpg", "jpeg", "gif", "webp")):
        return "images"
    if any(t in ct for t in ("zip", "gzip", "tar", "7z", "archive")):
        return "archives"
    if any(t in ct for t in ("pdf", "octet-stream", "binary")):
        return "bin"
    ext = url.rsplit(".", 1)[-1].lower() if "." in url else ""
    if ext in ("js", "ts", "jsx", "tsx"):
        return "scripts"
    if ext in ("css", "less", "scss"):
        return "stylesheets"
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "svg", "ico"):
        return "images"
    if ext in ("zip", "gz", "tar", "7z", "bz2"):
        return "archives"
    if ext in ("pdf", "doc", "docx", "xls", "xlsx"):
        return "bin"
    return "html"


def _safe_name(parsed) -> str:
    p = parsed.path.lstrip("/")
    if parsed.query:
        p = p + "_" + parsed.query
    name = p.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_") or "root"
    return name[:200]


def _write_json(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2)


def _crawl_one(ready_path: str) -> str:
    """Perform one request and write its outputs. Returns a 1-char status tag."""
    try:
        with open(ready_path) as fh:
            request_data = json.load(fh)
    except (OSError, ValueError):
        try:
            os.remove(ready_path)
        except OSError:
            pass
        return "x"

    req_id = request_data.get("id", "unknown")
    domain = request_data.get("domain", "unknown")
    url = request_data.get("url", "")
    method = request_data.get("method", "GET")
    headers = request_data.get("headers", {}) or {}
    body = request_data.get("body", "") or None
    source_type = request_data.get("source_type", "")

    try:
        start = time.time()
        req = urllib.request.Request(url, data=body if not isinstance(body, str) else body.encode(),
                                     method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status_code = resp.getcode()
            dur = round(time.time() - start, 3)
            resp_headers = dict(resp.headers)
            resp_body = resp.read()

        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.split(":")[0]
        ctype = resp_headers.get("Content-Type", "")
        atype = _asset_type(ctype, url, source_type, status_code)
        adir = os.path.join(TARGETS, domain, host, "assets", atype)
        os.makedirs(adir, exist_ok=True)

        base = _safe_name(parsed)
        body_file = os.path.join(adir, base + ".body")
        with open(body_file, "wb") as fh:
            fh.write(resp_body)
        result = {"id": req_id, "url": url, "status_code": status_code,
                  "content_type": ctype, "headers": resp_headers, "body_file": body_file,
                  "content_length": len(resp_body), "requested_at": datetime.now().isoformat(),
                  "source_type": source_type, "duration_s": dur}
        _write_json(os.path.join(adir, base + ".json"), result)

        cdir = os.path.join(COMPLETED, str(status_code))
        os.makedirs(cdir, exist_ok=True)
        _write_json(os.path.join(cdir, req_id + ".json"),
                    {"request": request_data, "response": result})
        _rm(ready_path)
        return "2" if 200 <= status_code < 300 else "h"

    except urllib.error.HTTPError as exc:
        status_code = exc.getcode() or 0
        try:
            preview = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            preview = ""
        cdir = os.path.join(COMPLETED, str(status_code))
        os.makedirs(cdir, exist_ok=True)
        _write_json(os.path.join(cdir, req_id + ".json"),
                    {"request": request_data,
                     "response": {"id": req_id, "url": url, "status_code": status_code,
                                  "error": str(exc), "body_preview": preview}})
        _rm(ready_path)
        return "h"
    except Exception as exc:  # noqa: BLE001 — network/timeout/dns etc.
        cdir = os.path.join(COMPLETED, "error")
        os.makedirs(cdir, exist_ok=True)
        _write_json(os.path.join(cdir, req_id + ".error.json"),
                    {"request": request_data, "error": str(exc)})
        _rm(ready_path)
        return "e"


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    if os.path.exists(PAUSED):
        print("queue paused — crawler idle")
        return
    os.makedirs(READY, exist_ok=True)

    lock_fd = open(LOCK, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        print("crawler already running — skipping tick")
        return

    try:
        try:
            names = [n for n in os.listdir(READY) if n.endswith(".json")]
        except FileNotFoundError:
            names = []
        names = names[:BATCH]
        if not names:
            return
        paths = [os.path.join(READY, n) for n in names]
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            tags = list(ex.map(_crawl_one, paths))
        dt = time.time() - t0
        ok = tags.count("2")
        print(f"crawler: did {len(tags)} in {dt:.1f}s "
              f"({len(tags)/dt:.0f}/s) ok2xx={ok} concurrency={CONCURRENCY}")
        print(f"::set crawler_done={len(tags)}")
        print(f"::set crawler_rate={round(len(tags)/dt, 1) if dt else 0}")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
