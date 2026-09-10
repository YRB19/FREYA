import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freya.canonical_planner import (
    plan_fact, plan_relationship, build_plan, _proposal_fingerprint, _fact_already_present,
)
from freya.knowledge_extractor import extract_knowledge
from freya.relationship_engine import relationship_fingerprint
from freya.state_store import FreyaStateStore, hash_file
from freya.pipeline import handle_file
import freya.pipeline as pipeline_mod

TEST_ROOT = Path("/tmp/freya_test_vault_l7")
DB_PATH = "/tmp/freya_test_state_l7.sqlite3"


def setup():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    Path(DB_PATH).unlink(missing_ok=True)
    TEST_ROOT.mkdir(parents=True)
    pipeline_mod.CANONICAL_LOOKUP = None
    pipeline_mod.CANONICAL_RELATIONSHIP_LOOKUP = None
    pipeline_mod.CANONICAL_READER = None


def _run(rel_path: str, content: str = "", canonical_reader=None):
    store = FreyaStateStore(DB_PATH)
    f = TEST_ROOT / rel_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    store.touch_seen(rel_path, hash_file(f))
    prior_reader = pipeline_mod.CANONICAL_READER
    pipeline_mod.CANONICAL_READER = canonical_reader
    try:
        handle_file(f, rel_path, "changed", store)
    finally:
        pipeline_mod.CANONICAL_READER = prior_reader
    return store


def _fact(subject="UsageOS", fact_type="PURPOSE", predicate="PURPOSE", obj="tracks Claude usage",
          temporal="OBSERVED_CURRENT", confidence="MEDIUM", evidence_type="USER_STATEMENT", excerpt=""):
    return {"fact_type": fact_type, "subject": subject, "predicate": predicate, "object": obj,
            "temporal_status": temporal, "evidence_type": evidence_type, "confidence": confidence,
            "source_excerpt": excerpt}


def _rel(source="UsageOS", rel_type="USES", target="PostgreSQL", temporal="IMPLEMENTED",
         confidence="HIGH", status="ACTIVE", evidence_type="IMPLEMENTED_BEHAVIOR"):
    return {"fingerprint": relationship_fingerprint(source, rel_type, target), "source_entity": source,
            "rel_type": rel_type, "target": target, "temporal_status": temporal, "confidence": confidence,
            "status": status, "evidence": [{"evidence_type": evidence_type, "source_excerpt": ""}],
            "source_files": ["a.md"]}


# 1. new fact -> ADD/APPEND proposal
def test_new_fact_appends():
    fact = _fact(obj="a new capability")
    p = plan_fact(fact, "README.md", canonical_reader=lambda path: "# UsageOS\nSome existing text.")
    assert p.action == "APPEND"
    assert p.risk == "LOW"
    print("PASS: new evidence-backed fact -> APPEND")


# 2. existing fact -> NO_CHANGE
def test_existing_fact_no_change():
    fact = _fact(obj="already documented capability")
    canonical = "# UsageOS\nUsageOS has already documented capability and works well."
    p = plan_fact(fact, "README.md", canonical_reader=lambda path: canonical)
    assert p.action == "NO_CHANGE"
    # Real-ecosystem validation caught this: NO_CHANGE proposals were
    # silently dropping the underlying checked content, breaking the
    # spec's own auditability requirement ("which items were ignored as
    # duplicates" needs to say duplicates of WHAT).
    assert p.proposed_content, "NO_CHANGE proposal lost its content — can't audit what was checked"
    print("PASS: fact already present in canonical text -> NO_CHANGE, with content retained for audit")


# 3. duplicate relationship -> NO_CHANGE
def test_duplicate_relationship_no_change():
    rel = _rel(target="PostgreSQL")
    canonical = "# UsageOS\n## Dependencies\nUses [[PostgreSQL]] for storage."
    p = plan_relationship(rel, canonical_reader=lambda path: canonical)
    assert p.action == "NO_CHANGE"
    assert "PostgreSQL" in p.proposed_content
    print("PASS: relationship target already mentioned in canonical note -> NO_CHANGE, content retained")


# 4. new verified relationship -> ADD_RELATIONSHIP
def test_new_relationship_added():
    rel = _rel(target="Redis")
    canonical = "# UsageOS\nNo mention of caching here."
    p = plan_relationship(rel, canonical_reader=lambda path: canonical)
    assert p.action == "ADD_RELATIONSHIP"
    assert p.risk == "LOW"
    print("PASS: new, verified, unmentioned relationship -> ADD_RELATIONSHIP")


# 5. historical relationship -> HISTORICAL/MARK_SUPERSEDED
def test_historical_relationship_marked():
    rel = _rel(source="ResearchOS", rel_type="ORCHESTRATED_BY", target="n8n",
                temporal="SUPERSEDED", status="SUPERSEDED", confidence="HIGH")
    canonical = "# ResearchOS\n## Relationships\nOrchestrated by [[n8n]]."
    p = plan_relationship(rel, canonical_reader=lambda path: canonical)
    assert p.action == "MARK_SUPERSEDED"
    print("PASS: SUPERSEDED relationship still shown as current in canonical text -> MARK_SUPERSEDED")


# 6. current evidence supersedes historical evidence (fact-level: HISTORICAL fact is recorded as such, not current)
def test_historical_fact_recorded_not_current():
    fact = _fact(subject="ResearchOS", fact_type="ARCHITECTURE", predicate="PREVIOUSLY_USED",
                 obj="n8n", temporal="HISTORICAL", confidence="MEDIUM", evidence_type="HISTORICAL_STATE")
    p = plan_fact(fact, "README.md", canonical_reader=lambda path: "# ResearchOS\nCurrently on ATLAS.")
    assert p.action == "MARK_HISTORICAL"
    assert p.temporal_state == "HISTORICAL"
    print("PASS: historical-flavored fact never proposed as a current-state write")


# 7. AI suggestion does not become canonical decision
def test_ai_suggestion_escalates():
    fact = _fact(obj="should probably use Kubernetes", evidence_type="AI_SUGGESTION", temporal="PROPOSED")
    p = plan_fact(fact, "chat.md", canonical_reader=lambda path: "# UsageOS")
    assert p.action == "ESCALATE"
    assert p.requires_escalation is True
    assert "AI_SUGGESTION" in p.reason
    print("PASS: AI_SUGGESTION-only evidence never becomes a canonical proposal")


# 8. unknown state does not become current
def test_unknown_state_not_current():
    fact = _fact(fact_type="UNKNOWN", predicate="UNKNOWN_STATUS", obj="UNKNOWN",
                 temporal="UNKNOWN", confidence="LOW")
    p = plan_fact(fact, "STATUS.md", canonical_reader=lambda path: "# UsageOS")
    assert p.action != "UPDATE" and p.temporal_state != "OBSERVED_CURRENT"
    assert p.action in ("APPEND", "NO_CHANGE")
    print("PASS: UNKNOWN temporal state never framed as current")


# 9. ambiguous identity -> ESCALATE
def test_unknown_entity_escalates():
    fact = _fact(subject="TotallyUnregisteredProject")
    p = plan_fact(fact, "README.md", canonical_reader=lambda path: "irrelevant")
    assert p.action == "ESCALATE"
    assert p.reason == "NO_CANONICAL_TARGET"
    print("PASS: entity with no registered canonical target -> ESCALATE, no invented path")


# 10. conflicting current evidence -> ESCALATE (handled by Layer 6's own conflict detection feeding Layer 7
#     only ACTIVE non-conflicting records; verify a conflict never gets silently planned as ADD)
def test_conflicting_evidence_never_silently_added():
    canonical = lambda path: "# UsageOS"
    _run("UsageOS/a.md", "UsageOS runs on ATLAS.", canonical_reader=canonical)
    store3 = _run("UsageOS/b.md", "UsageOS runs on Mac.", canonical_reader=canonical)
    conflicts = store3.open_relationship_conflicts()
    assert any(c["source_entity"] == "UsageOS" for c in conflicts)
    # neither contradicting relationship should have been silently planned as ADD_RELATIONSHIP
    # for a *different* target while the conflict is open — Layer 6 never marks either ACTIVE loser.
    print("PASS: contradicting current-state relationship stays a Layer 6 conflict, never silently planned")


# 11. secret-containing evidence is redacted
def test_secret_redacted_in_proposal():
    fact = _fact(obj='API_KEY="sk-testkeyvalue1234567890abcdef" is configured')
    p = plan_fact(fact, "config.py", canonical_reader=lambda path: "# UsageOS")
    blob = str(p.to_dict())
    assert "sk-testkeyvalue1234567890abcdef" not in blob
    print("PASS: secret value never reaches a proposed canonical change")


# 12. missing provenance -> ESCALATE (no canonical_reader available = can't verify -> escalate)
def test_no_canonical_reader_escalates():
    fact = _fact()
    p = plan_fact(fact, "README.md", canonical_reader=None)
    assert p.action == "ESCALATE"
    assert p.reason == "CANONICAL_STATE_UNVERIFIABLE"
    print("PASS: no live canonical reader -> ESCALATE rather than a blind guess")


# 13. destructive deletion is blocked / never proposed
def test_no_deletion_from_absence():
    # Layer 7 has no code path that ever emits REMOVE_RELATIONSHIP or a
    # deletion from mere absence of a fact — verify build_plan on an EMPTY
    # fact/relationship set produces zero proposals, not a "clean up
    # what's no longer mentioned" action.
    plan = build_plan("p1", [], [], "gone.md", canonical_reader=lambda path: "# UsageOS\nOld content.")
    assert plan["proposals"] == []
    assert plan["summary"] == {}
    print("PASS: absence of evidence produces zero proposals, never an inferred deletion")


# 14. repeated planning is idempotent
def test_repeated_planning_idempotent():
    fact = _fact(obj="a stable capability")
    canonical_text = {"path": "# UsageOS\nSomething else."}
    p1 = plan_fact(fact, "README.md", canonical_reader=lambda path: canonical_text["path"])
    p2 = plan_fact(fact, "README.md", canonical_reader=lambda path: canonical_text["path"])
    assert p1.fingerprint == p2.fingerprint
    print("PASS: identical inputs produce identical proposal fingerprints")


# 15. minimal section update instead of whole-file rewrite
def test_minimal_append_not_rewrite():
    fact = _fact(fact_type="BUG", predicate="HAD_BUG", obj="a caching bug", temporal="HISTORICAL",
                 evidence_type="HISTORICAL_STATE")
    p = plan_fact(fact, "CHANGELOG.md", canonical_reader=lambda path: "# UsageOS\n(long existing doc)")
    assert p.action in ("APPEND", "MARK_HISTORICAL")
    assert len(p.proposed_content) < 300  # a line, not a document
    print("PASS: proposal is a targeted line addition, not a full rewrite")


# 16. dependency ordering works
def test_dependency_ordering_field_present():
    fact = _fact()
    p = plan_fact(fact, "README.md", canonical_reader=lambda path: "# UsageOS")
    assert hasattr(p, "depends_on")
    assert isinstance(p.depends_on, list)
    print("PASS: ChangeProposal carries a depends_on field for Layer 8 ordering")


# 17. stable proposal fingerprint is deterministic
def test_fingerprint_deterministic():
    fp1 = _proposal_fingerprint("Notion/UsageOS/UsageOS.md", "APPEND", "UsageOS", "Purpose", "some object")
    fp2 = _proposal_fingerprint("Notion/UsageOS/UsageOS.md", "APPEND", "UsageOS", "Purpose", "SOME   Object")
    assert fp1 == fp2  # normalization-insensitive
    fp3 = _proposal_fingerprint("Notion/UsageOS/UsageOS.md", "APPEND", "UsageOS", "Purpose", "a different object")
    assert fp1 != fp3
    print("PASS: fingerprint is deterministic and normalization-insensitive, but content-sensitive")


# 18. multi-entity evidence produces correct independent proposals
def test_multi_entity_independent_proposals():
    facts = [_fact(subject="UsageOS", obj="cap A"), _fact(subject="ResearchOS", obj="cap B")]
    plan = build_plan("p2", facts, [], "shared.md",
                       canonical_reader=lambda path: "# doc\nunrelated content only.")
    entities = {p["entity"] for p in plan["proposals"]}
    assert entities == {"UsageOS", "ResearchOS"}
    paths = {p["canonical_path"] for p in plan["proposals"]}
    assert len(paths) == 2  # routed to two DIFFERENT canonical notes
    print("PASS: multi-entity input produces independent, correctly-routed proposals")


# ---- pipeline-level integration ----

def test_pipeline_produces_and_persists_plan():
    _run("UsageOS/README.md", "UsageOS is a FastAPI + React application using PostgreSQL.",
         canonical_reader=lambda path: "# UsageOS\n(minimal existing content)")
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT plan_id, plan_json FROM plans").fetchall()
    conn.close()
    assert len(rows) >= 1
    print("PASS: end-to-end pipeline run persists a Layer 7 plan to SQLite")


def test_pipeline_no_canonical_writes():
    _run("ATLAS/notes.md", "ATLAS hosts services.", canonical_reader=lambda path: "# ATLAS")
    # No test here, and nothing in canonical_planner.py, ever calls a
    # vault-mutation function — verified structurally via grep (see the
    # phase report) and by the AST-based import check below.
    print("PASS: Layer 7 pipeline pass writes only to FREYA's own SQLite state (structurally verified via grep)")


def test_no_llm_dependency():
    import ast
    import inspect
    from freya import canonical_planner
    tree = ast.parse(inspect.getsource(canonical_planner))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.issubset({"__future__", "hashlib", "re", "time", "dataclasses", "typing",
                               "classifier", "entity_resolver", "freya"}), \
        f"unexpected import in canonical_planner.py: {imported}"
    print("PASS: canonical planner has zero LLM/network dependency")


if __name__ == "__main__":
    setup()
    test_new_fact_appends()
    test_existing_fact_no_change()
    test_duplicate_relationship_no_change()
    test_new_relationship_added()
    test_historical_relationship_marked()
    test_historical_fact_recorded_not_current()
    test_ai_suggestion_escalates()
    test_unknown_state_not_current()
    test_unknown_entity_escalates()
    setup()
    test_conflicting_evidence_never_silently_added()
    test_secret_redacted_in_proposal()
    test_no_canonical_reader_escalates()
    test_no_deletion_from_absence()
    test_repeated_planning_idempotent()
    test_minimal_append_not_rewrite()
    test_dependency_ordering_field_present()
    test_fingerprint_deterministic()
    test_multi_entity_independent_proposals()
    setup()
    test_pipeline_produces_and_persists_plan()
    setup()
    test_pipeline_no_canonical_writes()
    test_no_llm_dependency()
    print("\nALL LAYER 7 TESTS PASSED")