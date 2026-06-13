#!/usr/bin/env python3
"""Create domain state file when a new target domain is added."""
import json
import os
import sys

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

event_path = variables.get("event_path")
event_filename = variables.get("event_filename")

if not event_path or not event_filename:
    print("Error: event_path or event_filename not found in context", file=sys.stderr)
    sys.exit(1)

domain = event_filename.strip()
if domain.startswith("http://") or domain.startswith("https://"):
    domain = domain.split("://", 1)[1]
domain = domain.rstrip("/")

target_dir = f"bugbounty/targets/{domain}"
os.makedirs(target_dir, exist_ok=True)

state_file = f"{target_dir}/state.json"
state = {
    "domain": domain,
    "requested_urls": [],
    "discovered_urls": [],
    "created_at": os.popen("date -Iseconds").read().strip()
}

with open(state_file, "w") as f:
    json.dump(state, f, indent=2)

# Remove the queue trigger file
if os.path.exists(event_path):
    os.remove(event_path)

print(f"Created domain state: {state_file}")
print(f"::set domain={domain}")
print(f"::set state_file={state_file}")
