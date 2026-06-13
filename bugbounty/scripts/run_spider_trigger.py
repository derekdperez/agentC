#!/usr/bin/env python3
"""On-demand spider trigger, run as an engine task so it produces a RunRecord
(and therefore shows up in the dashboard activity feed).

The dashboard drops a small JSON trigger file into
``bugbounty/requests/spider_queue/`` describing what to run:

    {"task_type": "all_spiders", "hostname": "m.example.com", "domain": "example.com"}

This script reads that file, creates the corresponding HTTP requests in
``bugbounty/requests/pending/`` (always attributing them to the *parent* domain
so assets file under targets/<domain>/<hostname>/), prints a human-readable
summary for the activity feed, then removes the trigger file.
"""
import json
import os
import sys
import time
import uuid

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

PENDING_DIR = "bugbounty/requests/pending"
UA = "Mozilla/5.0 (compatible; bugbounty-spider/1.0)"


def _trigger_path() -> str:
    """Locate the trigger file the engine fired on."""
    p = variables.get("event_path", "")
    if p and os.path.exists(p):
        return p
    name = variables.get("event_filename", "")
    if name:
        cand = os.path.join("bugbounty/requests/spider_queue", name)
        if os.path.exists(cand):
            return cand
    return ""


def _write_request(domain, url, source):
    os.makedirs(PENDING_DIR, exist_ok=True)
    req_id = uuid.uuid4().hex
    request_data = {
        "id": req_id,
        "domain": domain,            # parent target — keeps assets under it
        "url": url,
        "method": "GET",
        "headers": {"User-Agent": UA},
        "body": "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_type": source,
    }
    with open(os.path.join(PENDING_DIR, f"{req_id}.json"), "w") as fh:
        json.dump(request_data, fh, indent=2)


def _root_requests(hostname, domain, source):
    n = 0
    for tmpl in ("http://{}/", "https://{}/"):
        _write_request(domain, tmpl.format(hostname), source)
        n += 1
    return n


def _critical_requests(hostname, domain):
    wordlist = "bugbounty/wordlists/critical_paths.txt"
    if not os.path.exists(wordlist):
        print(f"critical wordlist missing: {wordlist}", file=sys.stderr)
        return 0
    with open(wordlist) as fh:
        paths = [l.strip() for l in fh
                 if l.strip() and not l.startswith("#") and "*" not in l]
    for p in paths:
        _write_request(domain, f"https://{hostname}/{p.lstrip('/')}", "manual_critical")
    return len(paths)


def main():
    trigger = _trigger_path()
    if not trigger:
        print("spider trigger: no trigger file found in context", file=sys.stderr)
        sys.exit(0)

    try:
        with open(trigger) as fh:
            spec = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"spider trigger: unreadable trigger {trigger}: {exc}", file=sys.stderr)
        _cleanup(trigger)
        sys.exit(0)

    task_type = spec.get("task_type", "all_spiders")
    hostname = spec.get("hostname", "")
    domain = spec.get("domain") or hostname
    if not hostname:
        print("spider trigger: no hostname in trigger", file=sys.stderr)
        _cleanup(trigger)
        sys.exit(0)

    label = {"all_spiders": "all spiders", "dom_spider": "DOM spider",
             "script_spider": "script spider", "critical": "critical paths"}.get(
        task_type, task_type)

    if task_type == "all_spiders":
        n = _root_requests(hostname, domain, "manual")
    elif task_type == "dom_spider":
        n = _root_requests(hostname, domain, "manual_dom")
    elif task_type == "script_spider":
        n = _root_requests(hostname, domain, "manual_script")
    elif task_type == "critical":
        n = _critical_requests(hostname, domain)
    else:
        print(f"spider trigger: unknown task_type {task_type!r}", file=sys.stderr)
        _cleanup(trigger)
        sys.exit(0)

    print(f"Queued {label} for {hostname} [{domain}] — {n} request(s)")
    print(f"::set spider_requests_queued={n}")
    _cleanup(trigger)


def _cleanup(trigger):
    try:
        if trigger and os.path.exists(trigger):
            os.remove(trigger)
    except OSError:
        pass


if __name__ == "__main__":
    main()
