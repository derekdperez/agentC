#!/usr/bin/env python3
"""Check if discovered file matches critical paths wordlist."""
import json
import os
import re
import sys
import shutil

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

event_path = variables.get("event_path")
event_filename = variables.get("event_filename")

if not event_path or not event_filename:
    print("Error: event_path or event_filename not found", file=sys.stderr)
    sys.exit(1)

if event_filename.endswith(".json"):
    print(f"Skipping JSON file: {event_filename}")
    sys.exit(0)

wordlist_file = "bugbounty/wordlists/critical_paths.txt"
if not os.path.exists(wordlist_file):
    print(f"Wordlist not found: {wordlist_file}", file=sys.stderr)
    sys.exit(1)

with open(wordlist_file, "r") as f:
    critical_patterns = [line.strip() for line in f if line.strip() and not line.startswith("#")]

is_critical = False
matched_pattern = None
asset_dir = os.path.dirname(event_path)

# Extract root_domain and full_hostname from path structure:
# bugbounty/targets/{root_domain}/{full_hostname}/assets/{type}/{filename}
norm_path = event_path.replace("\\", "/")
parts = norm_path.split("/")
root_domain = "unknown"
full_hostname = "unknown"
try:
    targets_idx = parts.index("targets")
    root_domain = parts[targets_idx + 1]
    full_hostname = parts[targets_idx + 2]
except (ValueError, IndexError):
    pass

content_to_check = ""
if os.path.exists(event_path):
    with open(event_path, "r", encoding="utf-8", errors="replace") as f:
        content_to_check = f.read()

filename_lower = event_filename.lower()

for pattern in critical_patterns:
    pattern_lower = pattern.lower()
    if pattern_lower in filename_lower:
        is_critical = True
        matched_pattern = pattern
        break

    if content_to_check and "/" + pattern_lower in content_to_check.lower():
        is_critical = True
        matched_pattern = pattern
        break

    if content_to_check and pattern_lower.startswith(".") and pattern_lower in content_to_check.lower():
        is_critical = True
        matched_pattern = pattern
        break

if is_critical and matched_pattern:
    critical_dir = f"bugbounty/targets/{root_domain}/{full_hostname}/assets/critical"
    os.makedirs(critical_dir, exist_ok=True)

    critical_file = os.path.join(critical_dir, f"CRITICAL_{matched_pattern.replace('/', '_').replace('.', '_')}_{os.path.basename(event_path)}")

    if os.path.exists(event_path) and not os.path.exists(critical_file):
        shutil.copy2(event_path, critical_file)

    critical_info_file = critical_file + ".json"
    critical_info = {
        "original_path": event_path,
        "matched_pattern": matched_pattern,
        "filename": event_filename,
        "detected_at": os.popen("date -Iseconds").read().strip(),
        "asset_dir": asset_dir
    }
    with open(critical_info_file, "w") as f:
        json.dump(critical_info, f, indent=2)

    print(f"CRITICAL FILE DETECTED: {event_filename}")
    print(f"Matched pattern: {matched_pattern}")
    print(f"Copied to: {critical_file}")
    print(f"::set is_critical=true")
    print(f"::set matched_pattern={matched_pattern}")
    print(f"::set critical_file={critical_file}")
else:
    print(f"File checked: {event_filename} - no critical patterns found")
    print(f"::set is_critical=false")