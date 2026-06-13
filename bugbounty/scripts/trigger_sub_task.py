#!/usr/bin/env python3
import json
import os
import sys
import uuid

def main():
    if len(sys.argv) < 3:
        print("Usage: trigger_sub_task.py <task_type> <hostname> [target_domain]")
        sys.exit(1)

    task_type = sys.argv[1]
    hostname = sys.argv[2]
    target_domain = sys.argv[3] if len(sys.argv) > 3 else hostname

    pending_dir = "bugbounty/requests/pending"
    os.makedirs(pending_dir, exist_ok=True)

    if task_type == "subfinder":
        # We just drop the hostname into the queue to trigger the full init (including subfinder)
        # But wait, the init task deletes the queue file and runs create_domain_state which re-inits requested_urls.
        # If the domain already exists, we might want to just run run_subfinder.py directly or similar.
        # Actually, let's just create a trigger file for a NEW task that only runs subfinder.
        pass

    elif task_type == "all_spiders":
        # To run all spiders, we can just re-queue the root URLs
        create_requests(hostname, target_domain, ["http://{}/", "https://{}/"])
    
    elif task_type == "dom_spider":
        # DOM spider normally runs on HTML assets. To "run" it on a hostname, 
        # we might just want to request the root again and let the natural flow happen.
        create_requests(hostname, target_domain, ["http://{}/", "https://{}/"], source="manual_dom")

    elif task_type == "script_spider":
        create_requests(hostname, target_domain, ["http://{}/", "https://{}/"], source="manual_script")

    elif task_type == "critical":
        # Run critical probe
        run_critical_probe(hostname, target_domain)

def create_requests(hostname, domain, templates, source="manual"):
    pending_dir = "bugbounty/requests/pending"
    for temp in templates:
        url = temp.format(hostname)
        req_id = uuid.uuid4().hex
        request_data = {
            # Attribute the request to the parent target so the asset is filed
            # under targets/<domain>/<hostname>/ — NOT as a new top-level target.
            "id": req_id,
            "domain": domain,
            "url": url,
            "method": "GET",
            "headers": {"User-Agent": "Mozilla/5.0 (compatible; bugbounty-spider/1.0)"},
            "body": "",
            "created_at": os.popen("date -Iseconds").read().strip(),
            "source_type": source
        }
        with open(os.path.join(pending_dir, f"{req_id}.json"), "w") as f:
            json.dump(request_data, f, indent=2)

def run_critical_probe(hostname, domain):
    wordlist = "bugbounty/wordlists/critical_paths.txt"
    if not os.path.exists(wordlist): return
    with open(wordlist) as f:
        paths = [l.strip() for l in f if l.strip() and not l.startswith("#") and "*" not in l]
    
    pending_dir = "bugbounty/requests/pending"
    for p in paths:
        url = f"https://{hostname}/{p.lstrip('/')}"
        req_id = uuid.uuid4().hex
        request_data = {
            # Attribute to the parent target (see create_requests above).
            "id": req_id,
            "domain": domain,
            "url": url,
            "method": "GET",
            "headers": {"User-Agent": "Mozilla/5.0 (compatible; bugbounty-spider/1.0)"},
            "body": "",
            "created_at": os.popen("date -Iseconds").read().strip(),
            "source_type": "manual_critical"
        }
        with open(os.path.join(pending_dir, f"{req_id}.json"), "w") as f:
            json.dump(request_data, f, indent=2)

if __name__ == "__main__":
    main()
