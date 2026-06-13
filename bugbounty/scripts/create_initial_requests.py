#!/usr/bin/env python3
"""Create initial HTTP request files for a new target domain."""
import json
import os
import sys
import uuid

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

event_filename = variables.get("event_filename")

if not event_filename:
    print("Error: event_filename not found in context", file=sys.stderr)
    sys.exit(1)

domain = event_filename.strip()
if domain.startswith("http://") or domain.startswith("https://"):
    domain = domain.split("://", 1)[1]
domain = domain.rstrip("/")

state_file = f"bugbounty/targets/{domain}/state.json"
if not os.path.exists(state_file):
    print(f"Error: state file not found: {state_file}", file=sys.stderr)
    sys.exit(1)

with open(state_file, "r") as f:
    state = json.load(f)

pending_dir = "bugbounty/requests/pending"
os.makedirs(pending_dir, exist_ok=True)

urls_to_request = [
    f"http://{domain}/",
    f"https://{domain}/",
]

for url in urls_to_request:
    if url in state.get("requested_urls", []):
        print(f"URL already requested: {url}")
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
        "created_at": os.popen("date -Iseconds").read().strip()
    }

    with open(request_file, "w") as f:
        json.dump(request_data, f, indent=2)

    print(f"Created request: {request_file} -> {url}")

print(f"::set domain={domain}")
print(f"::set initial_requests_created=true")