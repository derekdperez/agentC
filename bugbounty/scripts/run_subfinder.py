#!/usr/bin/env python3
"""Run subfinder on a new domain and create requests for discovered subdomains."""
import json
import os
import subprocess
import sys
import uuid

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

# event_filename is always in context (set by engine from event payload)
domain = variables.get("domain") or variables.get("event_filename", "").strip()
if domain.startswith("http://") or domain.startswith("https://"):
    domain = domain.split("://", 1)[1]
domain = domain.rstrip("/")

if not domain:
    print("Error: domain not found in context", file=sys.stderr)
    sys.exit(0)  # non-fatal — let create_initial_requests still run

print(f"Running subfinder for domain: {domain}")

try:
    result = subprocess.run(
        ["subfinder", "-d", domain, "-silent"],
        capture_output=True,
        text=True,
    )
    subdomains = [s.strip() for s in result.stdout.splitlines() if s.strip()]
    if result.returncode != 0:
        print(f"subfinder exited {result.returncode} for {domain}: {result.stderr.strip()}", file=sys.stderr)
except (FileNotFoundError, subprocess.TimeoutExpired) as e:
    print(f"subfinder unavailable or timed out: {e}", file=sys.stderr)
    subdomains = []

print(f"Subfinder discovered {len(subdomains)} subdomains for {domain}")

pending_dir = "bugbounty/requests/pending"
os.makedirs(pending_dir, exist_ok=True)

created = 0
for sub in subdomains:
    # For each subdomain, we create requests for both http and https
    for proto in ["http", "https"]:
        url = f"{proto}://{sub}/"
        
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
            "source_type": "subfinder"
        }

        with open(request_file, "w") as f:
            json.dump(request_data, f, indent=2)
        
        created += 1

print(f"Created {created} requests from subfinder results")
print(f"::set subfinder_done=true")
print(f"::set subdomains_discovered={len(subdomains)}")

# Clean up the queue trigger file (used when running as a standalone on-demand task).
event_path = variables.get("event_path", "")
if event_path and os.path.exists(event_path):
    os.remove(event_path)
