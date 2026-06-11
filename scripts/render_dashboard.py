#!/usr/bin/env python3
"""Write the static dashboard.html from the engine's current state.

This is the *read-only* renderer used by the scheduled ``dashboard`` task. The
actual HTML/server logic lives in :mod:`agentc.dashboard`; this script just adds
the project root to sys.path (so it can import the package when run as a
subprocess) and writes the file. For the interactive dashboard with add/edit/
delete, run ``agentc serve``.

Self-locating: ``scripts/render_dashboard.py`` -> repo root.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from agentc.dashboard import Paths, render_page  # noqa: E402


def main():
    paths = Paths(ROOT)
    page = render_page(paths, interactive=False)
    tmp = paths.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(tmp, paths.out)
    print(f"dashboard written: {paths.out}")


if __name__ == "__main__":
    main()
