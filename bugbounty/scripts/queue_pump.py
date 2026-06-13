#!/usr/bin/env python3
"""Promote requests from pending/ to ready/ — deduplicating and rate-limiting.

This single scheduled task replaces the old per-file ``bugbounty-dedup`` and
``bugbounty-rate-orchestrator`` watcher tasks. Those fired a *fresh Python
subprocess for every request file*, and the engine spawned an unbounded thread
(holding two stdio pipes in the engine process) per event. A backlog of tens of
thousands of requests therefore exhausted the engine's file-descriptor limit
("too many open files"), rewrote each target's whole ``state.json`` once per
request (O(n^2) I/O), and slept a worker thread up to 30s inside the limiter.

The pump does that work in one process, in two phases per tick:

  Phase 1 — **intake & dedup.** Read each new file dropped in the flat
  ``pending/`` dir *once*: drop duplicates (in-memory set per target, O(1)),
  record the URL in ``state.json``, and file the request into a per-host bucket
  ``pending/<hostname>/``. Each request is read exactly once in its lifetime.

  Phase 2 — **fair, rate-limited promotion.** For *every* host bucket, refill a
  per-host token bucket (default 2 req/s, tunable in ``limits.json``) and move up
  to that many requests to ``ready/`` — a cheap ``listdir`` + ``rename``, no file
  re-reads. Because promotion visits every bucket, a host with thousands of
  queued URLs (e.g. media.cnn.com) can't starve the long tail: N subdomains run
  concurrently at N x 2 req/s.

A non-blocking flock makes ticks non-overlapping (a slow tick is skipped, not
stacked). Designed to run on a short interval (~1s).
"""
import fcntl
import json
import os
import sys
import time
from urllib.parse import urlparse, urlunparse

from pipeline_config import load_limits

REQ_DIR = "bugbounty/requests"
PENDING = os.path.join(REQ_DIR, "pending")
READY = os.path.join(REQ_DIR, "ready")
RATE_CONFIG = os.path.join(REQ_DIR, "rate_config.json")
RATE_STATE = os.path.join(REQ_DIR, "rate_state.json")
HOST_DOMAINS = os.path.join(REQ_DIR, "host_domains.json")  # host -> apex domain cache
PUMP_LOCK = os.path.join(REQ_DIR, "pump.lock")
PAUSED = os.path.join(REQ_DIR, "PAUSED")
TARGETS = "bugbounty/targets"

NO_HOST = "_nohost_"


def _load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return default


def _atomic_write(path, data):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _norm(url):
    """Canonical form for dedup: scheme+host+path, no query/fragment."""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, "", "", ""))
    except Exception:
        return url


def _bucket_name(host):
    """A safe per-host directory name (hostnames have no '/'; guard anyway)."""
    return (host or NO_HOST).replace("/", "_") or NO_HOST


def main():
    if os.path.exists(PAUSED):
        print("queue paused — pump idle")
        return

    os.makedirs(PENDING, exist_ok=True)
    os.makedirs(READY, exist_ok=True)

    # Single-instance guard: if a previous tick is still running, skip this one.
    lock_fd = open(PUMP_LOCK, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        print("pump already running — skipping tick")
        return

    try:
        _run_tick()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _run_tick():
    now = time.time()
    limits = load_limits()
    default_rate = limits["default_rate_per_host"]
    burst = limits["burst"]
    domain_rate = limits["default_rate_per_domain"]
    domain_burst = limits["domain_burst"]
    system_rate = limits["default_rate_per_system"]
    system_burst = limits["system_burst"]
    batch = limits["pump_batch"]
    rate_config = _load_json(RATE_CONFIG, {})
    rate_state = _load_json(RATE_STATE, {})  # {host|@dom|@system: {tokens, last}}
    if not isinstance(rate_state, dict):
        rate_state = {}
    # host -> apex-domain cache so promotion never re-reads a file per bucket.
    hostdom = _load_json(HOST_DOMAINS, {})
    if not isinstance(hostdom, dict):
        hostdom = {}
    hd_before = len(hostdom)

    intake, duplicates = _phase_intake(batch, hostdom)
    promoted, deferred = _phase_promote(now, default_rate, burst, domain_rate,
                                        domain_burst, system_rate, system_burst,
                                        rate_config, rate_state, hostdom)
    if len(hostdom) != hd_before:               # persist cache once new hosts appeared
        try:
            _atomic_write(HOST_DOMAINS, hostdom)
        except OSError:
            pass

    try:
        _atomic_write(RATE_STATE, rate_state)
    except OSError as exc:
        print(f"rate_state write failed: {exc}", file=sys.stderr)

    if intake or promoted or duplicates or deferred:
        print(f"pump: intake={intake} promoted={promoted} "
              f"dup={duplicates} deferred={deferred}")
    print(f"::set pump_intake={intake}")
    print(f"::set pump_promoted={promoted}")
    print(f"::set pump_duplicates={duplicates}")
    print(f"::set pump_deferred={deferred}")


def _phase_intake(batch, hostdom):
    """Read new flat pending/ files once; dedup and file into per-host buckets.
    Records each host's apex domain into ``hostdom`` so promotion never has to
    re-read a request file just to learn the domain."""
    try:
        entries = sorted(os.listdir(PENDING))
    except FileNotFoundError:
        return 0, 0
    names = [n for n in entries if n.endswith(".json")][:batch]
    if not names:
        return 0, 0

    domain_state = {}   # domain -> dict(state.json)
    domain_seen = {}    # domain -> set(requested_paths)
    domain_dirty = set()

    def _state_for(domain):
        if domain not in domain_state:
            st = _load_json(os.path.join(TARGETS, domain, "state.json"), None)
            if not isinstance(st, dict):
                st = {"domain": domain, "requested_urls": [],
                      "requested_paths": [], "discovered_urls": []}
            domain_state[domain] = st
            domain_seen[domain] = set(st.get("requested_paths", []) or [])
        return domain_state[domain], domain_seen[domain]

    intake = duplicates = 0
    for name in names:
        src = os.path.join(PENDING, name)
        req = _load_json(src, None)
        url = req.get("url", "") if isinstance(req, dict) else ""
        if not url:                          # unreadable/partial/garbage — drop
            _safe_remove(src)
            continue
        domain = req.get("domain") or "unknown"
        st, seen = _state_for(domain)
        norm = _norm(url)

        if norm in seen:                     # duplicate — never request twice
            duplicates += 1
            _safe_remove(src)
            continue

        host = urlparse(url).netloc or domain
        hostdom[_bucket_name(host)] = domain     # cache host bucket -> apex domain
        bucket = os.path.join(PENDING, _bucket_name(host))
        os.makedirs(bucket, exist_ok=True)
        try:
            os.replace(src, os.path.join(bucket, name))
        except FileNotFoundError:
            continue

        seen.add(norm)
        ru = set(st.get("requested_urls", []) or []); ru.add(url)
        du = set(st.get("discovered_urls", []) or []); du.add(url)
        st["requested_paths"] = sorted(seen)
        st["requested_urls"] = sorted(ru)
        st["discovered_urls"] = sorted(du)
        domain_dirty.add(domain)
        intake += 1

    for domain in domain_dirty:
        sf = os.path.join(TARGETS, domain, "state.json")
        os.makedirs(os.path.dirname(sf), exist_ok=True)
        try:
            _atomic_write(sf, domain_state[domain])
        except OSError as exc:
            print(f"state write failed for {domain}: {exc}", file=sys.stderr)

    return intake, duplicates


_DOMAIN_KEY = "@domain::"   # rate_state namespace for per-domain aggregate buckets


def _bucket_domain(bdir, files):
    """The apex/target domain a host bucket belongs to, read from a request's
    authoritative ``domain`` field (one cheap read per non-empty bucket/tick)."""
    req = _load_json(os.path.join(bdir, files[0]), None)
    if isinstance(req, dict) and req.get("domain"):
        return req["domain"]
    return None


def _refill(state, key, rps, cap, now):
    st = state.get(key) or {}
    return min(cap, st.get("tokens", rps) + (now - st.get("last", now)) * rps)


_SYSTEM_KEY = "@system"     # rate_state key for the global system-wide bucket


def _phase_promote(now, default_rate, burst, domain_rate, domain_burst,
                   system_rate, system_burst, rate_config, rate_state, hostdom):
    """Promote pending → ready honoring THREE token buckets:

      * **per host (subdomain):** ``default_rate_per_host`` (polite, default 2/s)
      * **per apex domain:** ``default_rate_per_domain`` aggregate
      * **per system (global):** ``default_rate_per_system`` ceiling across all

    The host→domain map (``hostdom``) means we never re-read a request file just
    to learn a bucket's domain — that per-bucket read was the throughput killer
    at thousands of buckets. Within a domain, promotion is round-robin across its
    host buckets so the domain ceiling is shared fairly."""
    try:
        buckets = [d for d in os.listdir(PENDING)
                   if os.path.isdir(os.path.join(PENDING, d))]
    except FileNotFoundError:
        return 0, 0

    work, by_domain = [], {}
    for bucket in buckets:
        host = bucket  # bucket dir name IS the hostname (see _bucket_name)
        bdir = os.path.join(PENDING, bucket)
        try:
            files = sorted(f for f in os.listdir(bdir) if f.endswith(".json"))
        except FileNotFoundError:
            continue
        if not files:
            try:
                os.rmdir(bdir)            # tidy a drained host bucket
            except OSError:
                pass
            continue
        domain = hostdom.get(host) or _bucket_domain(bdir, files) or host
        hostdom.setdefault(host, domain)        # backfill cache for pre-existing buckets
        rps = max(float(rate_config.get(host, default_rate)), 0.01)
        e = {"host": host, "bdir": bdir, "files": files, "idx": 0, "moved": 0,
             "htokens": _refill(rate_state, host, rps, burst, now)}
        work.append(e)
        by_domain.setdefault(domain, []).append(e)

    # Refill each domain's aggregate bucket + the global system bucket once.
    dtokens = {}
    for domain in by_domain:
        drps = max(float(rate_config.get(_DOMAIN_KEY + domain, domain_rate)), 0.01)
        dtokens[domain] = _refill(rate_state, _DOMAIN_KEY + domain, drps,
                                  domain_burst, now)
    stokens = _refill(rate_state, _SYSTEM_KEY, max(system_rate, 0.01),
                      system_burst, now)

    promoted = 0
    for domain, entries in by_domain.items():
        if stokens < 1:
            break
        progressing = True
        while dtokens[domain] >= 1 and stokens >= 1 and progressing:
            progressing = False
            for e in entries:
                if dtokens[domain] < 1 or stokens < 1:
                    break
                if e["htokens"] < 1 or e["idx"] >= len(e["files"]):
                    continue
                name = e["files"][e["idx"]]
                e["idx"] += 1
                try:
                    os.replace(os.path.join(e["bdir"], name),
                               os.path.join(READY, name))
                except FileNotFoundError:
                    continue          # file vanished (deduped/raced) — no token spent
                e["moved"] += 1
                e["htokens"] -= 1
                dtokens[domain] -= 1
                stokens -= 1
                promoted += 1
                progressing = True

    deferred = 0
    for e in work:
        rate_state[e["host"]] = {"tokens": e["htokens"], "last": now}
        deferred += max(0, len(e["files"]) - e["moved"])
    for domain, toks in dtokens.items():
        rate_state[_DOMAIN_KEY + domain] = {"tokens": toks, "last": now}
    rate_state[_SYSTEM_KEY] = {"tokens": stokens, "last": now}

    return promoted, deferred


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
