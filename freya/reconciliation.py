"""
freya/reconciliation.py -- Phase 9A combined reconciliation (read-only).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Any

from .health_report import HealthReport, ComponentStatus, Finding
from .state_store import FreyaStateStore

# Known historical residues per spec
KNOWN_HISTORICAL_RESIDUES = {
    ("tx-d9d44f355acd4b87", "Notion/_ENFORCER_CRASH_BEFORE_WRITE.md"),
    ("tx-b6c1f10f30f34726", "Notion/_ENFORCER_TEST.md"),
    ("tx-9890a2b96e4f47a3", "Notion/_ENFORCER_TEST.md"),
}

KNOWN_RESIDUE_FULL = [
    {
        "tx_id": "tx-d9d44f355acd4b87",
        "path": "Notion/_ENFORCER_CRASH_BEFORE_WRITE.md",
        "reason": "Phase 8.4 crash residue",
    },
    {
        "tx_id": "tx-b6c1f10f30f34726",
        "path": "Notion/_ENFORCER_TEST.md",
        "reason": "Phase 8.2-A APPLY on test artifact (known historical)",
    },
    {
        "tx_id": "tx-9890a2b96e4f47a3",
        "path": "Notion/_ENFORCER_TEST.md",
        "reason": "Phase 8.2-B rollback recovery artifact (known historical)",
    },
]


class Phase9AReconciliation:
    def __init__(self, store: FreyaStateStore, recovery_dir: Optional[str] = None):
        self.store = store
        self.recovery_dir = Path(recovery_dir or os.path.expanduser("~/.local/share/knowledge-system/recovery"))
        self.finding_counter = 0

    def _next_finding_id(self, component: str) -> str:
        self.finding_counter += 1
        return f"9A-{component.upper()}-{self.finding_counter:03d}"

    def _make_finding(self, component: str, severity: str, message: str, evidence: Optional[Dict] = None) -> Finding:
        fid = self._next_finding_id(component)
        return Finding(id=fid, component=component, severity=severity, message=message, evidence=evidence or {})

    def reconcile(self) -> HealthReport:
        ts = time.time()
        report = HealthReport(overall_status="HEALTHY", timestamp=ts, recommended_action="No action required.")
        report.evidence["known_residues"] = KNOWN_RESIDUE_FULL

        # SQLite component
        sqlite_comp = ComponentStatus(name="sqlite", status="OK")
        report.components["sqlite"] = sqlite_comp

        # Transactions
        tx_comp = self._reconcile_transactions()
        report.components["transactions"] = tx_comp
        for f in tx_comp.findings:
            report.findings.append(f)
        report.warnings.extend(tx_comp.warnings)
        report.blocking_issues.extend(tx_comp.blocking)

        # Path locks
        lock_comp = self._reconcile_locks()
        report.components["path_locks"] = lock_comp
        for f in lock_comp.findings:
            report.findings.append(f)
        report.warnings.extend(lock_comp.warnings)
        report.blocking_issues.extend(lock_comp.blocking)

        # Recovery
        rec_comp = self._reconcile_recovery()
        report.components["recovery"] = rec_comp
        for f in rec_comp.findings:
            report.findings.append(f)
        report.warnings.extend(rec_comp.warnings)
        report.blocking_issues.extend(rec_comp.blocking)

        # Determine overall
        any_blocked = any(c.status == "BLOCKED" for c in report.components.values())
        any_degraded = any(c.status == "DEGRADED" for c in report.components.values())
        if any_blocked:
            report.overall_status = "BLOCKED"
            report.recommended_action = "Investigate blocking issues (read-only); do not repair."
        elif any_degraded:
            report.overall_status = "DEGRADED"
            report.recommended_action = "Review findings; read-only investigation only."
        else:
            report.overall_status = "HEALTHY"
            report.recommended_action = "No inconsistencies detected under read-only validation."

        return report

    def _reconcile_transactions(self) -> ComponentStatus:
        comp = ComponentStatus(name="transactions", status="OK")
        try:
            # Load all transactions
            with self.store._conn_ro() as conn:
                cur = conn.execute(
                    "SELECT transaction_id, plan_id, proposal_fingerprint, entity, canonical_path, action, mode, state, enforcer_state, pre_hash, post_hash, error, created_at, updated_at FROM transactions ORDER BY created_at"
                )
                txs = [dict(r) for r in cur.fetchall()]

            # Load events
            with self.store._conn_ro() as conn:
                cur = conn.execute(
                    "SELECT transaction_id, event, result, detail_json, created_at FROM transaction_events ORDER BY transaction_id, created_at"
                )
                evs = [dict(r) for r in cur.fetchall()]

            ev_by_tx: Dict[str, List[Any]] = {}
            for e in evs:
                ev_by_tx.setdefault(e["transaction_id"], []).append(e)

            comp.evidence = {"count_transactions": len(txs), "count_events": len(evs)}

            for tx in txs:
                tid = tx["transaction_id"]
                state = (tx["state"] or "").upper()
                events = ev_by_tx.get(tid, [])
                event_names = [ev["event"] for ev in events]

                # Check for lifecycle inconsistencies - basic checks
                if state in ("COMMITTED", "COMPLETE") and tx.get("post_hash") is None:
                    comp.findings.append(self._make_finding("transactions", "WARN", f"Terminal state {state} lacks post_hash", {
                        "transaction_id": tid, "state": state, "post_hash": tx.get("post_hash")
                    }))
                    comp.status = "DEGRADED"

                # Check if terminal but has non-terminal lifecycle clues? Be conservative - just report
                if state in ("AMBIGUOUS", "RECOVERY_REQUIRED"):
                    comp.findings.append(self._make_finding("transactions", "INFO", f"Transaction in {state} state", {
                        "transaction_id": tid, "state": state
                    }))

        except Exception as e:
            comp.status = "BLOCKED"
            comp.blocking.append(str(e))
        return comp

    def _reconcile_locks(self) -> ComponentStatus:
        comp = ComponentStatus(name="path_locks", status="OK")
        try:
            with self.store._conn_ro() as conn:
                cur = conn.execute("SELECT canonical_path, transaction_id, locked_at FROM path_locks ORDER BY locked_at")
                locks = [dict(r) for r in cur.fetchall()]

            comp.evidence = {"count_locks": len(locks)}

            # Build tx lookup
            tx_set = set()
            tx_states: Dict[str, str] = {}
            with self.store._conn_ro() as conn:
                cur = conn.execute("SELECT transaction_id, state FROM transactions")
                for r in cur.fetchall():
                    tx_set.add(r["transaction_id"])
                    tx_states[r["transaction_id"]] = (r["state"] or "").upper()

            for lock in locks:
                tid = lock["transaction_id"]
                path = lock["canonical_path"]
                key = (tid, path)
                if key in KNOWN_HISTORICAL_RESIDUES:
                    comp.findings.append(self._make_finding("path_locks", "INFO", "KNOWN_HISTORICAL_RESIDUE (classified, not blocked)", {
                        "transaction_id": tid,
                        "canonical_path": path,
                        "reason": "matches known Phase 8 historical residue registry"
                    }))
                    continue  # Never blocked, never auto-released

                # Check tx exists and state
                if tid not in tx_set:
                    comp.findings.append(self._make_finding("path_locks", "WARN", "Lock references non-existent transaction", {
                        "transaction_id": tid, "canonical_path": path
                    }))
                    comp.status = "DEGRADED"
                else:
                    st = tx_states.get(tid, "")
                    if st in ("COMMITTED", "COMPLETE", "APPLIED_CONFIRMED", "ALREADY_APPLIED"):
                        comp.findings.append(self._make_finding("path_locks", "WARN", f"Terminal transaction retains lock ({st})", {
                            "transaction_id": tid, "canonical_path": path, "state": st
                        }))
                        comp.status = "DEGRADED"
                    elif st in ("FAILED", "ABORTED", "NOT_APPLIED", "RECOVERY_REQUIRED"):
                        comp.findings.append(self._make_finding("path_locks", "INFO", f"Non-terminal/failed state lock present ({st})", {
                            "transaction_id": tid, "canonical_path": path, "state": st
                        }))

        except Exception as e:
            comp.status = "BLOCKED"
            comp.blocking.append(str(e))
        return comp

    def _reconcile_recovery(self) -> ComponentStatus:
        comp = ComponentStatus(name="recovery", status="OK")
        try:
            # STEP 0 documentation (from code + observable artifacts)
            comp.evidence["step0"] = {
                "artifact_meaning": "Recovery artifacts are created by knowledge-enforcer on crash/interruption during write operations. They preserve pre-state snapshots and transaction metadata to enable restart-safety classification.",
                "authoritative_fields": ["transaction_id", "canonical_path", "pre_hash", "post_hash", "mode", "action", "state", "timestamp"],
                "tx_id_mapping": "Artifacts are stored under tx-<id> directories or timestamped naming patterns; tx id is extracted from directory name when it starts with 'tx-'",
                "pre_state_hashes": "Represented as hex SHA-256 of canonical file content before write attempt (stored in artifact metadata).",
                "terminal_states_imply": "Terminal states (COMMITTED/COMPLETE/APPLIED_CONFIRMED) mean write completed; NOT_APPLIED/RECOVERY_REQUIRED mean write did not complete; AMBIGUOUS means current hash differs from both pre and post.",
            }

            if not self.recovery_dir.exists():
                comp.status = "DEGRADED"
                comp.warnings.append(f"Recovery dir missing: {self.recovery_dir}")
                return comp

            artifacts = []
            for p in sorted(self.recovery_dir.iterdir()):
                if p.is_dir():
                    name = p.name
                    txid = name if name.startswith("tx-") else None
                    artifacts.append(RecoveryArtifact(path=p, name=name, tx_id=txid, info={}))
                elif p.is_file():
                    name = p.name
                    # try to infer txid from common patterns
                    txid = None
                    if name.startswith("tx-"):
                        # maybe txid_token_...
                        parts = name.split("__")
                        if parts and parts[0].startswith("tx-"):
                            cand = parts[0]
                            if len(cand) > 3:
                                txid = cand
                    artifacts.append(RecoveryArtifact(path=p, name=name, tx_id=txid, info={}))

            comp.evidence["artifact_count"] = len(artifacts)

            # Look for the specific known residues mentioned
            # tx-9890a2b96e4f47a3 mentioned; also timestamp dirs? We saw them
            for art in artifacts:
                # Basic checks
                if art.tx_id:
                    key = (art.tx_id, "Notion/_ENFORCER_TEST.md")
                    if key in KNOWN_HISTORICAL_RESIDUES:
                        comp.findings.append(self._make_finding("recovery", "INFO", "Known historical recovery artifact (classified)", {
                            "tx_id": art.tx_id, "artifact": str(art.path)
                        }))
                        continue
                    # check other
                    if (art.tx_id, "Notion/_ENFORCER_CRASH_BEFORE_WRITE.md") in KNOWN_HISTORICAL_RESIDUES:
                        comp.findings.append(self._make_finding("recovery", "INFO", "Known historical recovery artifact (classified)", {
                            "tx_id": art.tx_id, "artifact": str(art.path)
                        }))
                        continue

                # Don't invent format - just note presence
                comp.findings.append(self._make_finding("recovery", "INFO", f"Recovery artifact present: {art.name}", {
                    "path": str(art.path), "tx_id": art.tx_id
                }))

        except Exception as e:
            comp.status = "BLOCKED"
            comp.blocking.append(str(e))
        return comp
