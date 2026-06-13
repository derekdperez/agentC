#!/usr/bin/env python3
import json
import os
import sys
import subprocess

def main():
    if len(sys.argv) < 2:
        print("Usage: trigger_subfinder.py <domain>")
        sys.exit(1)
    
    domain = sys.argv[1]
    # We set AGENTC_CONTEXT to fool run_subfinder.py
    env = os.environ.copy()
    env["AGENTC_CONTEXT"] = json.dumps({"variables": {"domain": domain}})
    
    # We need to be in the repo root for run_subfinder.py to work (it uses relative paths)
    subprocess.run([sys.executable, "bugbounty/scripts/run_subfinder.py"], env=env, check=True)

if __name__ == "__main__":
    main()
