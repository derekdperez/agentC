# Design: Wordlist Spider & AI Spider

## Context

The bug bounty spider pipeline currently has four deterministic content-discovery
spiders:

| Spider | Trigger | What it does |
|--------|---------|--------------|
| **DOM spider** | new `.body` in `assets/html/` | Extracts href/src/action links from HTML |
| **Script spider** | new `.body` in `assets/scripts/` | Regex-extracts API endpoints from JS |
| **Spider init** | new file in `targets/queue/` | Probes the `critical_paths.txt` wordlist |
| **Dedup + rate pump** | scheduled (1 s) | Deduplicates and rate-limits pending → ready |

This design adds two new spiders:

1. **Wordlist spider** — deterministic. Runs the `common_paths.txt` wordlist (284
   entries) against every discovered host. Uses the same request pipeline, so it
   goes through the queue pump and rate limiting automatically.

2. **AI spider** — non-deterministic. Fires only on demand (never auto-triggers on
   file events) after the other spiders have built a sitemap. It reads the full
   `state.json` for a target, feeds the discovered URL/path structure to an AI
   agent, and asks for educated guesses at unseen paths. Those guesses are filed
   as pending requests through the same pipeline.

---

## 1. Wordlist Spider

### Goal

For every hostname that has been seen (the root domain plus any subdomains discovered
by the DOM/script spiders), enqueue a request for every path in
`bugbounty/wordlists/common_paths.txt` that has not already been requested.

### Why it runs through the pipeline

All wordlist entries become JSON request files in `bugbounty/requests/pending/`.
The existing queue pump deduplicates, rate-limits, and promotes them to `ready/`
automatically. 200 responses land in `assets/critical/` via the existing
`source_type == "critical_probe"` path in `perform_http_request.py` — no changes
to that script are needed.

### New files

```
bugbounty/
  scripts/
    enqueue_wordlist.py          # new Python action

configs/tasks/
  bugbounty_wordlist_spider.json # new task definition
```

### Task configuration

```json
{
  "name": "bugbounty-wordlist-spider",
  "description": "Enqueues unchecked common_paths.txt entries for every discovered host.",
  "enabled": true,
  "persist": false,
  "trigger": {
    "type": "schedule",
    "interval": 300
  },
  "actions": [
    {
      "name": "enqueue-wordlist",
      "type": "python",
      "executable": "bugbounty/scripts/enqueue_wordlist.py"
    }
  ]
}
```

`persist: false` because it would otherwise flood `agentc runs` every 5 minutes.

The schedule interval (300 s = 5 min) is deliberately conservative. It can be
tuned later. To run it immediately: `agentc run bugbounty-wordlist-spider`.

### `enqueue_wordlist.py` — script design

```
Input : AGENTC_CONTEXT (standard)
Output: new JSON request files in bugbounty/requests/pending/
        stdout lines ::set host=<hostname> count=<N> skipped=<M>
```

**Algorithm**

1. Load `pipeline_config.py` (`load_limits`) to get `pump_batch`.
2. Scan `bugbounty/targets/<domain>/state.json` for every discovered domain.
3. For each domain, build the set of all hosts that have been seen
   (parse `requested_urls` for netlocs, plus the domain itself).
4. Load `bugbounty/wordlists/common_paths.txt` into a list.
5. For each host, for each path in the wordlist:
   - Construct `https://<host>/<path>` (skip the scheme toggle; always try HTTPS first
     as the wordlist assumes TLS by default, which matches modern practice).
   - Normalise and deduplicate against `requested_paths` already in state.json.
   - Write a request file to `pending/` with `"source_type": "wordlist_spider"`.
6. Record all newly enqueued URLs in `state.json` (atomic write via `_atomic_write`).
7. Print summary `::set` lines.

**Request file shape**

```json
{
  "id": "<uuid>",
  "domain": "uber.com",
  "url": "https://uber.com/admin",
  "method": "GET",
  "headers": { "User-Agent": "Mozilla/5.0 (compatible; bugbounty-spider/1.0)" },
  "body": "",
  "created_at": "<ISO timestamp>",
  "source_type": "wordlist_spider"
}
```

**Path construction details**

- If the wordlist entry ends in `/` (e.g. `staging/`), strip the trailing slash
  to get a clean path segment (`staging`) before appending.
- Skip entries that look like filenames with extensions (contain `.`) when checking
  against a host that only serves HTML — but since we cannot know that ahead of
  time, just enqueue everything; dedup handles the rest.
- Skip entries that start with `.` (dotfiles like `.env`, `.git/config`) — those
  belong to the critical-paths probe, not this spider.

**Skipped entries log**

Print a one-line summary to stdout per host:
```
enqueue-wordlist: host=api.uber.com enqueued=47 skipped=237 (already requested)
```

---

## 2. AI Spider

### Goal

After deterministic spiders have built a comprehensive sitemap, use an AI agent to
analyse the discovered URL structure and guess additional paths that are likely to
exist but have not been found. Generate between **20 and 100** suggestions per run.

### Key design constraint: runs last / separately

The AI spider must **never** be auto-triggered by file events. It is run explicitly:

```bash
agentc run bugbounty-ai-spider -v domain=uber.com
```

The `-v domain=...` variable tells the script which target to operate on.

### Why 20–100 ceiling?

- Below 20: not enough signal diversity for the AI to be useful.
- Above 100: the AI will start hallucinating low-confidence paths, wasting rate
  limits on the target and increasing false-positive noise.
- The ceiling is enforced in the prompt (see below).

### New files

```
bugbounty/
  scripts/
    generate_sitemap.py           # builds the prompt context from state.json
    ai_spider.py                  # new Python action (reads sitemap, calls agent, enqueues)

configs/tasks/
  bugbounty_ai_spider.json        # new task definition
```

### Task configuration

```json
{
  "name": "bugbounty-ai-spider",
  "description": "AI-powered path discovery. Reads the accumulated sitemap for a target and asks an AI agent to suggest unseen paths. Run manually: agentc run bugbounty-ai-spider -v domain=uber.com",
  "enabled": true,
  "trigger": {
    "type": "manual"
  },
  "variables": {
    "domain": "",
    "min_suggestions": 20,
    "max_suggestions": 100
  },
  "actions": [
    {
      "name": "build-sitemap",
      "type": "python",
      "executable": "bugbounty/scripts/generate_sitemap.py",
      "set": {
        "sitemap_json": "${build-sitemap.stdout}"
      }
    },
    {
      "name": "ask-ai",
      "type": "agent",
      "agent": "researcher",
      "prompt": "You are a security researcher specialising in web application path enumeration. A target domain has been crawled and the following URL structure was discovered:\n\n${sitemap_json}\n\nBased ONLY on this structure, suggest between ${min_suggestions} and ${max_suggestions} additional URL paths that are likely to exist on this target but were NOT found by the crawl. Think about:\n- Common API naming conventions consistent with the discovered endpoints\n- Admin/management paths that typically accompany the discovered application type\n- Configuration and monitoring endpoints typical for the detected stack\n- Version-specific paths if version info was found\n- Backup/staging paths that mirror production paths found\n\nFor each suggestion include:\n1. The full URL path (e.g. /api/v2/users/$id/profile)\n2. A brief one-sentence rationale explaining WHY this path is likely to exist given the discovered structure\n3. A confidence score 0.0-1.0\n\nReturn ONLY a valid JSON array of objects with keys: url_path, rationale, confidence. Do not include any other text.",
      "set": {
        "ai_suggestions": "${ask-ai.stdout}"
      }
    },
    {
      "name": "enqueue-ai-suggestions",
      "type": "python",
      "executable": "bugbounty/scripts/ai_spider.py",
      "when": "${ai_suggestions}",
      "set": {
        "ai_enqueued": "${enqueue-ai-suggestions.output.enqueued}",
        "ai_skipped": "${enqueue-ai-suggestions.output.skipped}"
      }
    }
  ],
  "emits": ["bugbounty.ai-spider.done"]
}
```

### `generate_sitemap.py` — script design

```
Input : AGENTC_CONTEXT (domain from variables)
Output: JSON document printed to stdout (captured as sitemap_json variable)
        ::set url_count=<N> path_count=<M> host_count=<K>
```

The script reads `bugbounty/targets/<domain>/state.json` and produces a
structured JSON summary:

```json
{
  "domain": "uber.com",
  "hosts": ["uber.com", "api.uber.com", "m.uber.com"],
  "url_count": 780,
  "path_count": 245,
  "discovered_paths": [
    { "path": "/", "count": 3, "hosts": ["uber.com", "api.uber.com", "m.uber.com"] },
    { "path": "/login", "count": 1, "hosts": ["uber.com"] },
    { "path": "/api/v1/users", "count": 2, "hosts": ["api.uber.com"] }
  ],
  "path_structure": {
    "api_prefixes": ["/api/v1", "/api/internal"],
    "common_extensions": [".json", ".js", ".html"],
    "parameter_patterns": ["id", "user", "uuid"]
  },
  "technology_hints": ["react", "nodejs", "nginx"]
}
```

**Algorithm**

1. Load `state.json` for the given domain.
2. Parse all `discovered_urls`, grouping by:
   - hostname (extract netloc)
   - path (strip query strings)
   - file extension
3. Identify common path prefixes (e.g. `/api/`, `/admin/`, `/static/`).
4. Identify technology hints from:
   - `source_type` fields in the state
   - Server header patterns from completed requests (if stored)
   - Common framework fingerprints in paths (e.g. `__webpack__`, `wp-content`)
5. Count path frequency across hosts.
6. Print the summary as a compact JSON string to stdout.

### `ai_spider.py` — script design

```
Input : AGENTC_CONTEXT (ai_suggestions JSON string from previous action)
Output: new JSON request files in bugbounty/requests/pending/
        stdout lines ::set enqueued=<N> skipped=<M> errors=<E>
```

**Algorithm**

1. Parse `ai_suggestions` (a JSON array) from the previous action's stdout.
2. Load `bugbounty/targets/<domain>/state.json` to get already-requested paths.
3. For each suggestion:
   - Skip if `confidence < 0.6`.
   - Skip if already in `requested_paths`.
   - Construct the full URL: `https://<domain>/<path>` (use the domain itself,
     not a specific host, to let the target resolve to the correct one).
   - Write the request file with `"source_type": "ai_spider"`.
4. Update `state.json` with new `requested_urls`.
5. Print `::set` summary.

**Request file shape** — same as wordlist spider, with `"source_type": "ai_spider"`.

**Error handling**

- If the AI returns malformed JSON, skip the entire suggestions array and emit a
  warning. Do not crash.
- If `domain` variable is empty/missing, exit with code 1 and a clear error
  message.

---

## 3. Execution order & integration

```
agentc start
    └─► queue pump (schedule, every 1 s)
    └─► wordlist spider (schedule, every 5 min — creates pending requests)
    └─► DOM spider (file: new .body in assets/html/)
    └─► script spider (file: new .body in assets/scripts/)
    └─► HTTP request (file: new .json in requests/ready/)
            └─► results land in assets/*/
                    └─► triggers DOM/script spiders again (cycle continues)

agentc run bugbounty-ai-spider -v domain=uber.com   ← runs ONCE, on demand
    └─► generate_sitemap.py  →  sitemap_json
    └─► researcher agent     →  ai_suggestions (JSON array)
    └─► ai_spider.py         →  new pending requests (go through pump on next tick)
```

### Ensuring AI spider runs "last"

There is no hard enforcement — the AI spider is simply never given an auto-trigger.
Users run it explicitly after the crawl has plateaued. The `emit` at the end
(`bugbounty.ai-spider.done`) is there so downstream tasks can chain off it if
needed (e.g. a notification or report task).

---

## 4. Rate-limiting considerations

Both new spiders submit requests through the existing queue pump, so they inherit
all existing rate limiting automatically.

The AI spider can generate up to 100 suggestions per run. At 2 req/s/host, those
100 URLs take at most ~50 seconds to drain from `ready/` if they all map to the
same host. In practice they will be spread across hosts and drain faster.

---

## 5. Wordlist reduction to top 100

The existing `common_paths.txt` has 284 entries. The wordlist spider task should
use only the **top 100** entries by priority. Rather than editing the shared
`common_paths.txt` (which is also used by other tools), create a focused
wordlist:

```
bugbounty/wordlists/
  top100_common_paths.txt       # new file — top 100 entries only
```

The wordlist spider script should accept a configurable wordlist path via
environment variable or variable, defaulting to this new file:

```python
WORDLIST_PATH = os.environ.get(
    "AGENTC_WORDLIST_PATH",
    "bugbounty/wordlists/top100_common_paths.txt"
)
```

This lets users swap in different wordlists without changing the script.

---

## 6. Summary of all new files

| File | Purpose |
|------|---------|
| `bugbounty/wordlists/top100_common_paths.txt` | Top-100 subset of `common_paths.txt` for the wordlist spider |
| `bugbounty/scripts/enqueue_wordlist.py` | Python action: enqueues wordlist entries for all known hosts |
| `bugbounty/scripts/generate_sitemap.py` | Python action: builds structured sitemap JSON from `state.json` |
| `bugbounty/scripts/ai_spider.py` | Python action: parses AI suggestions and creates pending requests |
| `configs/tasks/bugbounty_wordlist_spider.json` | Scheduled task definition for wordlist spider |
| `configs/tasks/bugbounty_ai_spider.json` | Manual task definition for AI spider |

---

## 7. Implementation order

1. **`top100_common_paths.txt`** — hand-curated slice of `common_paths.txt`
   (no code changes needed).
2. **`enqueue_wordlist.py`** + **`bugbounty_wordlist_spider.json`** — easiest
   piece; validates the new wordlist-spider pattern before the AI piece is added.
3. **`generate_sitemap.py`** — standalone testable; print the JSON to stdout and
   eyeball it.
4. **`ai_spider.py`** — depends on step 3 being correct; parse and enqueue.
5. **`bugbounty_ai_spider.json`** — wire steps 3–4 into a task with the
   `researcher` agent prompt.
6. **Test**: run `agentc run bugbounty-wordlist-spider` and verify pending files
   appear. Then run `agentc run bugbounty-ai-spider -v domain=crawler-test.com`
   and verify AI suggestions are enqueued.
7. **Update `bugbounty/README.md`** to document the two new spiders.