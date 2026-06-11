"""A tiny thread-safe publish/subscribe event bus.

Subscriptions match by exact name or by a trailing-``*`` prefix wildcard
(``file.*`` matches ``file.created``). ``*`` alone matches everything.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Tuple

from .models import Event

log = logging.getLogger("agentc.events")

Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self):
        self._subs: List[Tuple[str, Subscriber]] = []
        self._lock = threading.RLock()

    def subscribe(self, pattern: str, callback: Subscriber) -> None:
        with self._lock:
            self._subs.append((pattern, callback))

    @staticmethod
    def _matches(pattern: str, name: str) -> bool:
        if pattern == "*" or pattern == name:
            return True
        if pattern.endswith("*"):
            return name.startswith(pattern[:-1])
        return False

    def publish(self, event: Event) -> None:
        with self._lock:
            targets = [cb for pat, cb in self._subs if self._matches(pat, event.name)]
        log.debug("event %s (%s) -> %d subscriber(s)", event.name, event.source, len(targets))
        for cb in targets:
            try:
                cb(event)
            except Exception:  # noqa: BLE001 — one bad subscriber must not sink the bus
                log.exception("subscriber for %r raised", event.name)
