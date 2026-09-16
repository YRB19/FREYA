import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freya.entity_resolver import resolve_entity, known_entity_names, canonical_path_for_entity
from freya.state_store import FreyaStateStore, hash_file
from freya.pipeline import handle_file

TEST_ROOT = Path("/tmp/freya_test_vault_l4")
DB_PATH = "/tmp/freya_test_state_l4.sqlite3"


def setup():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    Path(DB_PATH).unlink(missing_ok=True)
    TEST_ROOT.mkdir(parents=True)


def _run(rel_path: str, content: str = ""):
    """Drive a single file through hash->settle->handle_file, like the real watcher does."""
    store = FreyaStateStore(DB_PATH)
    f = TEST_ROOT / rel_path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    store.touch_seen(rel_path, hash_file(f))
    handle_file(f, rel_path, "changed", store)
    return store.get(rel_path)


def test_obvious_usageos():
    rec = _run("UsageOS/backend/main.py", "from fastapi import FastAPI\napp = FastAPI()")
    assert rec.entity == "UsageOS"
    assert rec.confidence == "HIGH"
    assert rec.entity_status == "RESOLVED"
    print("PASS: obvious UsageOS path -> HIGH confidence")


def test_obvious_researchos():
    rec = _run("ResearchOS/researchos.py", "# ResearchOS main script")
    assert rec.entity == "ResearchOS"
    assert rec.confidence == "HIGH"
    print("PASS: obvious ResearchOS path -> HIGH confidence")


def test_infrastructure_n8n():
    rec = _run("Infrastructure/n8n/workflows/sync.json", '{"name": "sync workflow"}')
    assert rec.entity == "Infrastructure/n8n"
    assert rec.confidence == "HIGH"
    print("PASS: Infrastructure/n8n path -> HIGH confidence")


def test_shared_infrastructure_multi_entity():
    # docker-compose referencing two real services should trigger MULTI_ENTITY
    content = "services:\n  usageos:\n    build: ./UsageOS\n  n8n:\n    image: n8nio/n8n\n"
    rec = _run("Shared/docker-compose.yml", content)
    assert rec.entity_status == "MULTI_ENTITY", rec.entity_status
    assert "UsageOS" in rec.multi_entities or "Infrastructure/n8n" in rec.multi_entities
    print("PASS: shared docker-compose.yml -> MULTI_ENTITY")


def test_ambiguous_two_entities_similar_strength():
    # No path signal at all; content weakly mentions two unrelated entities
    # with comparable strength -> AMBIGUOUS, not an arbitrary pick.
    content = "This note discusses both companionos and personaos integration ideas."
    rec = _run("Shared/notes/random_idea.md", content)
    assert rec.entity_status == "AMBIGUOUS", rec.entity_status
    assert "CompanionOS" in rec.entity_candidates and "PersonaOS" in rec.entity_candidates
    print("PASS: comparably-weak dual mention -> AMBIGUOUS (no arbitrary pick)")


def test_unknown_generic_file():
    rec = _run("misc/todo.txt", "buy milk\ncall dentist")
    assert rec.entity_status == "UNKNOWN"
    assert rec.entity is None
    print("PASS: generic unrelated file -> UNKNOWN")


def test_name_collision_account_nickname_suppressed():
    # File clearly belongs to UsageOS by path; content mentions "StudioOS"
    # ONLY as an account/subscription-tier label. Must NOT flip resolution
    # to StudioOS, and the mention must not even count as evidence.
    content = '{"account_nickname": "StudioOS", "subscription_tier": "pro", "usage_hours": 12}'
    rec = _run("UsageOS/data/account_log.json", content)
    assert rec.entity == "UsageOS", f"expected UsageOS, got {rec.entity}"
    assert not any(e.get("entity") == "StudioOS" for e in rec.entity_evidence), \
        "account-nickname mention leaked into entity evidence"
    print("PASS: StudioOS-as-account-nickname does not hijack resolution")


def test_historical_file_still_resolves():
    # Old ClipOS architecture doc under Claude Data (historical zone) —
    # entity resolution must still succeed; temporal status is a later concern.
    rec = _run("Claude Data/ClipOS/old_architecture.md", "# ClipOS Architecture (2025)\nPipeline design.")
    assert rec.entity == "ClipOS"
    print("PASS: historical ClipOS doc still resolves to ClipOS entity")


def test_duplicate_event_same_result_no_reprocessing():
    store = FreyaStateStore(DB_PATH)
    rel = "UsageOS/backend/dup_test.py"
    f = TEST_ROOT / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# usageos worker")
    h = hash_file(f)

    rec1, changed1 = store.touch_seen(rel, h)
    handle_file(f, rel, "changed", store)
    store.set_status(rel, "VERIFIED")  # simulate full pipeline completion downstream

    rec2, changed2 = store.touch_seen(rel, h)  # identical bytes again
    assert changed1 is True
    assert changed2 is False
    final = store.get(rel)
    assert final.entity == "UsageOS"
    print("PASS: duplicate event does not re-trigger entity resolution")


def test_rename_entity_remains_stable():
    # Same content, different (but still UsageOS-scoped) path -> same entity.
    rec_old = _run("UsageOS/backend/worker_old_name.py", "# usageos background worker")
    rec_new = _run("UsageOS/backend/worker_new_name.py", "# usageos background worker")
    assert rec_old.entity == rec_new.entity == "UsageOS"
    print("PASS: rename within same project scope keeps entity stable")


def test_scene_map_distinct_from_studioos_project():
    rec = _run("StudioOS - Chapter 1 Scene Map/Scene 4.md", "Scene 4: the confrontation.")
    assert rec.entity == "StudioOS:SceneMap", rec.entity
    print("PASS: Scene Map path resolves to distinct sub-entity, not plain StudioOS")


def test_clipos_logs_root_level_file():
    # Not under ClipOS/, but filename itself is a strong marker.
    rec = _run("ClipOS Logs.md", "Build log entry for clipos pipeline run.")
    assert rec.entity == "ClipOS", rec.entity
    print("PASS: root-level 'ClipOS Logs.md' still resolves via filename marker")


def test_no_llm_dependency_for_routine_cases():
    # Sanity: resolve_entity is a pure function with no network/tool calls.
    import inspect
    from freya import entity_resolver
    src = inspect.getsource(entity_resolver)
    assert "requests" not in src and "http" not in src.lower() and "anthropic" not in src.lower()
    print("PASS: entity resolver has zero LLM/network dependency for routine cases")


def test_canonical_path_for_entity_known_and_unknown():
    # Regression: canonical_path_for_entity() was referenced by Layer 7's
    # canonical_planner.py but missing from entity_resolver.py entirely
    # (ImportError on import). Reads the same ENTITY_REGISTRY.vault_note
    # field Layer 4 already uses -- no filename-similarity guessing, no
    # invented paths for an unregistered entity.
    for entity in known_entity_names():
        path = canonical_path_for_entity(entity)
        assert path is not None, f"registered entity {entity!r} must resolve to a canonical path"
    assert canonical_path_for_entity("NotARealEntity") is None
    print("PASS: canonical_path_for_entity resolves every registered entity, invents nothing for unknown ones")


if __name__ == "__main__":
    setup()
    test_obvious_usageos()
    test_obvious_researchos()
    test_infrastructure_n8n()
    test_shared_infrastructure_multi_entity()
    test_ambiguous_two_entities_similar_strength()
    test_unknown_generic_file()
    test_name_collision_account_nickname_suppressed()
    test_historical_file_still_resolves()
    test_duplicate_event_same_result_no_reprocessing()
    test_rename_entity_remains_stable()
    test_scene_map_distinct_from_studioos_project()
    test_clipos_logs_root_level_file()
    test_no_llm_dependency_for_routine_cases()
    test_canonical_path_for_entity_known_and_unknown()
    print(f"\nALL LAYER 4 TESTS PASSED — registry covers {len(known_entity_names())} entities")
