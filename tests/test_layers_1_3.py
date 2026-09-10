import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freya.state_store import FreyaStateStore, hash_file
from freya.classifier import classify, scan_for_secrets, is_protected_path
from freya.watcher import FreyaWatcher
from freya.pipeline import handle_file

TEST_ROOT = Path("/tmp/freya_test_vault")
DB_PATH = "/tmp/freya_test_state.sqlite3"


def setup():
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    Path(DB_PATH).unlink(missing_ok=True)
    (TEST_ROOT / "Notion").mkdir(parents=True)
    (TEST_ROOT / "Claude Data").mkdir(parents=True)
    (TEST_ROOT / ".obsidian").mkdir(parents=True)
    (TEST_ROOT / "SomeProject" / "node_modules" / "pkg").mkdir(parents=True)


def test_classifier_rules():
    assert classify(Path("app.py")).category == "source_code"
    assert classify(Path("README.md")).category == "documentation"
    assert classify(Path("ADR-004-runtime.md")).category == "project_specification"
    assert classify(Path("node_modules/pkg/index.js")).ignorable is True
    assert classify(Path(".env")).category == "secret_bearing"
    assert classify(Path("service.dylib")).ignorable is True
    print("PASS: classifier rules")


def test_secret_scan_never_leaks_value():
    f = TEST_ROOT / "config_snippet.py"
    f.write_text('API_KEY = "sk-testkeyvalue1234567890"\n')
    hits = scan_for_secrets(f)
    assert len(hits) == 1
    line_no, label = hits[0]
    assert line_no == 1
    assert "sk-testkeyvalue" not in label  # only the type leaks, never the value
    print("PASS: secret scan detects without leaking value")


def test_protected_path_never_touched():
    assert is_protected_path(".obsidian/plugins/foo.json") is True
    assert is_protected_path("Notion/ATLAS.md") is False
    print("PASS: .obsidian correctly flagged protected")


def test_idempotency_same_content_twice():
    store = FreyaStateStore(DB_PATH)
    rel = "Notion/Test.md"
    f = TEST_ROOT / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("hello world")

    h = hash_file(f)
    rec1, changed1 = store.touch_seen(rel, h)
    store.set_status(rel, "VERIFIED")  # simulate full pipeline completion

    rec2, changed2 = store.touch_seen(rel, h)  # same bytes, re-encountered
    assert changed1 is True
    assert changed2 is False, "identical content must be a no-op (idempotency)"
    print("PASS: idempotency — duplicate content does not re-trigger processing")


def test_content_change_does_retrigger():
    store = FreyaStateStore(DB_PATH)
    rel = "Notion/Test2.md"
    f = TEST_ROOT / rel
    f.write_text("version 1")
    h1 = hash_file(f)
    store.touch_seen(rel, h1)
    store.set_status(rel, "VERIFIED")

    f.write_text("version 2 - actually different content")
    h2 = hash_file(f)
    rec, changed = store.touch_seen(rel, h2)
    assert changed is True, "changed content must retrigger processing"
    assert rec.status == "NEW"
    print("PASS: content change correctly retriggers pipeline")


def test_deletion_preserves_canonical_knowledge():
    store = FreyaStateStore(DB_PATH)
    rel = "Notion/WillBeDeleted.md"
    f = TEST_ROOT / rel
    f.write_text("some canonical fact")
    store.touch_seen(rel, hash_file(f))
    store.set_status(rel, "VERIFIED")

    store.mark_unavailable(rel)
    rec = store.get(rel)
    assert rec.status == "UNAVAILABLE"
    assert rec.content_hash is not None  # last known content_hash preserved, not wiped
    print("PASS: deletion marks UNAVAILABLE without destroying record/knowledge")


def test_watcher_end_to_end_with_reconciliation():
    store = FreyaStateStore(DB_PATH)
    events = []

    def on_ready(abspath, rel, kind):
        events.append((rel, kind))
        handle_file(abspath, rel, kind, store)

    watcher = FreyaWatcher(TEST_ROOT, store, on_ready=on_ready,
                            ignore_check=lambda rel: "node_modules" in rel)

    # Simulate what would happen on a real watch by driving reconcile()
    # directly (deterministic for a test; live fs events are covered by
    # watchdog's own test suite).
    (TEST_ROOT / "Notion" / "NewEntity.md").write_text("# New Entity\nRuns on ATLAS.")
    watcher.reconcile()

    rec = store.get("Notion/NewEntity.md")
    assert rec is not None
    assert rec.status in ("NEEDS_REVIEW", "IGNORED", "CLASSIFIED")
    node_modules_rec = store.get("SomeProject/node_modules/pkg/.gitkeep") if (TEST_ROOT / "SomeProject/node_modules/pkg/.gitkeep").exists() else None

    audit = store.why("Notion/NewEntity.md")
    assert any(a["event"] == "classified" for a in audit)
    print("PASS: watcher + reconciliation + pipeline handoff, with audit trail")


if __name__ == "__main__":
    setup()
    test_classifier_rules()
    test_secret_scan_never_leaks_value()
    test_protected_path_never_touched()
    test_idempotency_same_content_twice()
    test_content_change_does_retrigger()
    test_deletion_preserves_canonical_knowledge()
    test_watcher_end_to_end_with_reconciliation()
    print("\nALL LAYER 1-3 TESTS PASSED")
