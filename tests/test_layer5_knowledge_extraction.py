import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freya.knowledge_extractor import extract_knowledge, fingerprint
from freya.state_store import FreyaStateStore, hash_file
from freya.pipeline import handle_file
import freya.pipeline as pipeline_mod

TEST_ROOT = Path("/tmp/freya_test_vault_l5")
DB_PATH = "/tmp/freya_test_state_l5.sqlite3"


def setup():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    Path(DB_PATH).unlink(missing_ok=True)
    TEST_ROOT.mkdir(parents=True)
    pipeline_mod.CANONICAL_LOOKUP = None


def _run(rel_path: str, content: str = ""):
    """Drive a single file through hash->settle->handle_file, like the real watcher does."""
    store = FreyaStateStore(DB_PATH)
    f = TEST_ROOT / rel_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    store.touch_seen(rel_path, hash_file(f))
    handle_file(f, rel_path, "changed", store)
    return store.get(rel_path)


# ---- direct unit tests on the extractor (no pipeline plumbing needed) ----

def test_project_documentation():
    result = extract_knowledge("UsageOS", "UsageOS/README.md", "h1",
                                "UsageOS is a FastAPI + React application using PostgreSQL.",
                                "documentation")
    predicates = {f["predicate"] for f in result.facts}
    assert "IMPLEMENTED_AS" in predicates
    assert "USES" in predicates
    objs = {f["object"] for f in result.facts}
    assert "FastAPI" in objs and "React" in objs and "PostgreSQL" in objs
    print("PASS: project documentation -> architecture/implementation facts")


def test_current_runtime():
    result = extract_knowledge("ResearchOS", "ResearchOS/README.md", "h2",
                                "ResearchOS runs as a Python script on ATLAS.",
                                "documentation")
    by_pred = {f["predicate"]: f for f in result.facts}
    assert by_pred["IMPLEMENTED_AS"]["object"] == "Python"
    assert by_pred["IMPLEMENTED_AS"]["temporal_status"] == "IMPLEMENTED"
    assert by_pred["RUNS_ON"]["object"] == "ATLAS"
    assert by_pred["RUNS_ON"]["temporal_status"] == "OBSERVED_CURRENT"
    print("PASS: current runtime -> IMPLEMENTED_AS Python / RUNS_ON ATLAS (OBSERVED_CURRENT)")


def test_historical_architecture():
    result = extract_knowledge("ResearchOS", "ResearchOS/README.md", "h3",
                                "ResearchOS previously used local Mac n8n.",
                                "documentation")
    assert len(result.facts) >= 1
    f = result.facts[0]
    assert f["temporal_status"] in ("HISTORICAL", "SUPERSEDED")
    assert f["temporal_status"] != "OBSERVED_CURRENT"
    print("PASS: historical architecture -> HISTORICAL/SUPERSEDED, not current")


def test_historical_architecture_adr_style_phrasing():
    # Real-ecosystem validation caught this: the live ResearchOS note's
    # actual phrasing didn't match the narrower "previously used X"
    # pattern at all, so the flagship ResearchOS/n8n historical fact was
    # silently missed. Regression-tests the exact real phrasing.
    result = extract_knowledge(
        "ResearchOS", "ResearchOS/ResearchOS.md", "h3b",
        "ADR-001 originally specified n8n-based hosting/orchestration for ResearchOS; "
        "this is now historical/superseded by the current Python-script-on-ATLAS implementation.",
        "documentation")
    hist = [f for f in result.facts if f["predicate"] == "PREVIOUSLY_USED"]
    assert hist, "ADR-style 'originally specified...now historical/superseded' phrasing was not extracted"
    assert hist[0]["object"] == "n8n"
    assert hist[0]["temporal_status"] == "HISTORICAL"
    print("PASS: ADR-style historical phrasing ('originally specified X...now superseded') is extracted")


def test_planned_work():
    result = extract_knowledge("FREYA", "FREYA/README.md", "h4",
                                "We plan to add specialist agents.",
                                "documentation")
    assert len(result.facts) == 1
    assert result.facts[0]["fact_type"] == "PLAN"
    assert result.facts[0]["temporal_status"] == "PROPOSED"
    print("PASS: planned work -> PLAN / PROPOSED, not implemented")


def test_explicit_decision():
    result = extract_knowledge("UsageOS", "UsageOS/ADR-001.md", "h5",
                                "PostgreSQL was selected instead of SQLite.",
                                "project_specification")
    by_pred = {f["predicate"]: f for f in result.facts}
    assert by_pred["DECIDED_TO_USE"]["object"] == "PostgreSQL"
    assert by_pred["DECIDED_TO_USE"]["temporal_status"] == "DECIDED"
    assert by_pred["REJECTED"]["object"] == "SQLite"
    print("PASS: explicit decision -> DECIDED")


def test_bug_and_fix():
    result = extract_knowledge("UsageOS", "UsageOS/CHANGELOG.md", "h6",
                                "The cookieStoreId cache bug was fixed by using orgId.",
                                "documentation")
    types = {f["fact_type"] for f in result.facts}
    assert "BUG" in types and "FIX" in types
    bug = next(f for f in result.facts if f["fact_type"] == "BUG")
    fix = next(f for f in result.facts if f["fact_type"] == "FIX")
    assert bug["temporal_status"] == "HISTORICAL"
    assert fix["temporal_status"] == "IMPLEMENTED"
    print("PASS: bug/fix pair extracted with correct temporal split")


def test_unknown():
    result = extract_knowledge("ResearchOS", "ResearchOS/STATUS.md", "h7",
                                "Deployment status is currently unclear.",
                                "documentation")
    assert len(result.facts) == 1
    assert result.facts[0]["fact_type"] == "UNKNOWN"
    assert result.facts[0]["temporal_status"] == "UNKNOWN"
    assert len(result.unknowns) == 1
    print("PASS: explicitly unresolved status -> UNKNOWN, not guessed")


def test_conflict_against_canonical():
    canonical = {("ResearchOS", "RUNS_ON"): "ATLAS"}
    lookup = lambda entity, predicate: canonical.get((entity, predicate))
    result = extract_knowledge("ResearchOS", "ResearchOS/README.md", "h8",
                                "ResearchOS runs on Mac.",
                                "documentation", canonical_lookup=lookup)
    assert result.status == "NEEDS_REVIEW"
    assert len(result.conflicts) == 1
    c = result.conflicts[0]
    assert c["canonical_object"] == "ATLAS"
    assert c["new_object"] == "Mac"
    assert result.escalation is not None
    assert result.escalation["status"] == "NEEDS_REVIEW"
    assert result.escalation["canonical"]["RUNS_ON"] == "ATLAS"
    assert result.escalation["new_evidence"]["RUNS_ON"] == "Mac"
    print("PASS: conflicting current-state claim -> CONFLICT + escalation packet, no auto-overwrite")


def test_secret_redacted():
    result = extract_knowledge("UsageOS", "UsageOS/config_snippet.py", "h9",
                                'API_KEY = "sk-testkeyvalue1234567890abcdef"\nimport fastapi\n',
                                "source_code")
    blob = str(result.to_dict())
    assert "sk-testkeyvalue1234567890abcdef" not in blob
    print("PASS: secret value never reaches extraction output")


def test_duplicate_knowledge_deduplicated():
    fp1 = fingerprint("UsageOS", "UsageOS", "RUNS_ON", "ATLAS", "OBSERVED_CURRENT")
    result = extract_knowledge("UsageOS", "UsageOS/second_doc.md", "h10",
                                "UsageOS runs on ATLAS.",
                                "documentation", known_fingerprints={fp1})
    assert result.duplicate_fact_count == 1
    assert result.new_fact_count == 0
    assert len(result.facts) == 0
    print("PASS: same fact from a second source -> deduplicated by fingerprint")


def test_negated_hosting_claim_not_extracted_as_positive():
    # Caught by real-ecosystem validation against a live vault note
    # (PersonaOS.md): a negated hosting claim must not become a positive
    # HOSTED_ON/RUNS_ON fact.
    result = extract_knowledge("PersonaOS", "PersonaOS/PersonaOS.md", "h13",
                                "It is not currently hosted on ATLAS.",
                                "documentation")
    assert not any(f["predicate"] == "HOSTED_ON" and f["object"] == "ATLAS" for f in result.facts), \
        "negated hosting claim was extracted as a positive fact"
    print("PASS: negated hosting claim is not misread as a positive current-state fact")


def test_bullet_list_sentences_not_glommed():
    content = "UsageOS is hosted on ATLAS.\n- Depends on PostgreSQL for storage\n- Uses Caddy as reverse proxy\n"
    result = extract_knowledge("UsageOS", "UsageOS/README.md", "h14", content, "documentation")
    hosted = next((f for f in result.facts if f["predicate"] == "HOSTED_ON"), None)
    assert hosted is not None
    assert len(hosted["object"]) < 30, f"bullet items glommed into one fact object: {hosted['object']!r}"
    print("PASS: bullet-list lines are treated as separate sentences, not glommed together")


def test_irrelevant_file_ignored():
    result = extract_knowledge("UsageOS", "misc/scratch.log", "h11", "random log noise", "generated_or_binary")
    assert result.facts == []
    print("PASS: irrelevant/generated category -> no extraction attempted")


# ---- pipeline-level tests (full watcher -> classify -> resolve -> extract) ----

def test_pipeline_end_to_end_resolved_entity():
    rec = _run("UsageOS/README.md", "UsageOS is a FastAPI + React application using PostgreSQL.")
    assert rec.entity == "UsageOS"
    assert rec.extraction_status == "COMPLETE"
    assert rec.status == "NEEDS_REVIEW"  # Layer 6/7 not built yet — never auto-canonicalized
    assert len(rec.extracted_knowledge["facts"]) >= 2
    print("PASS: end-to-end pipeline run produces Layer 5 extraction, no canonical write")


def test_pipeline_unchanged_file_no_duplicate_extraction():
    store = FreyaStateStore(DB_PATH)
    rel = "UsageOS/backend/stable.py"
    f = TEST_ROOT / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import fastapi\napp = fastapi.FastAPI()")
    h = hash_file(f)

    store.touch_seen(rel, h)
    handle_file(f, rel, "changed", store)
    first = store.get(rel)
    assert first.extraction_status == "COMPLETE"

    # Re-settle with identical bytes (simulates a reconcile pass re-touching
    # it). Layer 5 leaves status at NEEDS_REVIEW (not VERIFIED/IGNORED,
    # since no canonical write has happened), so the Layer 1-4 touch_seen
    # gate does NOT short-circuit this on its own — changed=True is
    # expected here. The dedup guarantee for Layer 5 specifically lives at
    # the extraction level (extraction_status + extracted_content_hash),
    # asserted below via the audit trail.
    rec2, changed2 = store.touch_seen(rel, h)
    assert changed2 is True
    handle_file(f, rel, "changed", store)
    second = store.get(rel)
    audit = store.why(rel)
    assert any(a["event"] == "extraction_skipped_unchanged" for a in audit)
    assert second.extracted_knowledge == first.extracted_knowledge
    print("PASS: unchanged content -> no duplicate extraction pass")


def test_multi_entity_fanout():
    content = "services:\n  usageos:\n    build: ./UsageOS\n  n8n:\n    image: n8nio/n8n\n"
    rec = _run("Shared/docker-compose.yml", content)
    assert rec.entity_status == "MULTI_ENTITY"
    assert rec.extraction_status in ("COMPLETE", "NEEDS_REVIEW")
    subjects = {f["subject"] for f in rec.extracted_knowledge["facts"]}
    assert subjects.issubset(set(rec.multi_entities))
    print("PASS: multi-entity file fans out extraction per entity")


def test_no_canonical_writes():
    # Layer 5 must never touch Notion/** — verify no such path appears
    # anywhere in the extraction output for a file that mentions one.
    result = extract_knowledge("ATLAS", "ATLAS/notes.md", "h12",
                                "ATLAS hosts UsageOS. See Notion/ATLAS.md for details.",
                                "documentation")
    for f in result.facts:
        assert "Notion/" not in f.get("object", "")
    print("PASS: Notion/** never appears as a written artifact from Layer 5")


def test_no_llm_dependency_for_routine_cases():
    import inspect
    from freya import knowledge_extractor
    src = inspect.getsource(knowledge_extractor)
    assert "requests" not in src and "anthropic" not in src.lower() and "http" not in src.lower()
    print("PASS: knowledge extractor has zero LLM/network dependency for routine cases")


if __name__ == "__main__":
    setup()
    test_project_documentation()
    test_current_runtime()
    test_historical_architecture()
    test_historical_architecture_adr_style_phrasing()
    test_planned_work()
    test_explicit_decision()
    test_bug_and_fix()
    test_unknown()
    test_conflict_against_canonical()
    test_secret_redacted()
    test_duplicate_knowledge_deduplicated()
    test_negated_hosting_claim_not_extracted_as_positive()
    test_bullet_list_sentences_not_glommed()
    test_irrelevant_file_ignored()
    test_pipeline_end_to_end_resolved_entity()
    test_pipeline_unchanged_file_no_duplicate_extraction()
    test_multi_entity_fanout()
    test_no_canonical_writes()
    test_no_llm_dependency_for_routine_cases()
    print("\nALL LAYER 5 TESTS PASSED")