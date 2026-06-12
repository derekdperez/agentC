"""Filesystem-event watching using the Linux file-watching system.

Backends, in order of preference:
  1. watchdog (cross-platform; wraps inotify on Linux)
  2. raw inotify via ctypes  — the native Linux file-watching syscalls
  3. a polling fallback       — for unusual environments

A watch maps a path + event kind + filename glob to a callback. When the path
is a *file*, we watch its parent directory and filter by name, so "this file
was updated" works the same as "a file arrived in this folder".
"""

from __future__ import annotations

import ctypes
import fnmatch
import logging
import os
import struct
import threading
import time
from typing import Callable, List, Optional

log = logging.getLogger("agentc.watcher")

WatchCallback = Callable[[str, str], None]  # (filepath, event_kind)

try:  # pragma: no cover
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _HAVE_WATCHDOG = True
except ImportError:  # pragma: no cover
    _HAVE_WATCHDOG = False


# Map a user-facing event kind to a set of canonical kinds we may emit.
_KIND_ALIASES = {
    "created": {"created", "moved_to"},
    "modified": {"modified", "closed_write"},
    "deleted": {"deleted"},
    "moved": {"moved_to", "moved_from"},
    "any": {"created", "modified", "deleted", "moved_to", "moved_from", "closed_write"},
}


def _ignored(path: str, patterns) -> bool:
    """True if *path* matches any ignore pattern.

    A pattern matches if it equals one of the path's directory/file segments
    (e.g. ``state``, ``.git``, ``__pycache__``), or globs the basename
    (``*.pyc``, ``dashboard.html``), or globs the full path.
    """
    if not patterns:
        return False
    parts = path.split(os.sep)
    base = os.path.basename(path)
    for pat in patterns:
        if pat in parts or fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(path, pat):
            return True
    return False


class _Watch:
    def __init__(self, path, on, pattern, recursive, callback, ignore=None):
        self.path = os.path.abspath(path)
        self.is_file = os.path.isfile(self.path) or "." in os.path.basename(self.path) and not os.path.isdir(self.path)
        self.dir = os.path.dirname(self.path) if self.is_file else self.path
        self.filename = os.path.basename(self.path) if self.is_file else None
        self.on = on
        self.kinds = _KIND_ALIASES.get(on, {on})
        self.pattern = pattern
        self.recursive = recursive
        self.callback = callback
        self.ignore = list(ignore or [])

    def consider(self, filepath: str, kind: str) -> None:
        if kind not in self.kinds:
            return
        if _ignored(filepath, self.ignore):
            return
        name = os.path.basename(filepath)
        if self.filename is not None and name != self.filename:
            return
        if not fnmatch.fnmatch(name, self.pattern):
            return
        try:
            self.callback(filepath, kind)
        except Exception:  # noqa: BLE001
            log.exception("watch callback for %s raised", filepath)


class _BaseWatcher:
    def __init__(self):
        self.watches: List[_Watch] = []

    def add(self, path, on="created", pattern="*", recursive=False, callback=None, ignore=None):
        self.watches.append(_Watch(path, on, pattern, recursive, callback, ignore))

    def _dispatch(self, directory: str, filename: str, kind: str) -> None:
        filepath = os.path.join(directory, filename)
        for w in self.watches:
            if os.path.abspath(directory) == w.dir or (
                w.recursive and os.path.abspath(directory).startswith(w.dir)
            ):
                w.consider(filepath, kind)

    def start(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def stop(self):  # pragma: no cover - overridden
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Backend 2: raw inotify via ctypes
# --------------------------------------------------------------------------- #
IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_ISDIR = 0x40000000

_MASK = IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
_EVENT_HEADER = struct.calcsize("iIII")

_MASK_TO_KIND = [
    (IN_CREATE, "created"),
    (IN_CLOSE_WRITE, "closed_write"),
    (IN_MODIFY, "modified"),
    (IN_MOVED_TO, "moved_to"),
    (IN_MOVED_FROM, "moved_from"),
    (IN_DELETE, "deleted"),
]


class InotifyWatcher(_BaseWatcher):
    backend = "inotify"

    def __init__(self):
        super().__init__()
        self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self._fd = -1
        self._wd_to_dir: dict[int, str] = {}
        self._watched: set[str] = set()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        self._fd = self._libc.inotify_init1(0o4000)  # IN_NONBLOCK
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        for w in self.watches:
            for directory in self._dirs_for(w):
                self._add_watch(directory)
        self._thread = threading.Thread(target=self._loop, name="agentc-inotify", daemon=True)
        self._thread.start()
        log.info("inotify watching %d director(ies)", len(self._wd_to_dir))

    def _add_watch(self, directory: str) -> None:
        if directory in self._watched:
            return
        wd = self._libc.inotify_add_watch(self._fd, directory.encode(), _MASK)
        if wd >= 0:
            self._wd_to_dir[wd] = directory
            self._watched.add(directory)

    def _covering_watch(self, path: str) -> Optional["_Watch"]:
        """Return a recursive watch that covers *path* and doesn't ignore it."""
        ap = os.path.abspath(path)
        for w in self.watches:
            if not w.recursive:
                continue
            if ap == w.dir or ap.startswith(w.dir + os.sep):
                if not _ignored(ap, w.ignore):
                    return w
        return None

    def _watch_new_tree(self, directory: str) -> None:
        """A directory just appeared under a recursive watch: start watching it
        (and any descendants), and replay files that were created inside it
        before the watch landed — inotify can't have captured those."""
        if self._covering_watch(directory) is None:
            return
        for root, subdirs, files in os.walk(directory):
            if self._covering_watch(root) is None:
                subdirs[:] = []
                continue
            self._add_watch(root)
            for name in files:
                self._dispatch(root, name, "created")

    def _dirs_for(self, w: _Watch) -> List[str]:
        if not w.recursive:
            return [w.dir]
        dirs = [w.dir]
        for root, subdirs, _ in os.walk(w.dir):
            # Prune ignored directories in place so os.walk doesn't descend them.
            subdirs[:] = [d for d in subdirs
                          if not _ignored(os.path.join(root, d), w.ignore)]
            dirs.extend(os.path.join(root, d) for d in subdirs)
        return dirs

    def _loop(self):
        buf = (ctypes.c_char * 8192)()
        while not self._stop.is_set():
            n = self._libc.read(self._fd, buf, 8192)
            if n <= 0:
                self._stop.wait(0.2)
                continue
            self._parse(bytes(buf[:n]))

    def _parse(self, data: bytes):
        offset = 0
        while offset + _EVENT_HEADER <= len(data):
            wd, mask, _cookie, length = struct.unpack_from("iIII", data, offset)
            offset += _EVENT_HEADER
            raw_name = data[offset:offset + length].split(b"\x00", 1)[0]
            offset += length
            directory = self._wd_to_dir.get(wd)
            if not directory or not raw_name:
                continue
            filename = raw_name.decode(errors="replace")
            # A new subdirectory under a recursive watch must itself be watched,
            # or files created inside it never generate events.
            if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                self._watch_new_tree(os.path.join(directory, filename))
            for bit, kind in _MASK_TO_KIND:
                if mask & bit:
                    self._dispatch(directory, filename, kind)

    def stop(self):
        self._stop.set()
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Backend 3: polling
# --------------------------------------------------------------------------- #
class PollingWatcher(_BaseWatcher):
    backend = "polling"

    def __init__(self, interval: float = 1.0):
        super().__init__()
        self.interval = interval
        self._snapshots: dict[str, dict[str, float]] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        for w in self.watches:
            self._snapshots[w.dir] = self._scan(w.dir)
        self._thread = threading.Thread(target=self._loop, name="agentc-poll", daemon=True)
        self._thread.start()
        log.info("polling watcher started (%.1fs)", self.interval)

    @staticmethod
    def _scan(directory: str) -> dict[str, float]:
        out: dict[str, float] = {}
        try:
            for name in os.listdir(directory):
                p = os.path.join(directory, name)
                if os.path.isfile(p):
                    out[name] = os.path.getmtime(p)
        except FileNotFoundError:
            pass
        return out

    def _loop(self):
        while not self._stop.is_set():
            for directory in list(self._snapshots):
                prev = self._snapshots[directory]
                now = self._scan(directory)
                for name, mtime in now.items():
                    if name not in prev:
                        self._dispatch(directory, name, "created")
                    elif mtime != prev[name]:
                        self._dispatch(directory, name, "modified")
                for name in prev:
                    if name not in now:
                        self._dispatch(directory, name, "deleted")
                self._snapshots[directory] = now
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------- #
# Backend 1: watchdog
# --------------------------------------------------------------------------- #
if _HAVE_WATCHDOG:  # pragma: no cover
    class _WDHandler(FileSystemEventHandler):
        def __init__(self, watcher: "WatchdogWatcher"):
            self.watcher = watcher

        def _emit(self, event, kind):
            if event.is_directory:
                return
            directory, filename = os.path.split(event.src_path)
            self.watcher._dispatch(directory, filename, kind)

        def on_created(self, event):
            self._emit(event, "created")

        def on_modified(self, event):
            self._emit(event, "modified")

        def on_deleted(self, event):
            self._emit(event, "deleted")

        def on_moved(self, event):
            if not event.is_directory:
                directory, filename = os.path.split(event.dest_path)
                self.watcher._dispatch(directory, filename, "moved_to")

    class WatchdogWatcher(_BaseWatcher):
        backend = "watchdog"

        def __init__(self):
            super().__init__()
            self._observer = Observer()

        def start(self):
            handler = _WDHandler(self)
            scheduled: set[str] = set()
            for w in self.watches:
                if w.dir not in scheduled:
                    self._observer.schedule(handler, w.dir, recursive=w.recursive)
                    scheduled.add(w.dir)
            self._observer.start()
            log.info("watchdog watching %d director(ies)", len(scheduled))

        def stop(self):
            self._observer.stop()
            self._observer.join(timeout=2)


def make_watcher() -> _BaseWatcher:
    """Pick the best available backend."""
    if _HAVE_WATCHDOG:
        return WatchdogWatcher()
    try:
        ctypes.CDLL("libc.so.6").inotify_init1
        return InotifyWatcher()
    except (OSError, AttributeError):  # pragma: no cover
        return PollingWatcher()
