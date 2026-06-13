# Bug Bounty Spider

A filesystem-based spider/scanner for discovering publicly accessible assets and low-hanging fruit security issues on bug bounty targets.

## Overview

This feature runs within the agentC framework and uses a folder/task-based architecture to spider web servers, looking for developer mistakes or lazy admin configurations that result in critical data exposure.

## Folder Structure

```
bugbounty/
  targets/
    {domain}/
      state.json         # Tracks requested URLs and discovered URLs
  assets/
    html/                # HTML pages and similar content
    scripts/             # JavaScript, TypeScript, other scripts
    images/              # Downloaded images (not currently processed)
    stylesheets/          # CSS, LESS, SCSS, etc.
    archives/            # ZIP, GZIP, TAR.GZ, 7Z, etc.
    bin/                 # PDF, DOCX, and other binary files
    critical/            # Files matching critical path patterns
  requests/
    pending/             # HTTP requests waiting to be processed
    completed/
      200/               # Successful responses
      300/               # Redirection responses
      400/               # Client error responses
      500/               # Server error responses
      other/             # Other responses (errors, timeouts, etc.)
  wordlists/
    critical_paths.txt   # High-value paths to check (env files, git dirs, etc.)
    common_paths.txt     # Common web paths for brute-force discovery
```

## Getting Started

1. Add a target domain:
   ```bash
   echo "uber.com" > bugbounty/targets/uber.com
   ```

2. Start the agentC engine:
   ```bash
   agentc start
   ```

3. Monitor progress:
   ```bash
   ls -la bugbounty/requests/completed/*/
   ls -la bugbounty/assets/critical/
   ```

## How It Works

### 1. Spider Init Task
When you create a domain file like `uber.com` in `bugbounty/targets/`:
- Creates a domain state file in `bugbounty/targets/uber.com/state.json`
- Creates initial HTTP requests for `http://uber.com/` and `https://uber.com/`
- Requests are saved to `bugbounty/requests/pending/`

### 2. HTTP Request Task
When request files appear in `bugbounty/requests/pending/`:
- Performs the HTTP request
- Saves response body to appropriate `assets/` subfolder based on content type
- Moves request to `requests/completed/{status_code}/`
- If response is HTML: triggers DOM spider
- If response is a script: triggers script spider

### 3. DOM Spider Task
When HTML files appear in `bugbounty/assets/html/`:
- Extracts all links (href, src, action attributes)
- Creates new HTTP requests for discovered URLs
- Only creates requests for same-domain URLs

### 4. Script Spider Task
When script files appear in `bugbounty/assets/scripts/`:
- Extracts API endpoints and URLs using regex patterns
- Creates HTTP requests for discovered endpoints

### 5. Deduplication Task
When request files appear in `bugbounty/requests/pending/`:
- Checks if URL is already tracked in domain state
- If duplicate: removes the pending request
- If new: adds URL to domain state and keeps request

### 6. Critical File Detection
When files appear in `bugbounty/assets/` subdirectories:
- Checks filename and content against critical paths wordlist
- If match found: copies file to `bugbounty/assets/critical/`

## Wordlists

### critical_paths.txt
Contains paths that are high-value if found:
- `.env`, `.git/config`, `wp-config.php` (sensitive configuration)
- `.svn/`, `.hg/` (version control)
- `phpmyadmin/`, `admin/` (admin interfaces)
- Credential files, SSH keys, etc.

### common_paths.txt
Contains common web paths for brute-force discovery:
- `/admin`, `/login`, `/wp-admin`
- `/api/v1`, `/api/v2`, `/graphql`
- `/debug`, `/test`, `/backup`
- `/phpmyadmin`, `/phpinfo`
- And hundreds more...

## Adding Wordlist-based Discovery

To use the common paths wordlist for brute-force discovery on a domain:

```bash
# The system currently spider-driven discovery
# Future: Run a wordlist scan to check common paths
```

## Task Configuration

Tasks are configured in `configs/tasks/`:
- `bugbounty_spider_init.json` - Monitors `bugbounty/targets/`
- `bugbounty_http_request.json` - Monitors `bugbounty/requests/pending/`
- `bugbounty_dom_spider.json` - Monitors `bugbounty/assets/html/`
- `bugbounty_script_spider.json` - Monitors `bugbounty/assets/scripts/`
- `bugbounty_dedup.json` - Monitors `bugbounty/requests/pending/`
- `bugbounty_critical_detect.json` - Monitors `bugbounty/assets/`

## Notes

- The system is designed to be non-intrusive and respectful of target servers
- Default User-Agent identifies as `bugbounty-spider/1.0`
- Requests have a 30-second timeout
- Deduplication prevents requesting the same URL multiple times
- Critical file detection runs automatically when assets are discovered