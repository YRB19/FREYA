import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freya.relationship_engine import (
    RelationshipCandidate, facts_to_candidates, evaluate_candidate,
    relationship_fingerprint, build_relationship_conflict_packet,
)
from freya.knowledge_extractor import extract_knowledge
from freya.state_store import FreyaStateStore, hash_file
from freya.pipeline import handle_file
import freya.pipeline as pipeline_mod

TEST_ROOT = Path("/tmp/freya_test_vault_l6")
DB_PATH = "/tmp/freya_test_state_l6.sqlite3"


def setup():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    Path(DB_PATH).unlink(missing_ok=True)
    TEST_ROOT.mkdir(parents=True)
    pipeline_mod.CANONICAL_LOOKUP = None
    pipeline_mod.CANONICAL_RELATIONSHIP_LOOKUP = None


def _run(rel_path: str, content: str = ""):
    store = FreyaStateStore(DB_PATH)
    f = TEST_ROOT / rel_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    store.touch_seen(rel_path, hash_file(f))
    handle_file(f, rel_path, "changed", store)
    return store


# ---- direct unit tests on facts_to_candidates / evaluate_candidate ------

def test_basic_relationship():
    result = extract_knowledge("UsageOS", "x.md", "h1", "UsageOS depends on PostgreSQL.", "documentation")
    cands = facts_to_candidates(result.facts)
    assert len(cands) == 1
    c = cands[0]
    assert c.source == "UsageOS" and c.rel_type == "DEPENDS_ON" and c.target == "PostgreSQL"
    assert c.confidence == "HIGH" or c.confidence == "MEDIUM"
    print("PASS: basic relationship -> UsageOS DEPENDS_ON PostgreSQL")


def test_runtime():
    result = extract_knowledge("ResearchOS", "x.md", "h2", "ResearchOS runs on ATLAS.", "documentation")
    cands = facts_to_candidates(result.facts)
    assert len(cands) == 1
    assert cands[0].rel_type == "RUNS_ON" and cands[0].target == "ATLAS"
    assert cands[0].temporal_status == "OBSERVED_CURRENT"
    print("PASS: runtime -> RUNS_ON ATLAS, CURRENT")


def test_implementation():
    result = extract_knowledge("ResearchOS", "x.md", "h3", "ResearchOS runs as a Python script on ATLAS.", "documentation")
    cands = facts_to_candidates(result.facts)
    impl = next(c for c in cands if c.rel_type == "IMPLEMENTED_AS")
    assert impl.target == "Python"
    assert impl.temporal_status == "IMPLEMENTED"
    print("PASS: implementation -> IMPLEMENTED_AS Python, CURRENT-flavored")


def test_historical():
    result = extract_knowledge("ResearchOS", "x.md", "h4", "ResearchOS previously used n8n.", "documentation")
    cands = facts_to_candidates(result.facts)
    assert len(cands) == 1
    assert cands[0].rel_type == "HISTORICAL"
    assert cands[0].temporal_status == "HISTORICAL"
    print("PASS: historical -> HISTORICAL type, not current")


def test_planned_not_current_hosted_on():
    # "may eventually" isn't one of Layer 5's hedge cues, so this exercises
    # the underlying guarantee via a cue Layer 5 does recognize: nothing
    # that isn't OBSERVED_CURRENT/IMPLEMENTED/DECIDED ever becomes a
    # CURRENT-flavored candidate.
    result = extract_knowledge("ClipOS", "x.md", "h5", "We plan to run ClipOS on ATLAS.", "documentation")
    cands = facts_to_candidates(result.facts)
    hosted = [c for c in cands if c.rel_type in ("HOSTED_ON", "RUNS_ON") and c.temporal_status in
              {"OBSERVED_CURRENT", "IMPLEMENTED", "DECIDED"}]
    assert hosted == [], f"a PLAN sentence produced a current-flavored hosting relationship: {hosted}"
    print("PASS: planned work never yields a CURRENT-flavored hosting relationship")


def test_unknown_no_current_relationship():
    result = extract_knowledge("CompanionOS", "x.md", "h6", "CompanionOS deployment is currently unclear.", "documentation")
    cands = facts_to_candidates(result.facts)
    assert cands == [], f"an UNKNOWN fact produced a relationship candidate: {cands}"
    print("PASS: explicitly unresolved deployment -> no relationship candidate at all")


def test_account_collision_no_integration_edge():
    # Layer 5 has no predicate that maps to INTEGRATES_WITH at all, and
    # PURPOSE/tracking facts are filtered out of facts_to_candidates
    # entirely — a "tracks account X" sentence must never become a graph
    # edge between the two projects.
    result = extract_knowledge("UsageOS", "x.md", "h7", "UsageOS tracks a Claude account named StudioOS.",
                                "documentation")
    cands = facts_to_candidates(result.facts)
    assert not any(c.rel_type == "INTEGRATES_WITH" for c in cands)
    assert not any(c.target == "StudioOS" for c in cands)
    print("PASS: account-nickname mention never produces an INTEGRATES_WITH edge")


def test_irrelevant_dependency_filtered():
    result = extract_knowledge("UsageOS", "backend.py", "h8", "import requests\nimport fastapi\n", "source_code")
    cands = facts_to_candidates(result.facts)
    targets = {c.target for c in cands}
    assert "requests" not in targets
    assert "FastAPI" in targets  # the ecosystem-relevant one still comes through
    print("PASS: 'import requests' does not become an ecosystem relationship; FastAPI does")


def test_known_target_with_trailing_clause_not_rejected():
    # Real-ecosystem validation caught this: Layer 5's bullet-derived
    # object text often carries a trailing clause ("PostgreSQL for
    # storage"), and the original exact-match granularity filter silently
    # dropped an unambiguously ecosystem-relevant dependency because of it.
    result = extract_knowledge("UsageOS", "x.md", "h8b", "Depends on PostgreSQL for storage.", "documentation")
    cands = facts_to_candidates(result.facts)
    assert any(c.rel_type == "DEPENDS_ON" and "PostgreSQL" in c.target for c in cands), \
        "a trailing clause on an otherwise-known target caused it to be filtered out"
    print("PASS: a known target with a trailing clause is still recognized")


def test_weak_co_mention_no_relationship():
    result = extract_knowledge("StudioOS", "x.md", "h9",
                                "This document mentions ClipOS and StudioOS in the same paragraph.",
                                "documentation")
    cands = facts_to_candidates(result.facts)
    assert cands == []
    print("PASS: two entities merely co-mentioned -> no relationship (no co-occurrence inference)")


# ---- evaluate_candidate (dedup / supersession / conflict) ---------------

def test_duplicate_confirms_not_duplicates():
    cand = RelationshipCandidate("UsageOS", "USES", "PostgreSQL", "IMPLEMENTED", "HIGH")
    fp = relationship_fingerprint("UsageOS", "USES", "PostgreSQL")
    existing = {"fingerprint": fp, "source_entity": "UsageOS", "rel_type": "USES", "target": "PostgreSQL",
                "temporal_status": "IMPLEMENTED", "confidence": "HIGH", "status": "ACTIVE", "evidence": [],
                "source_files": ["a.md"]}
    result = evaluate_candidate(cand, existing_exact=existing, existing_for_source=[existing])
    assert result.action == "CONFIRM"
    print("PASS: second source for the same edge -> CONFIRM, not a duplicate row")


def test_relationship_conflict():
    cand = RelationshipCandidate("UsageOS", "RUNS_ON", "Mac", "OBSERVED_CURRENT", "MEDIUM")
    existing_atlas = {"fingerprint": relationship_fingerprint("UsageOS", "RUNS_ON", "ATLAS"),
                       "source_entity": "UsageOS", "rel_type": "RUNS_ON", "target": "ATLAS",
                       "temporal_status": "OBSERVED_CURRENT", "confidence": "HIGH", "status": "ACTIVE",
                       "evidence": [], "source_files": ["b.md"]}
    result = evaluate_candidate(cand, existing_exact=None, existing_for_source=[existing_atlas])
    assert result.action == "CONFLICT"
    assert result.conflicts_with == [existing_atlas]
    packet = build_relationship_conflict_packet(cand, result.conflicts_with, ["c.md"])
    assert packet["status"] == "RELATIONSHIP_CONFLICT"
    assert packet["canonical"]["target"] == "ATLAS"
    assert packet["new_evidence"]["target"] == "Mac"
    print("PASS: two current-flavored RUNS_ON claims with different targets -> RELATIONSHIP_CONFLICT, no auto-pick")


def test_supersession():
    old = {"fingerprint": relationship_fingerprint("ResearchOS", "ORCHESTRATED_BY", "n8n"),
           "source_entity": "ResearchOS", "rel_type": "ORCHESTRATED_BY", "target": "n8n",
           "temporal_status": "IMPLEMENTED", "confidence": "HIGH", "status": "ACTIVE",
           "evidence": [], "source_files": ["old.md"]}
    new_historical = RelationshipCandidate("ResearchOS", "HISTORICAL", "n8n", "HISTORICAL", "MEDIUM")
    result = evaluate_candidate(new_historical, existing_exact=None, existing_for_source=[old])
    assert result.action == "SUPERSEDE_AND_ADD"
    assert result.supersedes == [old]
    print("PASS: explicit historical mention of an old target -> supersedes the old ACTIVE record, preserves it")


def test_low_confidence_needs_review():
    cand = RelationshipCandidate("CompanionOS", "RELATED_TO", "ATLAS", "IMPLEMENTED", "LOW")
    result = evaluate_candidate(cand, existing_exact=None, existing_for_source=[])
    assert result.action == "NEEDS_REVIEW"
    print("PASS: LOW confidence never becomes an automatic canonical-track relationship")


# ---- pipeline-level tests -------------------------------------------

def test_pipeline_end_to_end_adds_relationship():
    store = _run("UsageOS/README.md", "UsageOS is a FastAPI + React application using PostgreSQL.")
    rels = store.relationships_for_source("UsageOS")
    assert any(r["rel_type"] == "USES" and r["target"] == "PostgreSQL" for r in rels)
    assert any(r["rel_type"] == "IMPLEMENTED_AS" and r["target"] == "FastAPI" for r in rels)
    print("PASS: end-to-end pipeline run populates the relationship ledger")


def test_pipeline_confirms_not_duplicates():
    store = _run("UsageOS/README.md", "UsageOS depends on PostgreSQL.")
    store2 = _run("UsageOS/ARCHITECTURE.md", "UsageOS depends on PostgreSQL.")
    rels = store2.relationships_for_source("UsageOS")
    matching = [r for r in rels if r["rel_type"] == "DEPENDS_ON" and r["target"] == "PostgreSQL"]
    assert len(matching) == 1
    assert matching[0]["confirm_count"] == 2
    assert set(matching[0]["source_files"]) == {"UsageOS/README.md", "UsageOS/ARCHITECTURE.md"}
    print("PASS: same relationship from two files -> one row, confirm_count=2, both sources recorded")


def test_pipeline_supersession_end_to_end():
    # Realistic trigger for this path: Layer 5's own predicate set always
    # produces a HISTORICAL fact directly from hedge words in the SAME
    # sentence ("previously used n8n"), so there's no naturally-occurring
    # prior ACTIVE record for Layer 6 to flip within a single extraction
    # pass — supersession fires when an EARLIER pass already recorded
    # something as current and a LATER pass explicitly historicizes it.
    # Seed that earlier state directly (stands in for an earlier session,
    # or a relationship type Layer 5 doesn't produce yet, e.g.
    # ORCHESTRATED_BY) and confirm the pipeline wiring flips it correctly.
    store = FreyaStateStore(DB_PATH)
    store.add_relationship(
        relationship_fingerprint("ResearchOS", "ORCHESTRATED_BY", "n8n"),
        source_entity="ResearchOS", rel_type="ORCHESTRATED_BY", target="n8n",
        temporal_status="IMPLEMENTED", confidence="HIGH", evidence=[], source_file="ADR-001.md",
    )
    store = _run("ResearchOS/README.md",
                 "ResearchOS runs as a Python script on ATLAS.\nResearchOS previously used n8n.")
    rels = store.relationships_for_source("ResearchOS")
    historical = [r for r in rels if r["target"] == "n8n"]
    current = [r for r in rels if r["rel_type"] == "RUNS_ON" and r["target"] == "ATLAS"]
    assert historical and historical[0]["status"] == "SUPERSEDED"
    assert current and current[0]["status"] == "ACTIVE"
    print("PASS: a prior ACTIVE record + new historical mention -> old superseded, new active, both preserved")


def test_pipeline_conflict_end_to_end():
    store = _run("UsageOS/README.md", "UsageOS runs on ATLAS.")
    store2 = _run("UsageOS/OTHER_NOTE.md", "UsageOS runs on Mac.")
    conflicts = store2.open_relationship_conflicts()
    assert any(c["source_entity"] == "UsageOS" and c["rel_type"] == "RUNS_ON" for c in conflicts)
    print("PASS: contradicting current-state claims across two files -> open RELATIONSHIP_CONFLICT, no overwrite")


def test_no_canonical_writes():
    store = _run("ATLAS/notes.md", "ATLAS hosts services. See Notion/ATLAS.md for details.")
    for r in store.all_relationships():
        assert "Notion/" not in r["target"]
    print("PASS: Notion/** never appears as a Layer 6 relationship target/artifact")


def test_no_llm_dependency():
    import ast
    from freya import relationship_engine
    import inspect
    tree = ast.parse(inspect.getsource(relationship_engine))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.issubset({"__future__", "hashlib", "re", "dataclasses", "typing", "entity_resolver", "freya"}), \
        f"unexpected import in relationship_engine.py: {imported}"
    print("PASS: relationship engine has zero LLM/network dependency for routine cases")


if __name__ == "__main__":
    setup()
    test_basic_relationship()
    test_runtime()
    test_implementation()
    test_historical()
    test_planned_not_current_hosted_on()
    test_unknown_no_current_relationship()
    test_account_collision_no_integration_edge()
    test_irrelevant_dependency_filtered()
    test_known_target_with_trailing_clause_not_rejected()
    test_weak_co_mention_no_relationship()
    test_duplicate_confirms_not_duplicates()
    test_relationship_conflict()
    test_supersession()
    test_low_confidence_needs_review()
    setup()
    test_pipeline_end_to_end_adds_relationship()
    setup()
    test_pipeline_confirms_not_duplicates()
    setup()
    test_pipeline_supersession_end_to_end()
    setup()
    test_pipeline_conflict_end_to_end()
    setup()
    test_no_canonical_writes()
    test_no_llm_dependency()
    print("\nALL LAYER 6 TESTS PASSED")