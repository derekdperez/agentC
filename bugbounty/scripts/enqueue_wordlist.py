#!/usr/bin/env python3
"""Enqueue unchecked top100_common_paths.txt entries for every discovered host."""
import json
import os
import sys
import uuid
from urllib.parse import urlparse, urlunparse

from pipeline_config import load_limits

WORDLIST_PATH = os.environ.get(
    "AGENTC_WORDLIST_PATH",
    "bugbounty/wordlists/top100_common_paths.txt"
)

PENDING = "bugbounty/requests/pending"
TARGETS = "bugbounty/targets"


def _load_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return default


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _norm(url):
    try:
        p = urlparse(url)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, "", "", ""))
    except Exception:
        return url


def _load_wordlist(path):
    if not os.path.exists(path):
        print(f"Wordlist not found: {path}", file=sys.stderr)
        return []
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def _collect_hosts(state, domain):
    hosts = {domain}
    for url in state.get("requested_urls", []):
        netloc = urlparse(url).netloc
        if netloc:
            hosts.add(netloc.split(":")[0])
    return hosts


def main():
    wordlist = _load_wordlist(WORDLIST_PATH)
    if not wordlist:
        print(f"No wordlist entries loaded from {WORDLIST_PATH}", file=sys.stderr)
        sys.exit(1)

    domains = [d for d in os.listdir(TARGETS)
               if os.path.isdir(os.path.join(TARGETS, d)) and d != "queue"]

    total_enqueued = 0
    total_skipped = 0

    for domain in sorted(domains):
        state_file = os.path.join(TARGETS, domain, "state.json")
        state = _load_json(state_file)
        if not state:
            continue

        requested_paths = set(state.get("requested_paths", []) or [])
        requested_urls = set(state.get("requested_urls", []) or [])
        hosts = _collect_hosts(state, domain)

        domain_enqueued = 0
        domain_skipped = 0
        dirty = False

        for host in sorted(hosts):
            for entry in wordlist:
                path_part = entry.rstrip("/")
                if not path_part or path_part.startswith("."):
                    domain_skipped += 1
                    continue

                url = f"https://{host}/{path_part}"
                norm = _norm(url)

                if norm in requested_paths:
                    domain_skipped += 1
                    continue

                request_file = os.path.join(PENDING, f"{uuid.uuid4().hex}.json")
                request_data = {
                    "id": uuid.uuid4().hex,
                    "domain": domain,
                    "url": url,
                    "method": "GET",
                    "headers": {
                        "User-Agent": "Mozilla/5.0 (compatible; bugbounty-spider/1.0)"
                    },
                    "body": "",
                    "created_at": os.popen("date -Iseconds").read().strip(),
                    "source_type": "wordlist_spider"
                }

                os.makedirs(PENDING, exist_ok=True)
                with open(request_file, "w") as f:
                    json.dump(request_data, f, indent=2)

                requested_paths.add(norm)
                requested_urls.add(url)
                domain_enqueued += 1
                dirty = True

        if dirty:
            state["requested_paths"] = sorted(requested_paths)
            state["requested_urls"] = sorted(requested_urls)
            _atomic_write(state_file, state)

        print(f"wordlist-spider: domain={domain} enqueued={domain_enqueued} "
              f"skipped={domain_skipped} hosts={len(hosts)}")
        total_enqueued += domain_enqueued
        total_skipped += domain_skipped

    print(f"::set total_enqueued={total_enqueued}")
    print(f"::set total_skipped={total_skipped}")
    print(f"::set domains_scanned={len(domains)}")


if __name__ == "__main__":
    main()