#!/usr/bin/env python3
"""Build a structured sitemap JSON from state.json for a given domain."""
import json
import os
import sys
from collections import defaultdict
from urllib.parse import urlparse

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

domain = variables.get("domain", "").strip()
if not domain:
    print("Error: domain variable is required", file=sys.stderr)
    sys.exit(1)

TARGETS = "bugbounty/targets"
state_file = os.path.join(TARGETS, domain, "state.json")
if not os.path.exists(state_file):
    print(f"Error: state file not found: {state_file}", file=sys.stderr)
    sys.exit(1)

with open(state_file) as f:
    state = json.load(f)

requested_urls = state.get("requested_urls", [])
requested_paths = state.get("requested_paths", [])

hosts = set()
path_freq = defaultdict(int)
path_hosts = defaultdict(set)
extensions = set()
path_prefixes = set()
param_patterns = set()

for url in requested_urls:
    parsed = urlparse(url)
    netloc = parsed.netloc.split(":")[0]
    if netloc:
        hosts.add(netloc)
    path = parsed.path
    if path:
        path_freq[path] += 1
        path_hosts[path].add(netloc)
        if "." in path:
            ext = path.rsplit(".", 1)[-1]
            if len(ext) <= 10:
                extensions.add(ext)
        parts = path.strip("/").split("/")
        for i in range(1, len(parts)):
            prefix = "/" + "/".join(parts[:i])
            path_prefixes.add(prefix)
        for part in parts:
            if part and any(c.isdigit() for c in part) and len(part) > 2:
                param_patterns.add(part[:8])

discovered_paths = []
for path, count in sorted(path_freq.items(), key=lambda x: -x[1])[:50]:
    discovered_paths.append({
        "path": path,
        "count": count,
        "hosts": sorted(path_hosts[path])
    })

result = {
    "domain": domain,
    "hosts": sorted(hosts),
    "url_count": len(requested_urls),
    "path_count": len(path_freq),
    "host_count": len(hosts),
    "discovered_paths": discovered_paths,
    "path_structure": {
        "path_prefixes": sorted(path_prefixes)[:20],
        "file_extensions": sorted(extensions),
        "parameter_patterns": sorted(param_patterns)[:10]
    }
}

output = json.dumps(result, indent=2)
print(output)
sys.stderr.write(f"::set url_count={len(requested_urls)}\n")
sys.stderr.write(f"::set path_count={len(path_freq)}\n")
sys.stderr.write(f"::set host_count={len(hosts)}\n")