"""
tests/test_mcp_adapter.py -- Regression tests for ObsidianMCPAdapter.read()
tool-level error handling (the isError / "File not found" bug found during
Phase 8.1 real-DRY_RUN validation, transaction tx-a820baf0f63c444a).

Two layers of coverage:
  1. Unit tests against ObsidianMCPAdapter.read() directly, stubbing only
     the low-level _tool() call (no network) so the exact JSON-RPC
     tool-result shape the real server returns can be reproduced
     precisely, including the isError flag.
  2. One integration-shaped test that runs a real CREATE proposal through
     the real enforcer + real CanonicalExecutor (using a fake-but-real-
     shaped adapter that reproduces the exact buggy response shape at the
     _tool level) to confirm the fix actually reaches pre_existing in the
     real enforcer's recovery manifest -- not just the adapter's return
     value in isolation.

Never touches the real Obsidian vault/MCP server or a live network
connection: _tool() is monkeypatched in every test here. Follows the
existing test conventions (PASS/FAIL counters via check()).
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freya.state_store import FreyaStateStore
from freya.canonical_executor import CanonicalExecutor, load_real_enforcer, MODE_DRY_RUN
from freya.mcp_adapter import ObsidianMCPAdapter, McpUnavailable

TEST_ENTITY = "_ENFORCER_TEST"
TEST_PATH = "Notion/_ENFORCER_TEST.md"

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name}")


def make_adapter(tool_response):
    """A real ObsidianMCPAdapter with only the low-level _tool() call
    stubbed -- read() itself runs unmodified, so these tests exercise the
    actual fixed code path, not a re-implementation of it."""
    adapter = ObsidianMCPAdapter(api_key="unused-in-tests")
    adapter._tool = lambda name, arguments: tool_response
    return adapter


# 1. Genuine "not found" tool-level error becomes None -----------------------

def test_read_missing_file_returns_none():
    adapter = make_adapter({
        "isError": True,
        "content": [{"type": "text", "text": "File not found: Notion/_ENFORCER_TEST.md"}],
    })
    check("read() on a genuinely missing file returns None",
          adapter.read(TEST_PATH) is None)


# 2. The isError response body is never handed back as file content ---------

def test_read_isError_not_returned_as_content():
    adapter = make_adapter({
        "isError": True,
        "content": [{"type": "text", "text": "File not found: Notion/_ENFORCER_TEST.md"}],
    })
    result = adapter.read(TEST_PATH)
    check("error text is not returned as a string",
          not isinstance(result, str))
    check("error text specifically is not smuggled through as content",
          result != "File not found: Notion/_ENFORCER_TEST.md")


# 3. An unexpected (non-"not found") tool-level error raises, not silent ----

def test_read_unexpected_error_raises():
    adapter = make_adapter({
        "isError": True,
        "content": [{"type": "text", "text": "Permission denied: Notion/_ENFORCER_TEST.md"}],
    })
    try:
        adapter.read(TEST_PATH)
        check("unexpected read error raises McpUnavailable", False)
    except McpUnavailable as e:
        check("unexpected read error raises McpUnavailable", True)
        check("raised error preserves the underlying message",
              "Permission denied" in str(e))


# 4. Successful reads are completely unaffected by the fix -------------------

def test_read_existing_file_still_works():
    body = "---\nnotion-id: \n---\n# PostgreSQL\n\nShared infrastructure.\n"
    adapter = make_adapter({"content": [{"type": "text", "text": body}]})
    check("successful read still returns real content unchanged",
          adapter.read("Notion/Infrastructure/PostgreSQL.md") == body)


def test_read_existing_file_plain_string_content_still_works():
    """Some tool responses may return content as a bare string rather than
    a content-block list; the fix must not assume list-shaped content is
    the only success shape."""
    adapter = make_adapter({"content": "plain string body"})
    check("plain-string content success path still works",
          adapter.read(TEST_PATH) == "plain string body")


# 5. End-to-end: CREATE against a nonexistent target -> pre_existing=False --
#    Runs through the REAL enforcer and REAL CanonicalExecutor, with only
#    the adapter's _tool() stubbed to reproduce the exact buggy server
#    shape -- so this proves the fix reaches the enforcer's own recovery
#    manifest, not just the adapter's isolated return value.

def test_create_nonexistent_target_pre_existing_false_via_real_enforcer():
    db_path = f"/tmp/freya_mcpadapter_test_{uuid.uuid4().hex[:8]}.sqlite3"
    store = FreyaStateStore(db_path)
    enforcer = load_real_enforcer()

    adapter = make_adapter({
        "isError": True,
        "content": [{"type": "text", "text": f"File not found: {TEST_PATH}"}],
    })
    # create() must still work normally for this proposal's own DRY_RUN
    # path (DRY_RUN performs zero writes regardless, but keep the stub
    # honest about the rest of the McpAdapter interface).
    adapter.create = lambda path, content: True
    adapter.ping = lambda: True

    ex = CanonicalExecutor(store, enforcer=enforcer, mcp_adapter=adapter)

    fp = f"fp-mcpadapter-regress-{uuid.uuid4().hex[:12]}"
    proposal = {
        "proposal_id": fp,
        "action": "CREATE",
        "entity": TEST_ENTITY,
        "canonical_path": TEST_PATH,
        "section": None,
        "current_content": None,
        "proposed_content": "# _ENFORCER_TEST\n\nRegression-test sandbox note.\n",
        "reason": "mcp_adapter regression test",
        "evidence": ["OBSERVED"],
        "provenance": {
            "source_file": "test.md", "evidence_classification": "OBSERVED",
            "temporal_state": "OBSERVED_CURRENT", "confidence": "HIGH",
        },
        "temporal_state": "OBSERVED_CURRENT",
        "confidence": "HIGH",
        "risk": "LOW",
        "fingerprint": fp,
        "depends_on": [],
    }
    plan_id = f"plan-mcpadapter-regress-{uuid.uuid4().hex[:8]}"
    store.record_plan({"plan_id": plan_id, "origin": "VALIDATION", "proposals": [proposal]},
                       source_file=TEST_PATH)
    store.record_proposals([proposal], plan_id=plan_id)

    result = ex.execute_proposal(proposal, mode=MODE_DRY_RUN)
    check("DRY_RUN on CREATE against nonexistent target reaches DRY_RUN_VALIDATED",
          result["status"] == "DRY_RUN_VALIDATED")

    tx_id = result["transaction_id"]
    check("transaction id present in result", bool(tx_id))

    manifest_path = Path.home() / ".local" / "share" / "knowledge-system" / "recovery" / tx_id / "manifest.json"
    check("recovery manifest was written", manifest_path.exists())
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        check("manifest pre_existing is False for a genuinely nonexistent target",
              manifest.get("pre_existing") is False)

        prestate_dir = manifest_path.parent / "prestate"
        stray_content_files = list(prestate_dir.glob("*")) if prestate_dir.exists() else []
        check("no fake 'File not found' prestate content file was written",
              not any("File not found" in f.read_text() for f in stray_content_files
                      if f.is_file()))


def run():
    test_read_missing_file_returns_none()
    test_read_isError_not_returned_as_content()
    test_read_unexpected_error_raises()
    test_read_existing_file_still_works()
    test_read_existing_file_plain_string_content_still_works()
    test_create_nonexistent_target_pre_existing_false_via_real_enforcer()

    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)
    print("ALL MCP ADAPTER REGRESSION TESTS PASSED")


if __name__ == "__main__":
    run()
