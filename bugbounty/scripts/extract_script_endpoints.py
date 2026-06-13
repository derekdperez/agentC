#!/usr/bin/env python3
"""Extract API endpoints from JavaScript and create new pending requests."""
import json
import os
import re
import sys
import uuid
import urllib.parse

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

event_path = variables.get("event_path")
event_filename = variables.get("event_filename")

if not event_path or not event_filename:
    print("Error: event_path or event_filename not found", file=sys.stderr)
    sys.exit(1)

if event_filename.endswith(".json"):
    print(f"Skipping JSON result file: {event_filename}")
    sys.exit(0)

# Only process files under .../assets/scripts/
norm_path = event_path.replace("\\", "/")
if "/assets/scripts/" not in norm_path:
    print(f"Skipping non-script asset: {event_filename}")
    sys.exit(0)

if not os.path.exists(event_path):
    print(f"Script file not found: {event_path}", file=sys.stderr)
    sys.exit(1)

# Extract root_domain and full_hostname from path structure:
# bugbounty/targets/{root_domain}/{full_hostname}/assets/scripts/{filename}
parts = norm_path.split("/")
try:
    targets_idx = parts.index("targets")
    root_domain = parts[targets_idx + 1]
    full_hostname = parts[targets_idx + 2]
except (ValueError, IndexError):
    print(f"Could not extract domain from path: {event_path}", file=sys.stderr)
    sys.exit(1)

with open(event_path, "r", encoding="utf-8", errors="replace") as f:
    script_content = f.read()

state_file = f"bugbounty/targets/{root_domain}/state.json"
requested_urls = set()
if os.path.exists(state_file):
    with open(state_file, "r") as f:
        state = json.load(f)
        requested_urls = set(state.get("requested_urls", []))

patterns = [
    re.compile(r'["\']((?:/api/|[/a-zA-Z0-9_-]*\.(?:php|asp|aspx|jsp))[^"\']*?)["\']', re.IGNORECASE),
    re.compile(r'["\'](/[a-zA-Z0-9_/-]+\.json)["\']', re.IGNORECASE),
    re.compile(r'fetch\s*\(\s*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'axios\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'window\.open\s*\(\s*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'location\.href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'url\s*:\s*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'endpoint\s*:\s*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'baseURL\s*:\s*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'["\']((?:https?://)?[^"\']*(?:/api|/v[0-9]+|/rest|/graphql)[^"\']*)["\']', re.IGNORECASE),
    re.compile(r'https?://[a-zA-Z0-9.-]+(?::[0-9]+)?(/[a-zA-Z0-9_/.-]*(?:\.[a-zA-Z]{2,})?)', re.IGNORECASE),
]

found_endpoints = set()
for pattern in patterns:
    for match in pattern.finditer(script_content):
        endpoint = match.group(1) if match.groups() else match.group(0)
        if endpoint and len(endpoint) > 1:
            found_endpoints.add(endpoint)

pending_dir = "bugbounty/requests/pending"
os.makedirs(pending_dir, exist_ok=True)

requests_created = 0
for endpoint in found_endpoints:
    if not endpoint or endpoint.startswith("#"):
        continue

    if endpoint.startswith("//"):
        endpoint = "https:" + endpoint
    elif endpoint.startswith("/"):
        endpoint = f"https://{full_hostname}" + endpoint
    elif not endpoint.startswith("http://") and not endpoint.startswith("https://"):
        if ":" not in endpoint and not endpoint.startswith("/"):
            continue
        endpoint = urllib.parse.urljoin(f"https://{full_hostname}/", endpoint)

    if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
        continue

    parsed_link = urllib.parse.urlparse(endpoint)
    link_domain = parsed_link.netloc
    if root_domain not in link_domain and link_domain != "" and not link_domain.endswith(f".{root_domain}"):
        continue

    if endpoint in requested_urls:
        continue

    request_file = os.path.join(pending_dir, f"{uuid.uuid4().hex}.json")
    request_data = {
        "id": uuid.uuid4().hex,
        "domain": root_domain,
        "url": endpoint,
        "method": "GET",
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; bugbounty-spider/1.0)"
        },
        "body": "",
        "created_at": os.popen("date -Iseconds").read().strip(),
        "source_file": event_filename,
        "source_type": "script"
    }

    with open(request_file, "w") as f:
        json.dump(request_data, f, indent=2)

    requested_urls.add(endpoint)
    requests_created += 1
    print(f"Created request from script: {endpoint}")

# NOTE: do not write state.json here. dedup_check.py is the sole authoritative
# (locked, atomic) writer of requested_urls; the read above is only an in-run
# pre-filter to avoid queuing obvious duplicates. Writing here would race dedup
# and the DOM spider and corrupt the file.

print(f"::set domain={root_domain}")
print(f"::set endpoints_found={len(found_endpoints)}")
print(f"::set requests_created={requests_created}")
