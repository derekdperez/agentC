#!/usr/bin/env python3
"""Extract links from HTML and create new pending requests."""
import json
import os
import re
import sys
import uuid
import urllib.parse
from html.parser import HTMLParser

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

# Only process files under .../assets/html/
norm_path = event_path.replace("\\", "/")
if "/assets/html/" not in norm_path:
    print(f"Skipping non-HTML asset: {event_filename}")
    sys.exit(0)

if not os.path.exists(event_path):
    print(f"Body file not found: {event_path}", file=sys.stderr)
    sys.exit(1)

# Extract root_domain and full_hostname from path structure:
# bugbounty/targets/{root_domain}/{full_hostname}/assets/html/{filename}
parts = norm_path.split("/")
try:
    targets_idx = parts.index("targets")
    root_domain = parts[targets_idx + 1]
    full_hostname = parts[targets_idx + 2]
except (ValueError, IndexError):
    print(f"Could not extract domain from path: {event_path}", file=sys.stderr)
    sys.exit(1)

with open(event_path, "r", encoding="utf-8", errors="replace") as f:
    html_content = f.read()

state_file = f"bugbounty/targets/{root_domain}/state.json"
requested_urls = set()
if os.path.exists(state_file):
    with open(state_file, "r") as f:
        state = json.load(f)
        requested_urls = set(state.get("requested_urls", []))

class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag in ("a", "area", "link"):
            if attrs_dict.get("href"):
                self.links.append(attrs_dict["href"])
        elif tag == "img":
            if attrs_dict.get("src"):
                self.links.append(attrs_dict["src"])
        elif tag in ("script", "iframe"):
            if attrs_dict.get("src"):
                self.links.append(attrs_dict["src"])
        elif tag == "form":
            if attrs_dict.get("action"):
                self.links.append(attrs_dict["action"])

extractor = LinkExtractor()
try:
    extractor.feed(html_content)
except Exception as e:
    print(f"Error parsing HTML: {e}", file=sys.stderr)

raw_links = set(extractor.links)

href_pattern = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
src_pattern = re.compile(r'(?:src|action)=["\']([^"\']+)["\']', re.IGNORECASE)
url_pattern = re.compile(r'https?://[^\s"\'<>]+', re.IGNORECASE)

for match in href_pattern.findall(html_content):
    raw_links.add(match)
for match in src_pattern.findall(html_content):
    raw_links.add(match)
for match in url_pattern.findall(html_content):
    raw_links.add(match)

pending_dir = "bugbounty/requests/pending"
os.makedirs(pending_dir, exist_ok=True)

requests_created = 0
for link in raw_links:
    if not link or link.startswith("#") or link.startswith("javascript:"):
        continue

    if link.startswith("//"):
        link = "https:" + link
    elif link.startswith("/"):
        link = f"https://{full_hostname}" + link
    elif not link.startswith("http://") and not link.startswith("https://"):
        link = urllib.parse.urljoin(f"https://{full_hostname}/", link)

    if not link.startswith("http://") and not link.startswith("https://"):
        continue

    parsed_link = urllib.parse.urlparse(link)
    link_domain = parsed_link.netloc
    if root_domain not in link_domain and link_domain != "":
        continue

    if link in requested_urls:
        continue

    request_file = os.path.join(pending_dir, f"{uuid.uuid4().hex}.json")
    request_data = {
        "id": uuid.uuid4().hex,
        "domain": root_domain,
        "url": link,
        "method": "GET",
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; bugbounty-spider/1.0)"
        },
        "body": "",
        "created_at": os.popen("date -Iseconds").read().strip(),
        "source_file": event_filename,
        "source_type": "dom"
    }

    with open(request_file, "w") as f:
        json.dump(request_data, f, indent=2)

    requested_urls.add(link)
    requests_created += 1
    print(f"Created request from DOM: {link}")

# NOTE: do not write state.json here. dedup_check.py is the sole authoritative
# (locked, atomic) writer of requested_urls; the read above is only an in-run
# pre-filter to avoid queuing obvious duplicates. Writing here would race dedup
# and the script spider and corrupt the file.

print(f"::set domain={root_domain}")
print(f"::set links_extracted={len(raw_links)}")
print(f"::set requests_created={requests_created}")
