"""
freya/canonical_executor.py -- Phase 8: Canonical Write Executor.

Executes already-approved Phase 7 ChangeProposal records against the real
Obsidian vault, through the real knowledge-enforcer. This module is not a
reasoning layer: it never decides whether a fact is true, whether a
relationship exists, or whether something is current -- those decisions
belong to Layers 4-7. It only answers "is this already-approved change
safe to execute, and was it written correctly?"

Write path (never bypassed):
    Phase 7 proposal -> Phase 8 validation -> knowledge-enforcer
        -> Obsidian MCP adapter -> read-back verification -> commit/audit

There is no direct-filesystem canonical write path anywhere in this
module. All writes go through `mcp_adapter`, an injected object satisfying
the McpAdapter protocol below -- the real implementation
(ObsidianMCPAdapter, in freya/mcp_adapter.py) talks to the same
mcp-tools-istefox server FREYA's read-side already depends on. Recovery,
scope derivation, and path validation always go through the real
knowledge-enforcer module (imported from
~/.local/share/knowledge-system/enforcer/enforcer.py), never
reimplemented here.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import uuid
from pathlib import Path
from typing import Optional

from .classifier import redact_for_extraction
from .state_store import FreyaStateStore

MODE_PLAN_ONLY = "PLAN_ONLY"
MODE_DRY_RUN = "DRY_RUN"
MODE_APPLY = "APPLY"
VALID_MODES = (MODE_PLAN_ONLY, MODE_DRY_RUN, MODE_APPLY)

WRITE_ACTIONS = {
    "CREATE", "UPDATE", "APPEND", "REPLACE_SECTION",
    "ADD_RELATIONSHIP", "REMOVE_RELATIONSHIP",
    "MARK_SUPERSEDED", "MARK_HISTORICAL",
}
NO_WRITE_ACTIONS = {"NO_CHANGE", "ESCALATE", "BLOCKED"}

REQUIRED_PROPOSAL_FIELDS = (
    "proposal_id", "action", "entity", "canonical_path", "section",
    "proposed_content", "reason", "evidence", "provenance",
    "temporal_state", "confidence", "risk", "fingerprint",
)
REQUIRED_PROVENANCE_FIELDS = (
    "source_file", "evidence_classification", "temporal_state", "confidence",
)

ENFORCER_DIR = os.path.expanduser("~/.local/share/knowledge-system/enforcer")


class ExecutorError(Exception):
    pass


def _sha256(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return hashlib.sha256(text.encode()).hexdigest()


def load_real_enforcer():
    """Imports the real, external knowledge-enforcer module. Never
    reimplemented here -- if it can't be imported, that's BLOCKED, not a
    cue to substitute FREYA's own logic."""
    enforcer_path = Path(ENFORCER_DIR) / "enforcer.py"
    if not enforcer_path.exists():
        raise ExecutorError(f"enforcer not found at {enforcer_path}")
    spec = importlib.util.spec_from_file_location("knowledge_enforcer", str(enforcer_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_proposal_schema(proposal: dict) -> Optional[str]:
    """Structural validation only. Returns None if valid, else a reason
    string. Never reinterprets or repairs a malformed proposal."""
    if not isinstance(proposal, dict):
        return "proposal is not a dict"
    for field in REQUIRED_PROPOSAL_FIELDS:
        if field not in proposal:
            return f"missing required field: {field}"
    if proposal["action"] not in WRITE_ACTIONS | NO_WRITE_ACTIONS:
        return f"unsupported action: {proposal['action']}"
    if proposal["risk"] not in {"LOW", "MEDIUM", "HIGH"}:
        return f"invalid risk: {proposal['risk']}"
    return None


def validate_provenance(proposal: dict) -> Optional[str]:
    prov = proposal.get("provenance")
    if not isinstance(prov, dict):
        return "missing provenance"
    for field in REQUIRED_PROVENANCE_FIELDS:
        if not prov.get(field):
            return f"provenance missing required field: {field}"
    return None


def _contains_secret(text: str) -> bool:
    """Reuses classifier.redact_for_extraction's own _SECRET_PATTERNS
    (via its redaction side effect) rather than re-implementing secret
    detection here: if redaction changes the text, a secret-shaped
    substring was present."""
    if not text:
        return False
    return redact_for_extraction(text) != text


def scan_proposal_for_secrets(proposal: dict) -> Optional[str]:
    """Final secret-scan boundary. Phase 7 should already have redacted,
    but Phase 8 re-checks the exact bytes about to be written."""
    content = proposal.get("proposed_content") or ""
    if _contains_secret(content):
        return "secret-like content detected in proposed_content"
    return None


class McpAdapter:
    """Protocol for the canonical write mechanism. The real implementation
    (ObsidianMCPAdapter, freya/mcp_adapter.py) talks to mcp-tools-istefox.
    Tests inject an in-memory fake satisfying this same interface -- never
    a direct filesystem write, in tests or in production."""

    def ping(self) -> bool:
        raise NotImplementedError

    def read(self, canonical_path: str) -> Optional[str]:
        raise NotImplementedError

    def create(self, canonical_path: str, content: str) -> bool:
        raise NotImplementedError

    def append_to_section(self, canonical_path: str, section: str, content: str) -> bool:
        raise NotImplementedError

    def replace_section(self, canonical_path: str, section: str, content: str) -> bool:
        raise NotImplementedError

    def overwrite(self, canonical_path: str, content: str) -> bool:
        """Used only for restore-on-rollback, writing back an exact prior
        snapshot the enforcer's recovery point captured -- not a general
        canonical write path for proposals."""
        raise NotImplementedError

    def delete(self, canonical_path: str) -> bool:
        raise NotImplementedError


class CanonicalExecutor:
    def __init__(self, store: FreyaStateStore, enforcer=None, mcp_adapter: Optional[McpAdapter] = None):
        self.store = store
        self._enforcer = enforcer
        self._mcp = mcp_adapter

    @property
    def enforcer(self):
        if self._enforcer is None:
            self._enforcer = load_real_enforcer()
        return self._enforcer

    @property
    def mcp(self) -> McpAdapter:
        if self._mcp is None:
            from .mcp_adapter import ObsidianMCPAdapter
            self._mcp = ObsidianMCPAdapter()
        return self._mcp

    def _event(self, tx_id, event, result=None, detail=None):
        self.store.record_transaction_event(tx_id, event, result=result, detail=detail)

    def execute_proposal(self, proposal: dict, mode: str = MODE_DRY_RUN) -> dict:
        """Executes a single Phase 7 proposal. Returns a result dict with
        at least {"status": ..., "transaction_id": ...}. Never raises for
        ordinary validation/safety failures -- those are reported as a
        status (BLOCKED/STALE/ESCALATE/VALIDATION_FAILED/...), per spec."""
        if mode not in VALID_MODES:
            mode = MODE_DRY_RUN
        tx_id = f"tx-{uuid.uuid4().hex[:16]}"
        fingerprint = proposal.get("fingerprint", "") if isinstance(proposal, dict) else ""
        entity = proposal.get("entity") if isinstance(proposal, dict) else None
        canonical_path = proposal.get("canonical_path") if isinstance(proposal, dict) else None
        action = proposal.get("action") if isinstance(proposal, dict) else None

        self.store.record_transaction(tx_id, proposal.get("_plan_id") if isinstance(proposal, dict) else None,
                                       fingerprint, entity, canonical_path, action, mode, state="PLANNED")
        self._event(tx_id, "EXECUTION_STARTED", detail={"mode": mode, "action": action})

        def fail(status, reason):
            self.store.update_transaction(tx_id, state=status, error=reason)
            self._event(tx_id, status, result=reason)
            return {"status": status, "transaction_id": tx_id, "reason": reason,
                    "proposal_id": proposal.get("proposal_id") if isinstance(proposal, dict) else None}

        err = validate_proposal_schema(proposal)
        if err:
            return fail("VALIDATION_FAILED", err)

        if action in NO_WRITE_ACTIONS:
            self.store.update_transaction(tx_id, state="NOT_APPLICABLE")
            self._event(tx_id, "NOT_APPLICABLE", result=action)
            return {"status": "NOT_APPLICABLE", "transaction_id": tx_id,
                     "reason": f"Phase 7 action is {action}, never a write", "proposal_id": proposal.get("proposal_id")}

        persisted = self.store.get_proposal(fingerprint) if fingerprint else None
        if persisted is None:
            return fail("VALIDATION_FAILED", "proposal fingerprint not found in persisted Phase 7 proposals")
        for f in ("action", "entity", "canonical_path", "proposed_content", "fingerprint"):
            if persisted.get(f) != proposal.get(f):
                return fail("VALIDATION_FAILED", f"proposal field '{f}' does not match the persisted plan record")

        err = validate_provenance(proposal)
        if err:
            return fail("VALIDATION_FAILED", err)

        err = scan_proposal_for_secrets(proposal)
        if err:
            return fail("BLOCKED", err)

        already = self.store.get_committed_transaction_for_fingerprint(fingerprint)
        if already is not None:
            self.store.update_transaction(tx_id, state="ALREADY_APPLIED")
            self._event(tx_id, "ALREADY_APPLIED", result=already["transaction_id"])
            return {"status": "ALREADY_APPLIED", "transaction_id": tx_id,
                     "reason": f"fingerprint already committed by {already['transaction_id']}", "proposal_id": proposal.get("proposal_id")}

        try:
            enf = self.enforcer
        except ExecutorError as e:
            return fail("BLOCKED", f"enforcer unavailable: {e}")

        entity_scope = {"primary": [entity]} if entity else {"primary": []}
        try:
            derived_scope = enf.derive_filesystem_scope(entity_scope)
        except enf.EnforcerError as e:
            return fail("BLOCKED", f"no derivable filesystem scope for entity: {e}")

        path_check = enf.validate_path(canonical_path or "", derived_scope)
        if not path_check["allowed"]:
            return fail("BLOCKED", f"path rejected by enforcer: {path_check['reason']}")

        if not self.store.acquire_path_lock(canonical_path, tx_id):
            return fail("BLOCKED", f"path {canonical_path} is locked by another in-flight transaction")

        try:
            return self._execute_locked(tx_id, proposal, mode, enf, entity_scope, derived_scope, canonical_path, action, fingerprint)
        finally:
            self.store.release_path_lock(canonical_path, tx_id)

    def _execute_locked(self, tx_id, proposal, mode, enf, entity_scope, derived_scope, canonical_path, action, fingerprint):
        def fail(status, reason):
            self.store.update_transaction(tx_id, state=status, error=reason)
            self._event(tx_id, status, result=reason)
            return {"status": status, "transaction_id": tx_id, "reason": reason, "proposal_id": proposal.get("proposal_id")}

        try:
            mcp_ok = self.mcp.ping()
        except Exception:
            mcp_ok = False
        if not mcp_ok:
            return fail("BLOCKED", "Obsidian MCP unavailable")

        try:
            current_text = self.mcp.read(canonical_path)
        except Exception as e:
            return fail("BLOCKED", f"canonical read failed: {e}")

        planned_snapshot = proposal.get("current_content")
        if planned_snapshot is not None:
            if _sha256(planned_snapshot) != _sha256(current_text):
                self.store.update_transaction(tx_id, state="STALE", pre_hash=_sha256(current_text))
                self._event(tx_id, "STALE", result="canonical state changed since planning")
                return {"status": "STALE", "transaction_id": tx_id,
                         "reason": "canonical file changed since this proposal was planned -- not applied, needs replanning",
                         "proposal_id": proposal.get("proposal_id")}
        elif action != "CREATE" and current_text is None:
            return fail("VALIDATION_FAILED", "no planning-time snapshot and target file does not exist -- ambiguous, not applying")

        pre_hash = _sha256(current_text)
        self.store.update_transaction(tx_id, state="PATH_VALIDATED", pre_hash=pre_hash)
        self._event(tx_id, "PATH_VALIDATED")

        try:
            enf.create_recovery_point(
                tx_id, entity_scope, derived_scope, None, canonical_path,
                pre_existing=current_text is not None, pre_content=current_text,
                mode=mode, mcp_server="mcp-tools-istefox", intended_operation=action.lower(),
            )
            enf.update_transaction_state(tx_id, "AUTHORIZED")
            enf.update_transaction_state(tx_id, "PATH_VALIDATED")
            enf.update_transaction_state(tx_id, "RECOVERY_CREATED")
        except enf.EnforcerError as e:
            return fail("BLOCKED", f"enforcer recovery creation failed: {e}")

        if not enf.verify_recovery_point(tx_id):
            return fail("BLOCKED", "enforcer recovery point failed verification -- refusing to write")
        enf.update_transaction_state(tx_id, "RECOVERY_VERIFIED")
        self.store.update_transaction(tx_id, state="RECOVERY_CREATED", enforcer_state="RECOVERY_VERIFIED")
        self._event(tx_id, "RECOVERY_VERIFIED")

        if mode == MODE_DRY_RUN:
            # The real enforcer's transition graph only allows moving to
            # the NEXT state in its MUTATION happy-path sequence -- it has
            # no "skip ahead to VALIDATED" transition, and pushing it
            # through APPLYING/APPLIED/READ_BACK_VERIFIED here would be
            # dishonest bookkeeping (claiming APPLIED when nothing was
            # applied). Discovered by testing, not inspection: an early
            # version of this code called update_transaction_state(...,
            # "VALIDATED") directly from RECOVERY_VERIFIED and the real
            # enforcer correctly rejected it. DRY_RUN therefore leaves the
            # enforcer's own manifest at RECOVERY_VERIFIED -- the only
            # thing that actually happened -- and tracks its own
            # DRY_RUN_VALIDATED status purely in FREYA's transactions table.
            self.store.update_transaction(tx_id, state="DRY_RUN_VALIDATED", enforcer_state="RECOVERY_VERIFIED")
            self._event(tx_id, "DRY_RUN_VALIDATED", result="zero writes performed")
            return {"status": "DRY_RUN_VALIDATED", "transaction_id": tx_id,
                     "reason": "would apply; dry-run performed no write", "proposal_id": proposal.get("proposal_id"), "pre_hash": pre_hash}

        enf.update_transaction_state(tx_id, "APPLYING")
        self.store.update_transaction(tx_id, state="APPLYING", enforcer_state="APPLYING")
        self._event(tx_id, "APPLYING")

        try:
            applied = self._dispatch_write(action, proposal)
        except Exception as e:
            enf.update_transaction_state(tx_id, "APPLY_FAILED")
            self.store.update_transaction(tx_id, state="WRITE_FAILED", enforcer_state="APPLY_FAILED", error=str(e))
            self._event(tx_id, "WRITE_FAILED", result=str(e))
            return {"status": "WRITE_FAILED", "transaction_id": tx_id, "reason": f"write raised: {e}", "proposal_id": proposal.get("proposal_id")}

        if not applied:
            enf.update_transaction_state(tx_id, "APPLY_FAILED")
            self.store.update_transaction(tx_id, state="WRITE_FAILED", enforcer_state="APPLY_FAILED")
            self._event(tx_id, "WRITE_FAILED", result="mcp write returned falsy")
            return {"status": "WRITE_FAILED", "transaction_id": tx_id, "reason": "MCP write did not confirm success", "proposal_id": proposal.get("proposal_id")}

        enf.update_transaction_state(tx_id, "APPLIED")
        self.store.update_transaction(tx_id, state="APPLIED", enforcer_state="APPLIED")
        self._event(tx_id, "APPLIED")

        try:
            post_text = self.mcp.read(canonical_path)
        except Exception as e:
            return self._rollback(tx_id, enf, canonical_path, f"read-back failed: {e}")

        verify_err = self._verify_write(action, proposal, current_text, post_text)
        if verify_err:
            return self._rollback(tx_id, enf, canonical_path, verify_err)

        post_hash = _sha256(post_text)
        enf.update_transaction_state(tx_id, "READ_BACK_VERIFIED")
        enf.update_transaction_state(tx_id, "VALIDATED")
        # The real enforcer's MUTATION happy-path routes every successful
        # transaction through RESTORING/RESTORED/RESTORE_VALIDATED before
        # COMPLETE -- VALIDATED has no other outgoing transition for a
        # MUTATION transaction_type in its transition graph. FREYA performs
        # no actual content restoration here; these three calls are the
        # enforcer-mandated close-out of the recovery point, not a
        # rollback. Flagged in the Phase 8 report as an interpretation
        # forced by the real enforcer's state machine, not a guess hidden
        # from view.
        enf.update_transaction_state(tx_id, "RESTORING", note="finalizing recovery point; write succeeded, no content restore performed")
        enf.update_transaction_state(tx_id, "RESTORED")
        enf.update_transaction_state(tx_id, "RESTORE_VALIDATED")
        enf.update_transaction_state(tx_id, "COMPLETE")

        self.store.update_transaction(tx_id, state="COMMITTED", enforcer_state="COMPLETE", post_hash=post_hash)
        self._event(tx_id, "COMMITTED", result="read-back verified")
        enf.record_audit_event(tx_id, "CANONICAL_WRITE_COMMITTED", canonical_path, "success",
                                extra={"action": action, "fingerprint": fingerprint})
        return {"status": "COMMITTED", "transaction_id": tx_id, "reason": "applied and read-back verified",
                 "proposal_id": proposal.get("proposal_id"), "pre_hash": pre_hash, "post_hash": post_hash}

    def _dispatch_write(self, action: str, proposal: dict) -> bool:
        path = proposal["canonical_path"]
        content = proposal["proposed_content"]
        section = proposal.get("section")
        if action == "CREATE":
            return self.mcp.create(path, content)
        if action in ("APPEND", "ADD_RELATIONSHIP", "MARK_HISTORICAL", "MARK_SUPERSEDED"):
            return self.mcp.append_to_section(path, section, content)
        if action in ("UPDATE", "REPLACE_SECTION", "REMOVE_RELATIONSHIP"):
            return self.mcp.replace_section(path, section, content)
        raise ExecutorError(f"unsupported action for write dispatch: {action}")

    def _verify_write(self, action: str, proposal: dict, current_text: Optional[str], post_text: Optional[str]) -> Optional[str]:
        if post_text is None:
            return "read-back returned no content"
        marker = (proposal.get("proposed_content") or "").strip()
        if marker and marker not in post_text:
            return "expected content not found in canonical note after write"
        if action != "CREATE" and current_text:
            if current_text not in post_text:
                return "pre-existing canonical content was altered by the write -- unrelated content not preserved"
        if _contains_secret(post_text):
            return "secret-like content present in canonical note after write"
        return None

    def _rollback(self, tx_id: str, enf, canonical_path: str, reason: str) -> dict:
        self.store.update_transaction(tx_id, state="VERIFICATION_FAILED", error=reason)
        self._event(tx_id, "VERIFICATION_FAILED", result=reason)
        try:
            enf.update_transaction_state(tx_id, "VALIDATION_FAILED", note=reason)
        except enf.EnforcerError:
            pass
        try:
            restore = enf.determine_restore_operation(tx_id)
        except enf.EnforcerError as e:
            self.store.update_transaction(tx_id, state="RECOVERY_REQUIRED", error=str(e))
            self._event(tx_id, "RECOVERY_REQUIRED", result=str(e))
            return {"status": "RECOVERY_REQUIRED", "transaction_id": tx_id,
                     "reason": f"verification failed ({reason}) and restore plan unavailable: {e}"}

        try:
            if restore["operation"] == "restore_content":
                ok = self.mcp.overwrite(canonical_path, restore["content"])
            else:
                ok = self.mcp.delete(canonical_path)
        except Exception:
            ok = False

        if not ok:
            self.store.update_transaction(tx_id, state="ROLLBACK_FAILED")
            self._event(tx_id, "ROLLBACK_FAILED", result="restore write failed")
            return {"status": "ROLLBACK_FAILED", "transaction_id": tx_id,
                     "reason": f"verification failed ({reason}) and rollback write failed -- RECOVERY_REQUIRED"}

        try:
            restored_text = self.mcp.read(canonical_path)
        except Exception:
            restored_text = None
        if restore["operation"] == "restore_content" and _sha256(restored_text) != _sha256(restore.get("content")):
            self.store.update_transaction(tx_id, state="ROLLBACK_FAILED")
            self._event(tx_id, "ROLLBACK_FAILED", result="restored content hash mismatch")
            return {"status": "ROLLBACK_FAILED", "transaction_id": tx_id,
                     "reason": f"verification failed ({reason}); rollback completed but restored hash mismatch -- RECOVERY_REQUIRED"}

        self.store.update_transaction(tx_id, state="ROLLED_BACK")
        self._event(tx_id, "ROLLED_BACK", result="restored to exact pre-state, hash verified")
        return {"status": "ROLLED_BACK", "transaction_id": tx_id,
                 "reason": f"verification failed ({reason}); rolled back to pre-state, verified"}

    def execute_plan(self, plan: dict, mode: str = MODE_DRY_RUN) -> list[dict]:
        """Executes every proposal in a persisted Phase 7 plan, in
        dependency order. If a proposal fails, its dependents are skipped
        -- no partial graph corruption from applying a change on top of a
        failed prerequisite."""
        proposals = {p["proposal_id"]: p for p in plan.get("proposals", [])}
        order = self._topological_order(proposals)
        results = []
        blocked_ids: set = set()
        for pid in order:
            proposal = proposals[pid]
            depends_on = proposal.get("depends_on") or []
            if any(d in blocked_ids for d in depends_on):
                results.append({"status": "SKIPPED_DEPENDENCY_FAILED", "proposal_id": pid,
                                 "reason": "a dependency did not succeed"})
                blocked_ids.add(pid)
                continue
            result = self.execute_proposal(proposal, mode=mode)
            results.append(result)
            if result["status"] not in ("COMMITTED", "DRY_RUN_VALIDATED", "ALREADY_APPLIED", "NOT_APPLICABLE"):
                blocked_ids.add(pid)
        return results

    @staticmethod
    def _topological_order(proposals: dict) -> list:
        visited: set = set()
        order: list = []

        def visit(pid, stack):
            if pid in visited or pid not in proposals:
                return
            if pid in stack:
                return
            stack.add(pid)
            for dep in proposals[pid].get("depends_on") or []:
                visit(dep, stack)
            stack.discard(pid)
            visited.add(pid)
            order.append(pid)

        for pid in proposals:
            visit(pid, set())
        return order

    def recover_incomplete_transactions(self) -> list:
        """Restart-safety entry point. Never blindly replays: inspects
        current canonical hash for each incomplete transaction and only
        classifies, never re-applies automatically."""
        results = []
        for tx in self.store.list_incomplete_transactions():
            canonical_path = tx["canonical_path"]
            try:
                current_text = self.mcp.read(canonical_path) if canonical_path else None
                current_hash = _sha256(current_text)
            except Exception:
                current_hash = None

            if tx.get("post_hash") and current_hash == tx.get("post_hash"):
                classification, new_state = "APPLIED_CONFIRMED", "COMMITTED"
            elif current_hash == tx.get("pre_hash"):
                classification, new_state = "NOT_APPLIED", "RECOVERY_REQUIRED"
            else:
                classification, new_state = "AMBIGUOUS", "RECOVERY_REQUIRED"

            self.store.update_transaction(tx["transaction_id"], state=new_state)
            self._event(tx["transaction_id"], "RESTART_RECOVERY", result=classification)
            results.append({"transaction_id": tx["transaction_id"], "classification": classification, "new_state": new_state})
        return results
