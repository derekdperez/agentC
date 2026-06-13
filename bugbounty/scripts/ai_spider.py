#!/usr/bin/env python3
"""Parse AI suggestions and create pending requests for new paths."""
import json
import os
import sys
import uuid
from urllib.parse import urlparse, urlunparse

PENDING = "bugbounty/requests/pending"
TARGETS = "bugbounty/targets"


def _load_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return default


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _norm(url):
    try:
        p = urlparse(url)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, "", "", ""))
    except Exception:
        return url


def main():
    ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
    variables = ctx.get("variables", {})

    domain = variables.get("domain", "").strip()
    if not domain:
        print("Error: domain variable is required", file=sys.stderr)
        sys.exit(1)

    ai_suggestions_raw = variables.get("ai_suggestions", "").strip()
    if not ai_suggestions_raw:
        print("No AI suggestions provided", file=sys.stderr)
        sys.exit(1)

    try:
        suggestions = json.loads(ai_suggestions_raw)
    except json.JSONDecodeError as e:
        print(f"Error parsing AI suggestions as JSON: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(suggestions, list):
        print("AI suggestions must be a JSON array", file=sys.stderr)
        sys.exit(1)

    state_file = os.path.join(TARGETS, domain, "state.json")
    state = _load_json(state_file)
    if not state:
        print(f"Error: state file not found: {state_file}", file=sys.stderr)
        sys.exit(1)

    requested_paths = set(state.get("requested_paths", []) or [])
    requested_urls = set(state.get("requested_urls", []) or [])

    enqueued = 0
    skipped_low_confidence = 0
    skipped_already_requested = 0
    skipped_invalid = 0

    os.makedirs(PENDING, exist_ok=True)

    for item in suggestions:
        if not isinstance(item, dict):
            skipped_invalid += 1
            continue

        url_path = item.get("url_path", "")
        confidence = float(item.get("confidence", 0))
        rationale = item.get("rationale", "")

        if not url_path or not url_path.startswith("/"):
            skipped_invalid += 1
            continue

        if confidence < 0.6:
            skipped_low_confidence += 1
            continue

        url = f"https://{domain}{url_path}"
        norm = _norm(url)

        if norm in requested_paths:
            skipped_already_requested += 1
            continue

        request_file = os.path.join(PENDING, f"{uuid.uuid4().hex}.json")
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
            "source_type": "ai_spider",
            "ai_rationale": rationale,
            "ai_confidence": confidence
        }

        with open(request_file, "w") as f:
            json.dump(request_data, f, indent=2)

        requested_paths.add(norm)
        requested_urls.add(url)
        enqueued += 1

    if enqueued > 0:
        state["requested_paths"] = sorted(requested_paths)
        state["requested_urls"] = sorted(requested_urls)
        _atomic_write(state_file, state)

    print(f"ai-spider: domain={domain} enqueued={enqueued} "
          f"skipped_low_confidence={skipped_low_confidence} "
          f"skipped_already_requested={skipped_already_requested} "
          f"skipped_invalid={skipped_invalid}")
    print(f"::set ai_enqueued={enqueued}")
    print(f"::set ai_skipped_low_confidence={skipped_low_confidence}")
    print(f"::set ai_skipped_already_requested={skipped_already_requested}")


if __name__ == "__main__":
    main()