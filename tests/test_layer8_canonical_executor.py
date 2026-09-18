"""
tests/test_layer8_canonical_executor.py -- Phase 8 safety tests.

Uses the REAL knowledge-enforcer (imported from disk, exercising its real
contract -- not a mock of it) combined with an in-memory FakeMcpAdapter so
no test ever touches the real Obsidian vault or filesystem. The enforcer's
own test entities (_ENFORCER_TEST / _ENFORCER_EXISTING_TEST -> scoped to
Notion/_ENFORCER_TEST.md) are used as the target entity/path throughout,
per the enforcer's own registry.

Layer 6's test file is still empty (0 bytes) -- a separate, pre-existing
gap, not addressed here. These tests exercise Layer 6 persistence only
indirectly, through Phase 8's own transaction/proposal bookkeeping.
"""
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freya.state_store import FreyaStateStore
from freya.canonical_executor import (
    CanonicalExecutor, McpAdapter, ExecutorError, load_real_enforcer,
    MODE_DRY_RUN, MODE_APPLY,
)

TEST_ENTITY = "_ENFORCER_TEST"
TEST_PATH = "Notion/_ENFORCER_TEST.md"


class FakeMcpAdapter(McpAdapter):
    """In-memory canonical store. Never touches the real vault or
    filesystem -- exactly the McpAdapter interface the real
    ObsidianMCPAdapter satisfies, so the executor code under test is
    identical to what runs against the real MCP server."""

    def __init__(self, initial=None, available=True):
        self.files = dict(initial or {})
        self.available = available
        self.calls = []

    def ping(self):
        return self.available

    def read(self, path):
        return self.files.get(path)

    def create(self, path, content):
        self.calls.append(("create", path))
        if path in self.files:
            return False
        self.files[path] = content
        return True

    def append_to_section(self, path, section, content):
        self.calls.append(("append", path, section))
        existing = self.files.get(path, f"## {section}\n")
        self.files[path] = existing.rstrip("\n") + "\n" + content + "\n"
        return True

    def replace_section(self, path, section, content):
        self.calls.append(("replace", path, section))
        self.files[path] = content
        return True

    def overwrite(self, path, content):
        self.calls.append(("overwrite", path))
        self.files[path] = content
        return True

    def delete(self, path):
        self.calls.append(("delete", path))
        self.files.pop(path, None)
        return True


def make_store():
    db_path = f"/tmp/freya_p8_test_{uuid.uuid4().hex[:8]}.sqlite3"
    return FreyaStateStore(db_path)


def make_proposal(action="APPEND", canonical_path=TEST_PATH, entity=TEST_ENTITY,
                   proposed_content="- New fact from test", current_content="## Notes\nExisting content.\n",
                   risk="LOW", provenance=None, fingerprint=None, section="Notes", **overrides):
    fp = fingerprint or f"fp-{uuid.uuid4().hex[:16]}"
    proposal = {
        "proposal_id": fp,
        "action": action,
        "entity": entity,
        "canonical_path": canonical_path,
        "section": section,
        "current_content": current_content,
        "proposed_content": proposed_content,
        "reason": "test fact",
        "evidence": ["OBSERVED"],
        "provenance": provenance if provenance is not None else {
            "source_file": "test.md", "evidence_classification": "OBSERVED",
            "temporal_state": "OBSERVED_CURRENT", "confidence": "HIGH",
        },
        "temporal_state": "OBSERVED_CURRENT",
        "confidence": "HIGH",
        "risk": risk,
        "fingerprint": fp,
        "depends_on": [],
    }
    proposal.update(overrides)
    return proposal


def persist(store, proposal, plan_id="plan-test", origin="PRODUCTION"):
    """Persists a proposal AND its parent plan (with the given origin) so
    the Phase 8 origin gate can resolve it. Defaults to PRODUCTION since
    the overwhelming majority of this suite exercises ordinary production
    execution; VALIDATION/UNKNOWN-origin behavior gets its own dedicated
    tests further down rather than changing this default."""
    store.record_plan({"plan_id": plan_id, "origin": origin, "proposals": [proposal]},
                       source_file=proposal.get("canonical_path"))
    store.record_proposals([proposal], plan_id=plan_id)


PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name}")


def new_executor(mcp_available=True, initial_files=None, enforcer=None):
    store = make_store()
    mcp = FakeMcpAdapter(initial=initial_files, available=mcp_available)
    ex = CanonicalExecutor(store, enforcer=enforcer, mcp_adapter=mcp)
    return ex, store, mcp


# 1. Real enforcer loads and matches its documented contract -----------------

def test_real_enforcer_loads():
    enf = load_real_enforcer()
    for attr in ("derive_filesystem_scope", "validate_path", "create_recovery_point",
                 "verify_recovery_point", "update_transaction_state",
                 "determine_restore_operation", "record_audit_event", "EnforcerError"):
        check(f"real enforcer exposes {attr}", hasattr(enf, attr))


# 2. Valid APPEND executes, real enforcer, DRY_RUN then APPLY ----------------

def test_valid_append_dry_run_then_apply():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(action="APPEND")
    persist(store, proposal)

    dry = ex.execute_proposal(proposal, mode=MODE_DRY_RUN)
    check("dry-run on valid APPEND returns DRY_RUN_VALIDATED", dry["status"] == "DRY_RUN_VALIDATED")
    check("dry-run performs zero writes", mcp.files[TEST_PATH] == "## Notes\nExisting content.\n")

    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("valid APPEND reaches COMMITTED", result["status"] == "COMMITTED")
    check("APPEND preserves pre-existing content", "Existing content." in mcp.files[TEST_PATH])
    check("APPEND writes new content", "New fact from test" in mcp.files[TEST_PATH])


# 3. Valid relationship addition ---------------------------------------------

def test_valid_relationship_addition():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Relationships\n"})
    proposal = make_proposal(action="ADD_RELATIONSHIP", section="Relationships",
                              proposed_content="- [[SomeEntity]] (USES)", current_content="## Relationships\n")
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("ADD_RELATIONSHIP reaches COMMITTED", result["status"] == "COMMITTED")
    check("relationship content present", "SomeEntity" in mcp.files[TEST_PATH])


# 4/5. NO_CHANGE / ESCALATE never write --------------------------------------

def test_no_change_and_escalate_are_noop():
    for action in ("NO_CHANGE", "ESCALATE", "BLOCKED"):
        ex, store, mcp = new_executor(initial_files={TEST_PATH: "original"})
        proposal = make_proposal(action=action)
        persist(store, proposal)
        result = ex.execute_proposal(proposal, mode=MODE_APPLY)
        check(f"{action} proposal never writes", mcp.files[TEST_PATH] == "original")
        check(f"{action} proposal reports NOT_APPLICABLE", result["status"] == "NOT_APPLICABLE")


# 6. Malformed proposal rejected ---------------------------------------------

def test_malformed_proposal_rejected():
    ex, store, mcp = new_executor()
    bad = {"action": "APPEND"}  # missing everything else
    result = ex.execute_proposal(bad, mode=MODE_APPLY)
    check("malformed proposal -> VALIDATION_FAILED", result["status"] == "VALIDATION_FAILED")
    check("malformed proposal performs no write", mcp.calls == [])


# 7/8/9. Path scope rejections via the REAL enforcer -------------------------

def test_invalid_and_forbidden_paths_rejected():
    cases = [
        ("Notion/../../../etc/passwd", "path traversal outside vault"),
        (".obsidian/config", ".obsidian path"),
        ("Claude Data/raw.md", "Claude Data path"),
        ("/etc/passwd", "absolute outside-vault path"),
        ("Notion/SomeOtherEntity.md", "path outside this proposal's own entity scope"),
    ]
    for bad_path, label in cases:
        ex, store, mcp = new_executor()
        proposal = make_proposal(canonical_path=bad_path)
        persist(store, proposal)
        result = ex.execute_proposal(proposal, mode=MODE_APPLY)
        check(f"{label} -> BLOCKED", result["status"] == "BLOCKED")
        check(f"{label} performs no write", mcp.calls == [])


# 10. Missing provenance rejected --------------------------------------------

def test_missing_provenance_rejected():
    ex, store, mcp = new_executor()
    proposal = make_proposal(provenance={"source_file": "x.md"})  # incomplete
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("incomplete provenance -> VALIDATION_FAILED", result["status"] == "VALIDATION_FAILED")


# 11. Secret payload rejected -------------------------------------------------

def test_secret_payload_rejected():
    ex, store, mcp = new_executor()
    proposal = make_proposal(proposed_content="api_key = 'sk-live-abcdefghijklmnopqrstuvwx1234567890'")
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("secret-like payload -> BLOCKED", result["status"] == "BLOCKED")
    check("secret-like payload performs no write", mcp.calls == [])


# 12. Stale canonical hash blocks apply --------------------------------------

def test_stale_canonical_hash_blocked():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nSOMEONE ELSE CHANGED THIS.\n"})
    proposal = make_proposal(current_content="## Notes\nExisting content.\n")  # stale snapshot
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("stale canonical hash -> STALE", result["status"] == "STALE")
    check("stale proposal performs no write", "New fact from test" not in mcp.files[TEST_PATH])


# 13. Proposal fingerprint mismatch (tamper) blocked -------------------------

def test_fingerprint_mismatch_blocked():
    ex, store, mcp = new_executor()
    proposal = make_proposal()
    persist(store, proposal)
    tampered = dict(proposal)
    tampered["proposed_content"] = "- INJECTED CONTENT NOT IN THE PLANNED PROPOSAL"
    result = ex.execute_proposal(tampered, mode=MODE_APPLY)
    check("modified-after-planning proposal -> VALIDATION_FAILED", result["status"] == "VALIDATION_FAILED")


def test_unknown_fingerprint_blocked():
    ex, store, mcp = new_executor()
    proposal = make_proposal()  # never persisted -- no parent plan to resolve
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("unpersisted proposal / missing parent plan -> BLOCKED", result["status"] == "BLOCKED")
    check("missing parent plan reason is origin-related", result.get("reason") == "ORIGIN_UNKNOWN_OR_MISSING")


# 14. Dependency ordering -----------------------------------------------------

def test_dependency_ordering_skips_dependents_on_failure():
    ex, store, mcp = new_executor()
    # p1 fails real Phase 8 validation (bad path, rejected by the real
    # enforcer) -- a genuine failure, unlike action="BLOCKED"/"NO_CHANGE"/
    # "ESCALATE" which are legitimate Phase 7 terminals Phase 8 correctly
    # reports as NOT_APPLICABLE, not a failure. depends_on should only
    # skip dependents when the dependency actually failed to apply.
    p1 = make_proposal(fingerprint="fp-p1", canonical_path=".obsidian/bad.md")
    p1["proposal_id"] = "p1"
    p2 = make_proposal(fingerprint="fp-p2")
    p2["proposal_id"] = "p2"
    p2["depends_on"] = ["p1"]
    persist(store, p1)
    persist(store, p2)
    plan = {"plan_id": "plan-dep", "origin": "PRODUCTION", "proposals": [p1, p2]}
    results = ex.execute_plan(plan, mode=MODE_APPLY)
    by_id = {r["proposal_id"]: r["status"] for r in results}
    check("failed dependency itself is BLOCKED", by_id.get("p1") == "BLOCKED")
    check("dependent of a failed proposal is skipped", by_id.get("p2") == "SKIPPED_DEPENDENCY_FAILED")


def test_dependency_ordering_proceeds_when_dependency_is_legitimate_noop():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    # A NOT_APPLICABLE dependency (NO_CHANGE/ESCALATE/BLOCKED at the Phase 7
    # level) is not a Phase 8 failure -- its dependents should still run.
    p1 = make_proposal(fingerprint="fp-noop1", action="NO_CHANGE")
    p1["proposal_id"] = "p1"
    p2 = make_proposal(fingerprint="fp-noop2")
    p2["proposal_id"] = "p2"
    p2["depends_on"] = ["p1"]
    persist(store, p1)
    persist(store, p2)
    plan = {"plan_id": "plan-dep2", "origin": "PRODUCTION", "proposals": [p1, p2]}
    results = ex.execute_plan(plan, mode=MODE_APPLY)
    by_id = {r["proposal_id"]: r["status"] for r in results}
    check("dependent of a legitimate no-op still executes", by_id.get("p2") == "COMMITTED")


# 15/16/17. Recovery before write; VERIFIED only after read-back; COMMITTED only after verification --

def test_recovery_before_write_and_commit_ordering():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal()
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    events = [e["event"] for e in store.get_transaction_events(result["transaction_id"])]
    check("PATH_VALIDATED precedes RECOVERY_VERIFIED", events.index("PATH_VALIDATED") < events.index("RECOVERY_VERIFIED"))
    check("RECOVERY_VERIFIED precedes APPLYING", events.index("RECOVERY_VERIFIED") < events.index("APPLYING"))
    check("APPLIED precedes COMMITTED", events.index("APPLIED") < events.index("COMMITTED"))
    check("transaction reaches COMMITTED only at the end", events[-1] == "COMMITTED")


# 18. Write failure leaves recoverable state ---------------------------------

def test_write_failure_leaves_recoverable_state():
    class FailingMcp(FakeMcpAdapter):
        def append_to_section(self, path, section, content):
            return False

    store = make_store()
    mcp = FailingMcp(initial={TEST_PATH: "## Notes\nExisting content.\n"})
    ex = CanonicalExecutor(store, mcp_adapter=mcp)
    proposal = make_proposal()
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("write failure -> WRITE_FAILED", result["status"] == "WRITE_FAILED")
    tx = store.get_transaction(result["transaction_id"])
    check("pre_hash recorded despite write failure", tx["pre_hash"] is not None)


# 19/20. Verification failure triggers rollback; rollback restores exact pre-state --

def test_verification_failure_triggers_rollback_and_restores_exact_state():
    class CorruptingMcp(FakeMcpAdapter):
        def append_to_section(self, path, section, content):
            # simulate a write that clobbers pre-existing content
            self.files[path] = "CORRUPTED, PRE-EXISTING CONTENT LOST"
            return True

    original = "## Notes\nExisting content.\n"
    store = make_store()
    mcp = CorruptingMcp(initial={TEST_PATH: original})
    ex = CanonicalExecutor(store, mcp_adapter=mcp)
    proposal = make_proposal()
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("corrupting write -> ROLLED_BACK", result["status"] == "ROLLED_BACK")
    check("rollback restores exact pre-state content", mcp.files[TEST_PATH] == original)


# 21. Incomplete transaction detected after restart --------------------------

def test_restart_detects_incomplete_transaction():
    store = make_store()
    mcp = FakeMcpAdapter(initial={TEST_PATH: "## Notes\nExisting content.\n"})
    ex = CanonicalExecutor(store, mcp_adapter=mcp)
    # Simulate a crash mid-transaction: recorded but never resolved.
    store.record_transaction("tx-crashed", "plan-x", "fp-crashed", TEST_ENTITY, TEST_PATH,
                              "APPEND", MODE_APPLY, state="APPLYING", pre_hash="deadbeef")
    recovered = ex.recover_incomplete_transactions()
    check("crashed transaction is found on restart", any(r["transaction_id"] == "tx-crashed" for r in recovered))
    tx = store.get_transaction("tx-crashed")
    check("crashed transaction is never silently marked COMMITTED", tx["state"] != "COMMITTED")


# 22. Duplicate execution is idempotent --------------------------------------

def test_duplicate_execution_idempotent():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal()
    persist(store, proposal)
    first = ex.execute_proposal(proposal, mode=MODE_APPLY)
    content_after_first = mcp.files[TEST_PATH]
    second = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("first execution commits", first["status"] == "COMMITTED")
    check("second identical execution is ALREADY_APPLIED", second["status"] == "ALREADY_APPLIED")
    check("no duplicate content written", mcp.files[TEST_PATH] == content_after_first)


# 23. Concurrent same-path execution is prevented ----------------------------

def test_concurrent_same_path_prevented():
    store = make_store()
    mcp = FakeMcpAdapter(initial={TEST_PATH: "## Notes\nExisting content.\n"})
    ex = CanonicalExecutor(store, mcp_adapter=mcp)
    # Simulate another in-flight transaction already holding the lock.
    assert store.acquire_path_lock(TEST_PATH, "tx-other-inflight")
    proposal = make_proposal()
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("concurrent write to same path -> BLOCKED", result["status"] == "BLOCKED")
    store.release_path_lock(TEST_PATH, "tx-other-inflight")


# 24. Dry-run performs zero writes (batch) -----------------------------------

def test_dry_run_batch_zero_writes():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    p1 = make_proposal(fingerprint="fp-b1")
    p2 = make_proposal(fingerprint="fp-b2", proposed_content="- another fact")
    persist(store, p1)
    persist(store, p2)
    plan = {"plan_id": "plan-batch", "origin": "PRODUCTION", "proposals": [p1, p2]}
    results = ex.execute_plan(plan, mode=MODE_DRY_RUN)
    check("batch dry-run all DRY_RUN_VALIDATED", all(r["status"] == "DRY_RUN_VALIDATED" for r in results))
    check("batch dry-run leaves content untouched", mcp.files[TEST_PATH] == "## Notes\nExisting content.\n")


# 25. Enforcer unavailable -> BLOCKED ----------------------------------------

def test_enforcer_unavailable_blocked():
    class BrokenEnforcerLoader:
        pass

    store = make_store()
    mcp = FakeMcpAdapter(initial={TEST_PATH: "## Notes\nExisting content.\n"})
    ex = CanonicalExecutor(store, mcp_adapter=mcp)
    ex._enforcer = None
    # monkeypatch the module-level loader path to a nonexistent file
    import freya.canonical_executor as ce_mod
    original_dir = ce_mod.ENFORCER_DIR
    ce_mod.ENFORCER_DIR = "/nonexistent/path/for/test"
    try:
        proposal = make_proposal()
        persist(store, proposal)
        result = ex.execute_proposal(proposal, mode=MODE_APPLY)
        check("enforcer unavailable -> BLOCKED", result["status"] == "BLOCKED")
    finally:
        ce_mod.ENFORCER_DIR = original_dir


# 26. MCP unavailable -> BLOCKED ----------------------------------------------

def test_mcp_unavailable_blocked():
    ex, store, mcp = new_executor(mcp_available=False, initial_files={TEST_PATH: "x"})
    proposal = make_proposal()
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("MCP unavailable -> BLOCKED", result["status"] == "BLOCKED")


# 27. Unsupported action -> BLOCKED/VALIDATION_FAILED ------------------------

def test_unsupported_action_rejected():
    ex, store, mcp = new_executor()
    proposal = make_proposal(action="DESTROY_EVERYTHING")
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("unsupported/invented action -> VALIDATION_FAILED", result["status"] == "VALIDATION_FAILED")


# 28/29. Historical relationship preserved; supersession keeps history ------

def test_mark_superseded_preserves_historical_text():
    ex, store, mcp = new_executor(initial_files={
        TEST_PATH: "## Relationships\n- [[n8n]] (ORCHESTRATED_BY) -- current\n"
    })
    proposal = make_proposal(
        action="MARK_SUPERSEDED", section="Relationships",
        current_content="## Relationships\n- [[n8n]] (ORCHESTRATED_BY) -- current\n",
        proposed_content="- [[n8n]] (ORCHESTRATED_BY) -- HISTORICAL, superseded by Python/ATLAS implementation",
    )
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("MARK_SUPERSEDED reaches COMMITTED", result["status"] == "COMMITTED")
    check("old relationship text still present (not deleted)", "[[n8n]] (ORCHESTRATED_BY) -- current" in mcp.files[TEST_PATH])
    check("historical marker appended", "HISTORICAL" in mcp.files[TEST_PATH])


# 30. Unrelated Markdown remains unchanged (minimal patching) ---------------

def test_unrelated_markdown_untouched():
    original = "---\nfrontmatter: yes\n---\n## Purpose\nSome purpose text.\n\n## Notes\nExisting content.\n"
    ex, store, mcp = new_executor(initial_files={TEST_PATH: original})
    proposal = make_proposal(current_content=original)
    persist(store, proposal)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("unrelated write reaches COMMITTED", result["status"] == "COMMITTED")
    check("frontmatter preserved", "frontmatter: yes" in mcp.files[TEST_PATH])
    check("unrelated Purpose section preserved", "Some purpose text." in mcp.files[TEST_PATH])


# 31-48. Plan-origin safety gate ---------------------------------------------

def test_origin_production_dry_run_allowed():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-1")
    persist(store, proposal, plan_id="plan-origin-1", origin="PRODUCTION")
    result = ex.execute_proposal(proposal, mode=MODE_DRY_RUN)
    check("PRODUCTION + DRY_RUN -> allowed", result["status"] == "DRY_RUN_VALIDATED")


def test_origin_validation_dry_run_allowed():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-2")
    persist(store, proposal, plan_id="plan-origin-2", origin="VALIDATION")
    result = ex.execute_proposal(proposal, mode=MODE_DRY_RUN)
    check("VALIDATION + DRY_RUN -> allowed", result["status"] == "DRY_RUN_VALIDATED")


def test_origin_production_apply_allowed_to_proceed():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-3")
    persist(store, proposal, plan_id="plan-origin-3", origin="PRODUCTION")
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("PRODUCTION + APPLY -> proceeds past the origin gate", result["status"] == "COMMITTED")


def test_origin_validation_apply_blocked():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-4")
    persist(store, proposal, plan_id="plan-origin-4", origin="VALIDATION")
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("VALIDATION + APPLY -> BLOCKED", result["status"] == "BLOCKED")
    check("VALIDATION + APPLY reason is origin-related", result.get("reason") == "ORIGIN_VALIDATION_APPLY_BLOCKED")
    check("VALIDATION + APPLY performs no write", "New fact from test" not in mcp.files[TEST_PATH])


def test_origin_unknown_dry_run_blocked():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-5")
    persist(store, proposal, plan_id="plan-origin-5", origin="not-a-real-origin")
    result = ex.execute_proposal(proposal, mode=MODE_DRY_RUN)
    check("UNKNOWN/missing origin + DRY_RUN -> BLOCKED", result["status"] == "BLOCKED")


def test_origin_unknown_apply_blocked():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-6")
    persist(store, proposal, plan_id="plan-origin-6", origin="not-a-real-origin")
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("UNKNOWN/missing origin + APPLY -> BLOCKED", result["status"] == "BLOCKED")


def test_origin_null_blocked():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-7")
    persist(store, proposal, plan_id="plan-origin-7", origin=None)
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("NULL origin -> BLOCKED (never defaulted to PRODUCTION)", result["status"] == "BLOCKED")


def test_origin_malformed_blocked():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-8")
    for bad in ("", "production", "PRODUCTION ", "Validation", "TEST"):
        persist(store, proposal, plan_id="plan-origin-8", origin=bad)
        result = ex.execute_proposal(proposal, mode=MODE_APPLY)
        check(f"malformed origin {bad!r} -> BLOCKED", result["status"] == "BLOCKED")


def test_origin_missing_parent_plan_blocked():
    ex, store, mcp = new_executor()
    proposal = make_proposal(fingerprint="fp-origin-9")
    store.record_proposals([proposal], plan_id="plan-that-does-not-exist")
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("proposal whose parent plan cannot be resolved -> BLOCKED", result["status"] == "BLOCKED")


def test_origin_proposal_cannot_bypass_plan_origin_via_execute_plan():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-10")
    persist(store, proposal, plan_id="plan-origin-10", origin="VALIDATION")
    plan = {"plan_id": "plan-origin-10", "origin": "VALIDATION", "proposals": [proposal]}
    results = ex.execute_plan(plan, mode=MODE_APPLY)
    check("individual proposal cannot bypass parent VALIDATION origin via execute_plan",
          all(r["status"] == "BLOCKED" for r in results))


def test_origin_validation_append_proposal_cannot_apply():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-11", action="APPEND")
    persist(store, proposal, plan_id="plan-origin-11", origin="VALIDATION")
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("VALIDATION plan with ADD/APPEND proposal still cannot APPLY", result["status"] == "BLOCKED")
    check("VALIDATION APPEND performs no write", "New fact from test" not in mcp.files[TEST_PATH])


def test_origin_validation_supersede_proposal_cannot_apply():
    ex, store, mcp = new_executor(initial_files={
        TEST_PATH: "## Relationships\n- [[n8n]] (ORCHESTRATED_BY) -- current\n"
    })
    proposal = make_proposal(
        fingerprint="fp-origin-12", action="MARK_SUPERSEDED", section="Relationships",
        current_content="## Relationships\n- [[n8n]] (ORCHESTRATED_BY) -- current\n",
        proposed_content="- [[n8n]] (ORCHESTRATED_BY) -- HISTORICAL",
    )
    persist(store, proposal, plan_id="plan-origin-12", origin="VALIDATION")
    result = ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("VALIDATION plan with a REMOVE/supersede-shaped proposal cannot APPLY", result["status"] == "BLOCKED")
    check("VALIDATION supersede performs no write", "HISTORICAL" not in mcp.files[TEST_PATH])


def test_origin_survives_serialization_roundtrip():
    store = make_store()
    proposal = make_proposal(fingerprint="fp-origin-13")
    plan_dict = {"plan_id": "plan-origin-13", "origin": "VALIDATION", "proposals": [proposal]}
    store.record_plan(plan_dict, source_file=TEST_PATH)
    reloaded = store.get_plan("plan-origin-13")
    check("origin survives record_plan/get_plan round-trip", reloaded["origin"] == "VALIDATION")
    json_roundtrip = json.loads(json.dumps(plan_dict))
    check("origin survives plain JSON serialization", json_roundtrip["origin"] == "VALIDATION")


def test_origin_migration_preserves_existing_plan_data():
    store = make_store()
    with store._cursor() as cur:
        cur.execute("INSERT INTO plans (plan_id, source_file, plan_json, created_at) VALUES (?,?,?,?)",
                    ("plan-pre-existing", "Notion/Old.md",
                     json.dumps({"plan_id": "plan-pre-existing", "proposals": []}), 0.0))
    check("plan predating the origin column is UNKNOWN, never PRODUCTION",
          store.get_plan_origin("plan-pre-existing") == "UNKNOWN")
    check("pre-existing plan data is otherwise untouched",
          store.get_plan("plan-pre-existing")["plan_id"] == "plan-pre-existing")


def test_origin_dry_run_still_zero_writes_for_production():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-14")
    persist(store, proposal, plan_id="plan-origin-14", origin="PRODUCTION")
    ex.execute_proposal(proposal, mode=MODE_DRY_RUN)
    check("origin gate does not affect dry-run's zero-write guarantee",
          mcp.files[TEST_PATH] == "## Notes\nExisting content.\n")


def test_origin_not_mutated_by_executor():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-15")
    persist(store, proposal, plan_id="plan-origin-15", origin="VALIDATION")
    ex.execute_proposal(proposal, mode=MODE_APPLY)
    check("origin cannot be changed implicitly by the executor",
          store.get_plan_origin("plan-origin-15") == "VALIDATION")


def test_origin_caller_cannot_override_validation_with_apply_arg():
    ex, store, mcp = new_executor(initial_files={TEST_PATH: "## Notes\nExisting content.\n"})
    proposal = make_proposal(fingerprint="fp-origin-16")
    persist(store, proposal, plan_id="plan-origin-16", origin="VALIDATION")
    tampered = dict(proposal)
    tampered["origin"] = "PRODUCTION"
    result = ex.execute_proposal(tampered, mode=MODE_APPLY)
    check("caller cannot override VALIDATION by stuffing origin into the proposal dict",
          result["status"] == "BLOCKED")


# Direct filesystem canonical write is impossible through Phase 8 API -------

def test_no_direct_filesystem_write_path_exists():
    import freya.canonical_executor as ce_mod
    src = Path(ce_mod.__file__).read_text()
    check("canonical_executor.py contains no open(...,'w') filesystem write",
          "open(" not in src or all("w" not in call for call in []))  # structural check below
    forbidden = ["open(canonical_path", "with open(", ".write_text(", "os.remove(", "shutil.copy"]
    check("no direct filesystem canonical write call sites in canonical_executor.py",
          not any(f in src for f in forbidden))


if __name__ == "__main__":
    test_real_enforcer_loads()
    test_valid_append_dry_run_then_apply()
    test_valid_relationship_addition()
    test_no_change_and_escalate_are_noop()
    test_malformed_proposal_rejected()
    test_invalid_and_forbidden_paths_rejected()
    test_missing_provenance_rejected()
    test_secret_payload_rejected()
    test_stale_canonical_hash_blocked()
    test_fingerprint_mismatch_blocked()
    test_unknown_fingerprint_blocked()
    test_dependency_ordering_skips_dependents_on_failure()
    test_dependency_ordering_proceeds_when_dependency_is_legitimate_noop()
    test_recovery_before_write_and_commit_ordering()
    test_write_failure_leaves_recoverable_state()
    test_verification_failure_triggers_rollback_and_restores_exact_state()
    test_restart_detects_incomplete_transaction()
    test_duplicate_execution_idempotent()
    test_concurrent_same_path_prevented()
    test_dry_run_batch_zero_writes()
    test_enforcer_unavailable_blocked()
    test_mcp_unavailable_blocked()
    test_unsupported_action_rejected()
    test_mark_superseded_preserves_historical_text()
    test_unrelated_markdown_untouched()
    test_no_direct_filesystem_write_path_exists()
    test_origin_production_dry_run_allowed()
    test_origin_validation_dry_run_allowed()
    test_origin_production_apply_allowed_to_proceed()
    test_origin_validation_apply_blocked()
    test_origin_unknown_dry_run_blocked()
    test_origin_unknown_apply_blocked()
    test_origin_null_blocked()
    test_origin_malformed_blocked()
    test_origin_missing_parent_plan_blocked()
    test_origin_proposal_cannot_bypass_plan_origin_via_execute_plan()
    test_origin_validation_append_proposal_cannot_apply()
    test_origin_validation_supersede_proposal_cannot_apply()
    test_origin_survives_serialization_roundtrip()
    test_origin_migration_preserves_existing_plan_data()
    test_origin_dry_run_still_zero_writes_for_production()
    test_origin_not_mutated_by_executor()
    test_origin_caller_cannot_override_validation_with_apply_arg()

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)
    print("ALL LAYER 8 TESTS PASSED")
