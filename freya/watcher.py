"""
FREYA — Filesystem Watcher (Layer 1)

Event-driven (watchdog/FSEvents on macOS) rather than polling, per spec.
Debounces rapid-fire events (editors write in bursts), computes content
hashes, and hands off to the state store for the idempotency check —
then calls `on_ready(path, record)` only for files that actually need
processing (new content, not already VERIFIED/IGNORED).

Also runs a slower periodic reconciliation pass (Layer 12 seed) to catch
anything the fs-event stream missed (the spec is explicit: never rely on
events alone).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from .classifier import is_protected_path, is_historical_path
from .state_store import FreyaStateStore, hash_file

log = logging.getLogger("freya.watcher")

DEBOUNCE_SECONDS = 1.5


def _vault_relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(self, root: Path, on_settled: Callable[[Path], None]):
        self.root = root
        self.on_settled = on_settled
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str) -> None:
        with self._lock:
            existing = self._pending.get(path)
            if existing:
                existing.cancel()
            t = threading.Timer(DEBOUNCE_SECONDS, self._fire, args=(path,))
            self._pending[path] = t
            t.daemon = True
            t.start()

    def _fire(self, path: str) -> None:
        with self._lock:
            self._pending.pop(path, None)
        self.on_settled(Path(path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            # treat old path as deleted, new path as settled-created
            self._schedule(event.dest_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)  # handled as "missing" in _settle


class FreyaWatcher:
    """
    Wraps watchdog Observer + debounce + idempotency gate + reconciliation.

    Usage:
        w = FreyaWatcher(root, store, on_ready=my_pipeline_entry)
        w.start()
        ...
        w.reconcile()   # can also be called on a timer/cron
        w.stop()
    """

    def __init__(
        self,
        root: str | Path,
        store: FreyaStateStore,
        on_ready: Callable[[Path, str, str], None],
        # on_ready(absolute_path, vault_relative_path, event_kind)
        # event_kind in {"changed", "deleted"}
        ignore_check: Optional[Callable[[str], bool]] = None,
    ):
        self.root = Path(root).resolve()
        self.store = store
        self.on_ready = on_ready
        self.ignore_check = ignore_check or (lambda rel: False)
        self._observer = Observer()
        self._handler = _DebouncedHandler(self.root, self._settle)

    def start(self) -> None:
        self._observer.schedule(self._handler, str(self.root), recursive=True)
        self._observer.start()
        log.info("FREYA watcher started on %s", self.root)

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=5)

    # ---- core settle logic (shared by live events + reconciliation) ----

    def _settle(self, abspath: Path) -> None:
        rel = _vault_relative(self.root, abspath)

        if is_protected_path(rel):
            self.store.audit(rel, None, "skipped_protected", {"reason": ".obsidian is never touched"})
            return

        if not abspath.exists():
            self.store.mark_unavailable(rel)
            self.on_ready(abspath, rel, "deleted")
            return

        if self.ignore_check(rel):
            self.store.touch_seen(rel, "")  # record we saw it, but don't hash big/irrelevant files
            self.store.set_status(rel, "IGNORED")
            return

        try:
            content_hash = hash_file(abspath)
        except OSError as e:
            self.store.audit(rel, None, "hash_failed", {"error": str(e)})
            return

        record, changed = self.store.touch_seen(rel, content_hash)
        if not changed:
            return  # idempotency: identical content already fully processed

        self.store.audit(rel, record.entity, "discovered",
                          {"content_hash": content_hash, "historical_zone": is_historical_path(rel)})
        self.on_ready(abspath, rel, "changed")

    # ---- reconciliation (Layer 12 seed) --------------------------------

    def reconcile(self) -> dict:
        """
        Walks the tree once, comparing against known state. Catches:
          - missed fs events
          - files deleted while FREYA was down
          - hash mismatches (content changed while FREYA was down)
        Returns a summary dict for health reporting.
        """
        seen_on_disk: set[str] = set()
        settled = 0
        for p in self.root.rglob("*"):
            if p.is_dir():
                continue
            rel = _vault_relative(self.root, p)
            if is_protected_path(rel):
                continue
            seen_on_disk.add(rel)

        known = self.store.known_paths()
        missing = known - seen_on_disk
        for rel in missing:
            self.store.mark_unavailable(rel)

        # re-settle anything on disk that isn't VERIFIED/IGNORED with current hash
        for p in self.root.rglob("*"):
            if p.is_dir():
                continue
            self._settle(p)
            settled += 1

        summary = {"on_disk": len(seen_on_disk), "marked_unavailable": len(missing), "rechecked": settled}
        self.store.audit(None, None, "reconciliation_pass", summary)
        return summary
