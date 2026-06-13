#!/usr/bin/env python3
"""Check if URL was already requested, update domain state.

This script is the sole authoritative writer of a target's ``requested_urls``.
Many instances run concurrently (one per request file), so the read-modify-write
of ``state.json`` is guarded by a per-domain exclusive lock (``fcntl.flock``) and
persisted with an atomic temp-file + ``os.replace`` swap. Without this, parallel
instances interleave their byte-level writes and leave a torn file behind
(a valid document followed by an older, longer write's tail), and silently lose
each other's updates.
"""
import fcntl
import json
import os
import sys
from urllib.parse import urlparse, urlunparse

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

event_path = variables.get("event_path")
event_filename = variables.get("event_filename")

if not event_path or not event_filename:
    print("Error: event_path or event_filename not found", file=sys.stderr)
    sys.exit(1)

if not event_path.endswith(".json"):
    print(f"Skipping non-JSON file: {event_filename}")
    sys.exit(0)

if not os.path.exists(event_path):
    print(f"Request file not found: {event_path}", file=sys.stderr)
    sys.exit(1)

with open(event_path, "r") as f:
    request_data = json.load(f)

domain = request_data.get("domain", "")
url = request_data.get("url", "")
req_id = request_data.get("id", "")

if not domain or not url:
    print("Error: domain or url not found in request", file=sys.stderr)
    sys.exit(1)

target_dir = f"bugbounty/targets/{domain}"
state_file = f"{target_dir}/state.json"
lock_file = f"{target_dir}/state.lock"
os.makedirs(target_dir, exist_ok=True)


def normalize_url(u: str) -> str:
    """Strip query string and fragment for dedup — URLs differing only by
    parameters (e.g. ?locale=en vs ?locale=de) are treated as duplicates."""
    try:
        p = urlparse(u)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, "", "", ""))
    except Exception:
        return u


def load_state(path):
    """Read state.json, salvaging a torn file left by a past unlocked write.

    A corrupt file is a valid JSON document followed by stale trailing bytes;
    ``raw_decode`` recovers the leading (complete) document.
    """
    try:
        with open(path, "r") as fh:
            raw = fh.read()
    except FileNotFoundError:
        raw = ""
    if not raw.strip():
        return {"domain": domain, "requested_urls": [], "discovered_urls": []}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            obj, _ = json.JSONDecoder().raw_decode(raw.lstrip())
            print(f"Recovered torn state.json for {domain}", file=sys.stderr)
            return obj
        except json.JSONDecodeError:
            print(f"Unreadable state.json for {domain}; reinitialising",
                  file=sys.stderr)
            return {"domain": domain, "requested_urls": [], "discovered_urls": []}


def atomic_write(path, data):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# Hold an exclusive per-domain lock across the whole read-modify-write so
# concurrent instances serialise instead of clobbering each other.
with open(lock_file, "w") as lockf:
    fcntl.flock(lockf, fcntl.LOCK_EX)

    state = load_state(state_file)
    state.setdefault("domain", domain)
    requested_urls = set(state.get("requested_urls", []) or [])
    requested_paths = set(state.get("requested_paths", []) or [])
    norm_url = normalize_url(url)

    if url in requested_urls or norm_url in requested_paths:
        duplicate = True
    else:
        duplicate = False
        requested_urls.add(url)
        requested_paths.add(norm_url)
        state["requested_urls"] = sorted(requested_urls)
        state["requested_paths"] = sorted(requested_paths)

        discovered = set(state.get("discovered_urls", []) or [])
        discovered.add(url)
        state["discovered_urls"] = sorted(discovered)

        atomic_write(state_file, state)
    # Lock released when the with-block closes.

if duplicate:
    print(f"Duplicate request detected, removing: {url}")
    try:
        os.remove(event_path)
    except FileNotFoundError:
        pass
    print(f"::set url={url}")
    print("::set duplicate=true")
else:
    print(f"Tracked URL: {url}")
    print(f"::set url={url}")
    print("::set duplicate=false")
