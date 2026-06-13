#!/usr/bin/env python3
"""Scan HTTP response body for sensitive data patterns from critical_body_regex_patterns.txt.

Each new body file triggers this script. Matches are saved as individual finding
JSON records under bugbounty/findings/{target}/. Deduplication prevents re-reporting
the same (url, pattern) pair — tracked via a locked index file.
"""
import fcntl
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

event_path = variables.get("event_path")
event_filename = variables.get("event_filename")

if not event_path or not event_filename:
    print("Error: event_path or event_filename not found", file=sys.stderr)
    sys.exit(1)

if event_filename.endswith(".json"):
    print(f"Skipping JSON sidecar: {event_filename}")
    sys.exit(0)

if not os.path.exists(event_path):
    print(f"Body file not found: {event_path}", file=sys.stderr)
    sys.exit(1)

# Skip binary asset types — patterns only make sense in text content
norm_path = event_path.replace("\\", "/")
skip_dirs = ("/assets/images/", "/assets/archives/", "/assets/bin/")
if any(d in norm_path for d in skip_dirs):
    print(f"Skipping binary asset: {event_filename}")
    sys.exit(0)

# Load patterns
wordlist_file = "bugbounty/wordlists/critical_body_regex_patterns.txt"
if not os.path.exists(wordlist_file):
    print(f"Patterns file not found: {wordlist_file}", file=sys.stderr)
    sys.exit(1)

with open(wordlist_file, "r") as fh:
    raw_patterns = [line.strip() for line in fh
                    if line.strip() and not line.startswith("#")]

# Pre-compile patterns, skipping invalid ones
compiled = []
for pat in raw_patterns:
    try:
        compiled.append((pat, re.compile(pat)))
    except re.error as err:
        print(f"Skipping invalid pattern {pat!r}: {err}", file=sys.stderr)

if not compiled:
    print("No valid patterns loaded", file=sys.stderr)
    sys.exit(1)

# Extract target + hostname from path structure:
# bugbounty/targets/{root_domain}/{full_hostname}/assets/{type}/{filename}
parts = norm_path.split("/")
root_domain = "unknown"
full_hostname = "unknown"
try:
    ti = parts.index("targets")
    root_domain = parts[ti + 1]
    full_hostname = parts[ti + 2]
except (ValueError, IndexError):
    pass

# Load sidecar to get the original URL
sidecar_path = event_path[:-5] + ".json"
url = ""
if os.path.exists(sidecar_path):
    try:
        with open(sidecar_path) as fh:
            url = json.load(fh).get("url", "")
    except Exception:
        pass

# Read body as text — skip if binary (null bytes)
try:
    with open(event_path, "r", encoding="utf-8", errors="replace") as fh:
        # Cap at 512KB to avoid ReDoS on huge files
        content = fh.read(524288)
except Exception as err:
    print(f"Failed to read body: {err}", file=sys.stderr)
    sys.exit(1)

if "\x00" in content[:1024]:
    print(f"Skipping binary content: {event_filename}")
    sys.exit(0)

# Set up findings + dedup index under bugbounty/findings/{target}/
findings_dir = os.path.join("bugbounty", "findings", root_domain)
os.makedirs(findings_dir, exist_ok=True)

index_file = os.path.join(findings_dir, "seen_index.json")
lock_file = os.path.join(findings_dir, "seen_index.lock")


def load_seen(path):
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
            return set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(path, seen_set):
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(sorted(seen_set), fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def seen_key(u, pattern):
    """Compact hash so the index file stays small regardless of pattern length."""
    raw = f"{u}\x00{pattern}"
    return hashlib.sha1(raw.encode()).hexdigest()


found_count = 0

with open(lock_file, "w") as lockf:
    fcntl.flock(lockf, fcntl.LOCK_EX)
    seen = load_seen(index_file)
    new_keys = set()

    for pattern_str, pattern_re in compiled:
        key = seen_key(url or event_path, pattern_str)
        if key in seen:
            continue

        try:
            m = pattern_re.search(content)
        except Exception:
            continue
        if not m:
            continue

        # Count all matches (capped to avoid huge lists)
        all_matches = pattern_re.findall(content)
        match_count = len(all_matches)
        first_match = str(all_matches[0]) if all_matches else m.group(0)

        # Capture context window around first match
        ctx_start = max(0, m.start() - 120)
        ctx_end = min(len(content), m.end() + 120)
        context_snippet = content[ctx_start:ctx_end]

        finding = {
            "id": uuid.uuid4().hex,
            "target": root_domain,
            "hostname": full_hostname,
            "url": url,
            "asset_path": event_path,
            "pattern": pattern_str,
            "match_count": match_count,
            "first_match": first_match[:500],
            "context": context_snippet,
            "detected_at": datetime.now().isoformat(),
        }

        finding_file = os.path.join(findings_dir, f"{finding['id']}.json")
        with open(finding_file, "w") as fh:
            json.dump(finding, fh, indent=2)

        new_keys.add(key)
        found_count += 1
        print(f"MATCH [{pattern_str[:60]}] in {url or event_filename}")

    if new_keys:
        seen |= new_keys
        save_seen(index_file, seen)

if found_count:
    print(f"Found {found_count} sensitive pattern matches in {event_filename}")
    print(f"Findings saved to: {findings_dir}")
else:
    print(f"No sensitive patterns matched in {event_filename}")

print(f"::set matches_found={found_count}")
print(f"::set url={url}")
print(f"::set target={root_domain}")
