#!/usr/bin/env python3
"""Finalize a watched file after an agent has processed it.

Given the triggering file event (via ``$AGENTC_CONTEXT``), this:
  1. writes the agent's output as a companion document into a ``completed/``
     subfolder next to the original file, and
  2. moves the original file into that same ``completed/`` folder.

The ``completed/`` folder lives *inside* the watched directory, but because the
watch is non-recursive, writing/moving into it does not re-trigger the task.

Arguments:
    argv[1]  name of the variable holding the agent's output text
    argv[2]  suffix for the companion document (e.g. ".design.md")

All paths are derived from the original file's absolute path, so this works no
matter which directory the engine was started from.
"""
import json
import os
import shutil
import sys

ctx = json.loads(os.environ.get("AGENTC_CONTEXT", "{}"))
variables = ctx.get("variables", {})

src = variables.get("event_path")
name = variables.get("event_filename")
if not src or not name or not os.path.exists(src):
    print(f"finalize: source file missing: {src!r}", file=sys.stderr)
    sys.exit(1)

var_name = sys.argv[1] if len(sys.argv) > 1 else "result"
suffix = sys.argv[2] if len(sys.argv) > 2 else ".out.md"
document = str(variables.get(var_name, "")).rstrip()

completed = os.path.join(os.path.dirname(src), "completed")
os.makedirs(completed, exist_ok=True)

doc_path = os.path.join(completed, name + suffix)
with open(doc_path, "w", encoding="utf-8") as fh:
    fh.write(document + "\n")

archived_path = os.path.join(completed, name)
shutil.move(src, archived_path)

print(f"wrote companion document: {doc_path}")
print(f"archived original:        {archived_path}")
print(f"::set document={doc_path}")
print(f"::set archived={archived_path}")
