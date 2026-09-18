"""
freya/phase8_runner.py -- thin CLI wrapper for Phase 8.

Deliberately does not reimplement validation, transaction handling, or
dependency ordering -- CanonicalExecutor.execute_plan already does all of
that (topological order by depends_on, skip dependents of a failed
proposal). This module only: loads a persisted Phase 7 plan by id from
the real state store, hands it to CanonicalExecutor.execute_plan in the
requested mode, and formats/reports the result. Default mode is always
DRY_RUN; APPLY must be requested explicitly via --apply. Zero canonical
writes are possible unless --apply is passed.
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from .canonical_executor import CanonicalExecutor, MODE_DRY_RUN, MODE_APPLY
from .state_store import FreyaStateStore

DEFAULT_DB_PATH = "/Users/yoboxo/.local/share/knowledge-system/freya/freya_state.sqlite3"


def run_plan(plan_id: str, mode: str = MODE_DRY_RUN, db_path: str = DEFAULT_DB_PATH,
             store: Optional[FreyaStateStore] = None, executor: Optional[CanonicalExecutor] = None) -> dict:
    """Loads one persisted plan by id and executes it via
    CanonicalExecutor.execute_plan in the given mode. Returns a summary
    dict; never raises for ordinary validation/safety outcomes -- those
    are per-proposal statuses inside `results."""
    store = store or FreyaStateStore(db_path)
    plan = store.get_plan(plan_id)
    if plan is None:
        return {"plan_id": plan_id, "mode": mode, "error": "plan not found", "results": []}

    executor = executor or CanonicalExecutor(store)
    results = executor.execute_plan(plan, mode=mode)

    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    return {"plan_id": plan_id, "mode": mode, "proposal_count": len(plan.get("proposals", [])),
            "by_status": by_status, "results": results}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 8 operational runner: DRY_RUN / APPLY")
    parser.add_argument("--plan-id", required=True, help="Phase 7 plan_id to execute")
    parser.add_argument("--apply", action="store_true",
                         help="Actually mutate canonical vault state via the real enforcer. "
                              "Without this flag, always runs in DRY_RUN (zero canonical writes).")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH,
                         help="Path to the FREYA state SQLite database.")
    args = parser.parse_args(argv)

    mode = MODE_APPLY if args.apply else MODE_DRY_RUN
    summary = run_plan(args.plan_id, mode=mode, db_path=args.db_path)

    print(f"plan_id={summary['plan_id']} mode={summary['mode']} "
          f"proposals={summary.get('proposal_count', 0)} by_status={summary.get('by_status', {})}")
    if "error" in summary:
        print(f"error: {summary['error']}")
        return 2
    for r in summary["results"]:
        print(f"  {r['status']:<20} {r.get('action', ''):<18} {r.get('canonical_path', '')}"
              f"  reason={r.get('reason', '')}")

    failure_statuses = {"VALIDATION_FAILED", "BLOCKED", "STALE", "WRITE_FAILED", "VERIFICATION_FAILED", "ROLLBACK_FAILED"}
    had_failure = any(r["status"] in failure_statuses for r in summary["results"])
    return 1 if had_failure else 0


if __name__ == "__main__":
    sys.exit(main())