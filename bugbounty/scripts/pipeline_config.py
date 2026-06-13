#!/usr/bin/env python3
"""Central, tunable limits for the bug-bounty request pipeline.

Every component that has a "maximum" (rate, batch size, timeout, …) reads its
ceiling from here instead of hard-coding it, so the whole pipeline can be tuned
from one place without editing code.

Precedence (low → high):
  1. ``DEFAULTS`` below
  2. ``bugbounty/requests/limits.json`` (edit this to tune persistently)
  3. ``AGENTC_LIMIT_<KEY>`` environment variables (per-run override)

Note: ``engine.max_workers`` (concurrent task/subprocess cap) lives with the
engine and is set via ``AGENTC_MAX_WORKERS`` in the systemd unit, because it is
framework-level rather than bug-bounty-specific. The pump's *interval* is the
``trigger.interval`` of ``configs/tasks/bugbounty_queue_pump.json``.
"""
import json
import os

CONFIG_PATH = "bugbounty/requests/limits.json"

# key -> (default, type)
DEFAULTS = {
    "default_rate_per_host": (2.0, float),    # req/s per subdomain (host) — stays polite
    "burst": (6.0, float),                    # max requests a host may bank (catch-up headroom)
    "default_rate_per_domain": (50.0, float), # req/s aggregate per apex domain
    "domain_burst": (100.0, float),           # max requests a domain may bank (catch-up headroom)
    "default_rate_per_system": (100.0, float),# req/s ceiling across ALL domains
    "system_burst": (200.0, float),           # max requests the system may bank
    "pump_batch": (1500, int),                # pending files inspected per tick
    "crawler_concurrency": (400, int),        # in-flight HTTP requests in the batch crawler
    "crawler_batch": (1500, int),             # max ready files a crawler tick drains
    "crawler_timeout": (5.0, float),          # per-request timeout in the batch crawler (dead hosts fail fast)
    "http_timeout_seconds": (12.0, float),    # per-request network timeout (legacy single-request path)
}


def load_limits() -> dict:
    cfg = {k: d for k, (d, _t) in DEFAULTS.items()}
    try:
        with open(CONFIG_PATH) as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            for k in DEFAULTS:
                if k in user:
                    cfg[k] = user[k]
    except (FileNotFoundError, ValueError, OSError):
        pass
    for k, (_d, typ) in DEFAULTS.items():
        ev = os.environ.get("AGENTC_LIMIT_" + k.upper())
        if ev is not None:
            try:
                cfg[k] = typ(ev)
            except (TypeError, ValueError):
                pass
        else:
            try:
                cfg[k] = typ(cfg[k])
            except (TypeError, ValueError):
                cfg[k] = DEFAULTS[k][0]
    return cfg
