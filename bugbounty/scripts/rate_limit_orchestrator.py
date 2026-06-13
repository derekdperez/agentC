#!/usr/bin/env python3
"""Move requests from pending/ to ready/ respecting per-hostname rate limits."""
import fcntl
import json
import os
import shutil
import sys
import time
from urllib.parse import urlparse

DEFAULT_RATE_LIMIT = 2   # requests per second if not in rate_config.json

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

event_path = variables.get("event_path")
event_filename = variables.get("event_filename")

if not event_path or not event_filename:
    print("Error: event_path or event_filename not found", file=sys.stderr)
    sys.exit(1)

if not event_path.endswith(".json"):
    print(f"Skipping non-JSON file: {event_filename}")
    sys.exit(0)

if not os.path.exists(event_path):
    print(f"File already removed (likely a duplicate): {event_filename}")
    sys.exit(0)

# Check pause flag before claiming any rate slot
pause_file = "bugbounty/requests/PAUSED"
if os.path.exists(pause_file):
    print(f"Queue is paused — leaving {event_filename} in pending")
    sys.exit(0)

with open(event_path) as f:
    request_data = json.load(f)

domain = request_data.get("domain", "unknown")
url = request_data.get("url", "")

# Rate limit per actual hostname from the URL (not the root domain field)
hostname = urlparse(url).netloc or domain

rate_state_file = "bugbounty/requests/rate_state.json"
rate_config_file = "bugbounty/requests/rate_config.json"
lock_file = "bugbounty/requests/rate_state.lock"

os.makedirs("bugbounty/requests/ready", exist_ok=True)
os.makedirs(os.path.dirname(lock_file), exist_ok=True)

# Atomically claim a time slot for this hostname
with open(lock_file, "w") as lf:
    fcntl.flock(lf, fcntl.LOCK_EX)
    try:
        if os.path.exists(rate_state_file):
            with open(rate_state_file) as f:
                rate_state = json.load(f)
        else:
            rate_state = {}

        # Load per-hostname rate config; auto-initialize unknown hostnames
        try:
            with open(rate_config_file) as f:
                rate_config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            rate_config = {}

        if hostname not in rate_config:
            rate_config[hostname] = DEFAULT_RATE_LIMIT
            with open(rate_config_file, "w") as f:
                json.dump(rate_config, f, indent=2)

        rps = max(rate_config.get(hostname, DEFAULT_RATE_LIMIT), 0.01)
        min_interval = 1.0 / rps

        now = time.time()
        last_slot = rate_state.get(hostname, 0)
        # Cap stale future slots: if last_slot is more than 30s ahead, burst/crash
        # left a contaminated rate_state — reset to now so we don't wait hours.
        MAX_FUTURE_STACK = 30.0
        if last_slot - now > MAX_FUTURE_STACK:
            print(f"Rate state for {hostname} was {last_slot - now:.1f}s in the future — resetting", file=sys.stderr)
            last_slot = now
        next_slot = max(now, last_slot + min_interval)
        wait = max(0.0, next_slot - now)

        rate_state[hostname] = next_slot
        with open(rate_state_file, "w") as f:
            json.dump(rate_state, f, indent=2)
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)

if wait > 0:
    print(f"Rate limiting {hostname}: waiting {wait:.3f}s")
    time.sleep(wait)

ready_dir = "bugbounty/requests/ready"
ready_path = os.path.join(ready_dir, event_filename)

try:
    shutil.move(event_path, ready_path)
    print(f"Moved to ready: {event_filename} (hostname={hostname})")
    print(f"::set domain={domain}")
    print(f"::set hostname={hostname}")
    print(f"::set ready_path={ready_path}")
except FileNotFoundError:
    print(f"File gone before move (deduplicated): {event_filename}")
    sys.exit(0)
