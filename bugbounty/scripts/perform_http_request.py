#!/usr/bin/env python3
"""Perform HTTP request and save results."""
import json
import os
import shutil
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

from pipeline_config import load_limits

HTTP_TIMEOUT = load_limits()["http_timeout_seconds"]

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

with open(event_path, "r") as f:
    request_data = json.load(f)

req_id = request_data.get("id", "unknown")
domain = request_data.get("domain", "unknown")
url = request_data.get("url", "")
method = request_data.get("method", "GET")
headers = request_data.get("headers", {})
body = request_data.get("body", "") or None

source_type = request_data.get("source_type", "")

try:
    start_time = time.time()
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        req.add_header(key, value)

    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        status_code = response.getcode()
        duration_s = round(time.time() - start_time, 3)
        response_headers = dict(response.headers)
        response_body = response.read()

        parsed_url = urllib.parse.urlparse(url)
        full_hostname = parsed_url.netloc.split(":")[0]

        content_type = response_headers.get("Content-Type", "")

        # Critical probe 200 responses go directly to assets/critical/
        if source_type == "critical_probe" and status_code == 200:
            asset_type = "critical"
        elif "text/html" in content_type.lower():
            asset_type = "html"
        elif "javascript" in content_type.lower() or "application/javascript" in content_type.lower():
            asset_type = "scripts"
        elif "css" in content_type.lower():
            asset_type = "stylesheets"
        elif any(t in content_type.lower() for t in ["image/", "png", "jpg", "jpeg", "gif", "webp"]):
            asset_type = "images"
        elif any(t in content_type.lower() for t in ["zip", "gzip", "tar", "7z", "archive"]):
            asset_type = "archives"
        elif any(t in content_type.lower() for t in ["pdf", "octet-stream", "binary"]):
            asset_type = "bin"
        else:
            ext = url.split(".")[-1].lower() if "." in url else ""
            if ext in ["js", "ts", "jsx", "tsx"]:
                asset_type = "scripts"
            elif ext in ["css", "less", "scss"]:
                asset_type = "stylesheets"
            elif ext in ["png", "jpg", "jpeg", "gif", "webp", "svg", "ico"]:
                asset_type = "images"
            elif ext in ["zip", "gz", "tar", "7z", "bz2"]:
                asset_type = "archives"
            elif ext in ["pdf", "doc", "docx", "xls", "xlsx"]:
                asset_type = "bin"
            else:
                asset_type = "html"

        asset_dir = f"bugbounty/targets/{domain}/{full_hostname}/assets/{asset_type}"
        os.makedirs(asset_dir, exist_ok=True)

        url_path = parsed_url.path.lstrip("/")
        if parsed_url.query:
            url_path = url_path + "_" + parsed_url.query
        safe_filename = url_path.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_") or "root"
        safe_filename = safe_filename[:200]

        body_file = os.path.join(asset_dir, f"{safe_filename}.body")
        with open(body_file, "wb") as f:
            f.write(response_body)

        result_data = {
            "id": req_id,
            "url": url,
            "status_code": status_code,
            "content_type": content_type,
            "headers": response_headers,
            "body_file": body_file,
            "content_length": len(response_body),
            "requested_at": datetime.now().isoformat(),
            "source_type": source_type,
            "duration_s": duration_s,
        }

        result_file = os.path.join(asset_dir, f"{safe_filename}.json")
        with open(result_file, "w") as f:
            json.dump(result_data, f, indent=2)

        status_category = str(status_code)
        completed_dir = f"bugbounty/requests/completed/{status_category}"
        os.makedirs(completed_dir, exist_ok=True)

        completed_request_file = os.path.join(completed_dir, f"{req_id}.json")
        full_result = {
            "request": request_data,
            "response": result_data
        }
        with open(completed_request_file, "w") as f:
            json.dump(full_result, f, indent=2)

        try:
            os.remove(event_path)
        except FileNotFoundError:
            pass

        print(f"Completed request: {url} -> {status_code}")
        print(f"::set url={url}")
        print(f"::set status_code={status_code}")
        print(f"::set body_file={body_file}")
        print(f"::set asset_dir={asset_dir}")

except urllib.error.HTTPError as e:
    status_code = e.getcode()
    error_body = e.read().decode("utf-8", errors="replace")

    status_category = str(status_code)
    completed_dir = f"bugbounty/requests/completed/{status_category}"
    os.makedirs(completed_dir, exist_ok=True)

    result_data = {
        "id": req_id,
        "url": url,
        "status_code": status_code,
        "error": str(e),
        "body_preview": error_body[:500] if error_body else ""
    }

    completed_request_file = os.path.join(completed_dir, f"{req_id}.json")
    full_result = {
        "request": request_data,
        "response": result_data
    }
    with open(completed_request_file, "w") as f:
        json.dump(full_result, f, indent=2)

    try:
        os.remove(event_path)
    except FileNotFoundError:
        pass

    print(f"HTTP Error: {url} -> {status_code}", file=sys.stderr)
    print(f"::set url={url}")
    print(f"::set status_code={status_code}")
    print(f"::set error={str(e)}")

except Exception as e:
    print(f"Error performing request {url}: {e}", file=sys.stderr)
    error_dir = "bugbounty/requests/completed/other"
    os.makedirs(error_dir, exist_ok=True)
    error_file = os.path.join(error_dir, f"{req_id}.error.json")

    full_result = {
        "request": request_data,
        "error": str(e)
    }
    with open(error_file, "w") as f:
        json.dump(full_result, f, indent=2)

    if os.path.exists(event_path):
        shutil.move(event_path, error_file)

    print(f"::set url={url}")
    print(f"::set error={str(e)}")