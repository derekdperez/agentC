#!/usr/bin/env python3
"""Create HTTP requests for every critical path against a newly added domain."""
import json
import os
import sys
import uuid

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

event_filename = variables.get("event_filename", "")
domain = variables.get("domain", "") or event_filename.strip()

if domain.startswith("http://") or domain.startswith("https://"):
    domain = domain.split("://", 1)[1]
domain = domain.rstrip("/")

if not domain:
    print("Error: could not determine domain", file=sys.stderr)
    sys.exit(1)

wordlist_file = "bugbounty/wordlists/critical_paths.txt"
if not os.path.exists(wordlist_file):
    print(f"Wordlist not found: {wordlist_file}", file=sys.stderr)
    sys.exit(1)

with open(wordlist_file) as f:
    paths = [line.strip() for line in f if line.strip() and not line.startswith("#")]

# Skip glob patterns and directory-only entries (can't request them directly)
def is_requestable(path):
    return "*" not in path and "?" not in path

paths = [p for p in paths if is_requestable(p)]

state_file = f"bugbounty/targets/{domain}/state.json"
requested_urls = set()
if os.path.exists(state_file):
    with open(state_file) as f:
        state = json.load(f)
        requested_urls = set(state.get("requested_urls", []))

pending_dir = "bugbounty/requests/pending"
os.makedirs(pending_dir, exist_ok=True)

created = 0
for path in paths:
    path = path.lstrip("/")
    url = f"https://{domain}/{path}"

    if url in requested_urls:
        continue

    request_file = os.path.join(pending_dir, f"{uuid.uuid4().hex}.json")
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
        "source_type": "critical_probe"
    }

    with open(request_file, "w") as f:
        json.dump(request_data, f, indent=2)

    created += 1

print(f"Created {created} critical path probe requests for {domain}")
print(f"::set domain={domain}")
print(f"::set probe_requests_created={created}")
