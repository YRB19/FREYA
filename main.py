#!/usr/bin/env python3
"""
FREYA — main entrypoint.

Run directly for foreground testing:
    python3 main.py

Deploy via launchd for real (see com.yoboxo.freya.plist) so it survives
Mac restarts and starts at login.
"""

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

from freya.classifier import is_historical_path
from freya.pipeline import handle_file
from freya.state_store import FreyaStateStore
from freya.watcher import FreyaWatcher

DEFAULT_VAULT_ROOT = "/Users/yoboxo/Developer/Developer"
DEFAULT_STATE_DB = str(Path.home() / ".local" / "share" / "knowledge-system" / "freya" / "freya_state.sqlite3")
RECONCILE_INTERVAL_SECONDS = 15 * 60  # periodic safety net, per spec


def main() -> None:
    parser = argparse.ArgumentParser(description="FREYA autonomous knowledge maintenance watcher")
    parser.add_argument("--root", default=DEFAULT_VAULT_ROOT)
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB)
    parser.add_argument("--log-file", default=str(Path.home() / ".local" / "share" / "knowledge-system" / "freya" / "freya.log"))
    parser.add_argument("--reconcile-once", action="store_true", help="run one reconciliation pass and exit (for testing)")
    args = parser.parse_args()

    Path(args.state_db).parent.mkdir(parents=True, exist_ok=True)
    Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(args.log_file), logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("freya.main")

    store = FreyaStateStore(args.state_db)

    def on_ready(abspath: Path, rel: str, kind: str) -> None:
        # Never treat historical evidence as writable, and never process
        # anything under .obsidian (watcher already filters this, belt+suspenders here).
        try:
            handle_file(abspath, rel, kind, store)
        except Exception as e:  # isolate failures — one bad file must not kill FREYA
            log.exception("processing failed for %s", rel)
            store.set_status(rel, "FAILED", last_error=str(e),
                              retry_count=(store.get(rel).retry_count + 1 if store.get(rel) else 1))

    watcher = FreyaWatcher(args.root, store, on_ready=on_ready)

    if args.reconcile_once:
        summary = watcher.reconcile()
        print(json.dumps(summary, indent=2))
        print(json.dumps(store.health(), indent=2))
        return

    watcher.start()
    log.info("FREYA online. Watching %s", args.root)

    stop = {"flag": False}

    def _sigterm(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    last_reconcile = 0.0
    try:
        while not stop["flag"]:
            time.sleep(2)
            if time.time() - last_reconcile > RECONCILE_INTERVAL_SECONDS:
                log.info("running periodic reconciliation pass")
                summary = watcher.reconcile()
                log.info("reconciliation: %s", summary)
                last_reconcile = time.time()
    finally:
        watcher.stop()
        log.info("FREYA stopped cleanly")


if __name__ == "__main__":
    main()
