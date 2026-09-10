# FREYA — Phase 7 build

## Update (Phase 7 — Layer 7: Canonical Change Planner)

Added `freya/canonical_planner.py`, wired into `pipeline.py` right after
Layer 6 (`_run_canonical_planner`, called from `_run_extraction`). Schema
extended additively: two new tables (`plans`, `proposals`) — no changes
to any Layer 1-6 column or table. Verified the full Layer 1-6 suite
(57/57) before writing a line of Layer 7 logic, and again after.

**PLAN ONLY, structurally enforced:** grepped the entire codebase for
every canonical-write-capable call and every `anthropic.`-style call —
zero hits, same discipline as Layers 5/6, now also covering "never call
Claude." `canonical_planner.py` reads existing canonical text only
through an injected `canonical_reader` hook — same optional pattern as
Layer 5's `CANONICAL_LOOKUP` and Layer 6's `CANONICAL_RELATIONSHIP_LOOKUP`.
`pipeline.py`'s `CANONICAL_READER` defaults to `None`, same honestly-
stated production gap as before (the real pipeline is a background daemon
on the Mac, outside any Claude session, with no live reader wired this
phase). **The difference this phase:** with no reader, Layer 7 doesn't
just sit idle — it explicitly `ESCALATE`s with reason
`CANONICAL_STATE_UNVERIFIABLE` rather than silently proposing a possible
duplicate, which is the actual spec requirement ("if provenance cannot be
established: ESCALATE").

**Canonical target selection** goes through a new `canonical_path_for_entity()`
helper added to `entity_resolver.py`, reading the same registry Layer 4
already uses — no filename-similarity guessing, no invented paths. An
entity with no registered target always becomes `ESCALATE` /
`NO_CANONICAL_TARGET`.

**Change types:** all 11 from the spec. `plan_fact` handles Layer 5's
non-relational fact types (routed to `Purpose`/`Decisions`/`Bugs`/`Plans`/
etc. by fact_type); `plan_relationship` handles Layer 6's already-resolved
relationship records (routed to `Relationships`) — deliberately two
separate functions, since Layer 6 already did conflict/supersession
resolution at FREYA's own belief-graph level, and Layer 7's job is only
comparing that resolved belief against actual canonical text, not
re-deriving relationships.

**Evidence priority / never-silently-upgrade rules, enforced as actual
code branches, not just comments:** `AI_SUGGESTION`/`AI_CLAIM` alone →
`ESCALATE`; `LOW` confidence → `ESCALATE`; any temporal status outside
`{OBSERVED_CURRENT, IMPLEMENTED, DECIDED}` → `APPEND`/`MARK_HISTORICAL`
into History/Plans/Unknowns, never framed as current. To make this
actually enforceable, `relationship_engine.py`'s `facts_to_candidates`
was patched to carry `evidence_type` through into the candidate's
evidence list — it wasn't being propagated before, which would have made
this whole rule vacuous for relationship-shaped facts specifically.

**Duplicate/idempotency detection** is a deliberately coarse, honestly-
described containment check (`_target_already_mentioned` /
`_fact_already_present`): does the proposed object/target appear anywhere
in canonical text, normalized (wikilinks stripped, whitespace collapsed,
lowercased)? Documented as approximate (can false-"already exists" on an
unrelated mention, can false-negative on differently-worded phrasing) —
erring toward `NO_CHANGE` over a duplicate `ADD` is the deliberate,
spec-aligned default; Layer 8 re-verifies before any real write regardless.

**Stable fingerprinting:** `(canonical_path, action, entity, section,
normalized_content)` → sha256[:16], same pattern as Layers 5/6.
`proposals` table upserts by fingerprint (`record_proposals`) — running
planning twice on unchanged inputs increments `seen_count` on the same
row rather than duplicating, satisfying the idempotency requirement at
the storage layer, not just at the fingerprint-computation layer.

**No destructive inference:** verified structurally — there is no code
path anywhere in `canonical_planner.py` that emits `REMOVE_RELATIONSHIP`
or any deletion from mere absence; `build_plan([], [], ...)` on empty
input produces zero proposals, tested directly.

Run all five suites: `python3 tests/test_layers_1_3.py && python3 tests/test_layer4_entity_resolution.py && python3 tests/test_layer5_knowledge_extraction.py && python3 tests/test_layer6_relationship_engine.py && python3 tests/test_layer7_canonical_planner.py` — 78/78 passing.

**Real-ecosystem validation** (read-only, all 10 entities the spec names —
UsageOS, ResearchOS, PersonaOS, ClipOS, CompanionOS, StudioOS, ATLAS, n8n,
PostgreSQL, Caddy — using each note's real, complete, unmodified content
as both the extraction source AND the canonical_reader target, i.e. a
self-consistency/idempotency check: does FREYA reading a note's own
content propose re-adding what's already there?):

- **69/69 facts and relationships checked across all 10 entities resolved
  to `NO_CHANGE`.** This is the *correct* result for a self-consistency
  pass, not evidence the planner is inert — confirmed separately by
  feeding it one genuinely new sentence ("UsageOS depends on Redis for
  caching") against the real UsageOS canonical text, which correctly
  produced `ADD_RELATIONSHIP` at `LOW` risk.
- **ResearchOS**, specifically: the current Python/ATLAS implementation
  and the historical n8n relationship (the flagship example named in all
  three phase specs) both resolved to `NO_CHANGE` — meaning the planner
  correctly recognized both are already canonically documented, current
  and historical, without proposing a duplicate `ADD` for either.
- **PersonaOS**: zero proposals reference ATLAS at all — the negation
  fix from the Phase 6 real-validation pass ("It is not currently hosted
  on ATLAS") continues to hold against the real, complete note text.
- **ClipOS**: zero `ADD_RELATIONSHIP` proposals claim a current ATLAS
  hosting relationship — the historical/planned framing in the real note
  is correctly never promoted to current.
- **StudioOS**: zero new relationship proposals — the Scene Map → n8n →
  StudioOS Media pipeline relationship is already fully documented in the
  note's own Architecture/Infrastructure sections.

No canonical writes were made in this phase or at any point in the
validation — confirmed via grep for every canonical-write-capable call
name (zero hits) and via the fact that every real-vault interaction this
session was `get_vault_files` (read-only). No call to Claude was made
from anywhere in `canonical_planner.py` — confirmed via the same grep and
via a dedicated AST-based import-check test. Layer 8 remains solely
responsible for any actual write.

**Known gaps, stated honestly:**
- **No live canonical-relationship wiring**, same pattern and reasoning
  as Layers 5/6 — `CANONICAL_READER` is `None` by default in the
  production pipeline. Real-ecosystem validation this phase used a
  test-only closure over locally-saved real note content, not a live
  connection from the daemon itself; that live wiring is still an open
  item (see Layer 5/6 READMEs for why a raw HTTP client wasn't attempted
  blind).
- **Duplicate detection is containment-based, not semantic.** A
  differently-worded canonical statement of the same fact could produce
  a redundant `APPEND`/`ADD_RELATIONSHIP` rather than `NO_CHANGE`; a
  target name mentioned in an unrelated context could produce a false
  `NO_CHANGE`. Both are documented in `canonical_planner.py` at the
  functions responsible, not hidden.
- **Section mapping is a fixed fact_type → section lookup, not real
  document-structure awareness.** `split_canonical_sections()` exists and
  is tested, but `plan_fact`/`plan_relationship` don't currently use it to
  check "does this section already exist under a different heading" —
  they rely entirely on the containment check on the whole note instead.
  A future pass could use the section split to produce more precise
  `REPLACE_SECTION`/`UPDATE` proposals instead of always `APPEND`.
- **AI_SUGGESTION/AI_CLAIM branches are unit-tested but not exercised by
  real-ecosystem validation** — same honestly-stated gap carried over
  from Layer 5: FREYA's file-based extraction doesn't currently produce
  those evidence types at all (they're reserved for a hypothetical future
  conversation-transcript extraction source).
- **Multi-entity dependency ordering (`depends_on`) is a field on every
  `ChangeProposal`, present and tested for existence, but nothing in this
  phase actually populates it with real cross-proposal dependencies** —
  e.g. "create the canonical note before adding a relationship into it"
  isn't a case Layer 7 can hit yet, since it never proposes `CREATE` (no
  code path emits it — every entity checked this phase already has a
  registered canonical target). That's a genuine gap to close whenever a
  `CREATE` path is actually needed.

## Update (Phase 6 — Layer 6: Relationship Engine)

Added `freya/relationship_engine.py`, wired into `pipeline.py` right after
Layer 5 extraction succeeds (`_run_relationship_engine`, called from
`_run_extraction`). Schema extended additively: two new tables
(`relationships`, `relationship_conflicts`) — no changes to any Layer 1-5
column or table. Verified the full Layer 1-5 suite (38/38) unmodified
before writing a line of Layer 6 logic, and again after.

**Relationship model:** exactly the spec's field list — source, type,
target, temporal_status, confidence, evidence, source_files, discovered_at,
fingerprint — plus `status` (ACTIVE/SUPERSEDED) and `confirm_count` for
multi-source confirmation. Fingerprint is deliberately `(source, type,
normalized_target)` **without** temporal_status baked in — that's what
makes "second source confirms the same edge" collapse to one row instead
of a duplicate, and what makes supersession a state transition on an
existing row rather than a special merge case.

**Relationship types:** the spec's 14, no more. Only 8 of Layer 5's fact
predicates ever become a graph edge (`RUNS_ON`, `HOSTED_ON`, `USES`,
`DEPENDS_ON`, `IMPLEMENTED_AS`, `FEEDS_INTO`→`FEEDS`,
`PREVIOUSLY_USED`/`SUPERSEDED_BY`→`HISTORICAL`); everything else
(`PURPOSE`, `DECIDED_TO_USE`, `HAD_BUG`, `PLANNED`, `CONSTRAINED_BY`, …)
is filtered out at the door — those objects are prose fragments, not
named things a graph edge should point at.

**Granularity filter:** `USES`/`DEPENDS_ON` targets must match a known
project entity or a recognized piece of shared infra; this is the direct
mechanism behind the "`import requests` doesn't become an ecosystem
relationship" requirement — Layer 5 already curates which frameworks
even get extracted from source code, and Layer 6 double-checks doc-derived
mentions the same way.

**Conflict vs. supersession — the actual decision rule, not just the
label:** a new current-flavored candidate against an existing ACTIVE
current-flavored record for the same `(source, type)` but a *different*
target → `RELATIONSHIP_CONFLICT`, no auto-pick. A new *historical*-flavored
candidate whose target matches an existing ACTIVE current-flavored record
(any type, same source) → that old record gets flipped to `SUPERSEDED`
in place and kept, never deleted, and the new historical record is added
alongside it.

**Deduplication / confirmation:** exact-fingerprint match → `CONFIRM`,
not a new row — evidence and source_files merge onto the existing record,
confidence takes the stronger of the two, `confirm_count` increments.

**A real bug caught by testing, not inspection:** the first pass fed
Layer 6 the *already-fact-deduped* output of Layer 5 (the same list used
for the audit-facing `extracted_knowledge` ledger). Since Layer 5 silently
drops a fact it's already seen from the same entity, a second file
confirming the same relationship never reached Layer 6 at all — `CONFIRM`
could never fire through the real pipeline, only in a hand-built unit
test. Layer 5's dedup answers "should this appear again in the fact
ledger?"; Layer 6 needs to *see* every repeat sighting to know a second
source confirmed something. Fixed by having `_run_extraction` compute a
second, undeduped fact list specifically for Layer 6 (cheap — deterministic
regex, no I/O) without touching Layer 5's own dedup contract or its tests.

Run all four suites: `python3 tests/test_layers_1_3.py && python3 tests/test_layer4_entity_resolution.py && python3 tests/test_layer5_knowledge_extraction.py && python3 tests/test_layer6_relationship_engine.py` — 59/59 passing.

**Real-ecosystem validation** (read-only, same five live canonical notes as
the Phase 5 pass — UsageOS, ResearchOS, PersonaOS, Infrastructure/PostgreSQL,
Infrastructure/n8n — confirmed no write-capable call appears anywhere in
the codebase) surfaced two more real, non-synthetic bugs:
- **The flagship ResearchOS/n8n supersession example didn't fire at all**,
  because the live note's actual phrasing ("ADR-001 originally specified
  n8n-based hosting/orchestration...this is now historical/superseded by
  the current Python-script-on-ATLAS implementation") doesn't match Layer
  5's narrower "previously used X" pattern. This is exactly the case both
  phase specs name as the flagship example, and it was silently missed —
  a false negative, safer than a false positive, but a real gap. Fixed by
  generalizing the historical-mention pattern to also catch ADR-style
  "originally specified/used X[-based]... (historical|superseded)"
  phrasing, and added a regression test using the real sentence.
- **The granularity filter's exact-match check rejected an
  unambiguously-known target** ("PostgreSQL for storage" ≠ "postgresql"
  as an exact string, even though the target is obviously PostgreSQL).
  Fixed to check containment of a known term as its own word, not
  equality of the whole string; added a regression test.

Both fixes verified against the real content again after fixing, not just
asserted — `ResearchOS → HISTORICAL → n8n` and `UsageOS → DEPENDS_ON →
PostgreSQL for storage` both now surface correctly.

No canonical writes were made in this phase — confirmed via grep for
every canonical-write-capable call name (zero hits) plus a dedicated test.

**Known gaps, stated honestly:**
- **No live canonical-relationship wiring.** `CANONICAL_RELATIONSHIP_LOOKUP`
  in `pipeline.py` defaults to `None`, same pattern and same reason as
  Layer 5's `CANONICAL_LOOKUP` — "existing graph awareness" (recognizing
  a relationship that's already a real wikilink in the vault, per spec)
  is architecturally supported (the hook exists, `evaluate_candidate`
  would just need the extra signal) but not fed real vault state yet.
  That's a Layer 7 prerequisite, same reasoning as before.
- **Orphan detection (`state_store.orphan_entities`) is implemented but
  not wired into the per-file pipeline**, by design — it's a whole-graph
  scan, and the spec is explicit that Layer 6 shouldn't recompute the
  entire graph on every filesystem event. It's available to call from a
  future periodic-reconciliation pass (Layer 12), not from here.
  Genuinely untested against the real ecosystem as a result — no orphan
  report has actually been run against the live vault's full graph.
- **Sentence-level heuristics inherited from Layer 5 still apply** —
  semicolon-joined clauses aren't sentence boundaries, there's no
  grammatical-subject detection, and the two fixes above closed two real
  gaps but almost certainly not all of them; the ADR-style pattern in
  particular is still one specific phrasing family, not general NLP.
- **Multi-entity fan-out relationships aren't cross-validated against each
  other** — e.g. a shared docker-compose file producing both `ATLAS →
  HOSTS → UsageOS` and `ATLAS → HOSTS → ResearchOS` are each evaluated
  independently; nothing currently checks whether the *set* of relationships
  discovered from one file is internally consistent beyond each edge's own
  conflict/supersession check.

## Update (Phase 5 — Layer 5: Knowledge Extraction)

Added `freya/knowledge_extractor.py`, wired into `pipeline.py` right after
entity resolution (both the RESOLVED and MULTI_ENTITY branches — multi-entity
files fan out to one extraction pass per entity, then merge). Schema
extended additively: 5 new nullable columns on `files` (`extraction_status`,
`extraction_version`, `extracted_content_hash`, `conflicts`,
`escalation_packet`) plus one new table (`fact_fingerprints`, for
cross-source fact deduplication). Verified the full existing Layer 1-4
suite (20/20) still passes unmodified after the migration, before writing
a line of Layer 5 logic — and again after, since the first pass introduced
a real regression (see below).

**Extraction model:** deterministic only — AST parsing for Python imports
(never executes source; malformed files parse to zero facts, not a
guess), regex for docker-compose service names and JS/TS imports, and
sentence-level heuristics for docs/specs. 14 fact types, 7 temporal
statuses, 7 evidence types, 3 confidence levels, all exactly as specified.
Predicate-specific temporal defaults (e.g. `RUNS_ON` → `OBSERVED_CURRENT`,
`IMPLEMENTED_AS` → `IMPLEMENTED`) are overridden by explicit hedge-word
cues in the sentence (`previously`, `plan to`, `was selected`, `unclear`,
etc.) — temporal states are never collapsed into one bucket.

**Regression caught by testing, not by inspection:** the first pass had
Layer 5 writing extraction confidence into the same `confidence` column
Layer 4 uses for entity-resolution confidence, silently clobbering it.
The Layer 4 suite failed on rerun (`test_obvious_researchos`), which is
exactly why the "rerun the full existing suite before declaring a phase
done" discipline exists. Fixed with a dedicated `extraction_confidence`
column.

**Real-ecosystem validation** (read-only, via `mcp-tools-istefox:get_vault_files`
against the live vault — confirmed no write-capable call appears anywhere
in the codebase): ran extraction against the actual, unmodified content of
five live canonical notes (UsageOS, ResearchOS, PersonaOS,
Infrastructure/PostgreSQL, Infrastructure/n8n). This caught two real bugs
that synthetic test cases hadn't surfaced:
- **Negation blindness**: PersonaOS's note says "It is **not** currently
  hosted on ATLAS." The first-pass extractor ignored the negation and
  emitted `PersonaOS → HOSTED_ON → ATLAS [OBSERVED_CURRENT]` — a
  confidently wrong fact from a sentence that explicitly denies it. Fixed
  with a negation-window guard before RUNS_ON/HOSTED_ON matching, and
  added a regression test using this exact sentence.
- **Bullet lists glommed into one sentence**: the sentence splitter
  flattened newlines to spaces, so a bulleted list under a heading became
  one run-on "sentence" and polluted fact objects with unrelated trailing
  bullet text. Fixed by treating newlines as sentence boundaries; added a
  regression test.

Both fixes are covered by new tests, not just patched and moved on from.

Run all three suites: `python3 tests/test_layers_1_3.py && python3 tests/test_layer4_entity_resolution.py && python3 tests/test_layer5_knowledge_extraction.py` — 38/38 passing.

No canonical writes were made in this phase (confirmed — every MCP call
this session was `get_vault_overview` or `get_vault_files`, both reads;
grepped the full codebase for any canonical-write call and found none).

**Known gaps, stated honestly (not papered over):**
- **No canonical-context wiring by default.** `detect_conflicts()` and the
  escalation-packet builder are fully implemented and tested against a
  synthetic canonical lookup, but `pipeline.py`'s `CANONICAL_LOOKUP` hook
  is `None` by default — there's no live code path from the real vault
  into Layer 5's conflict check yet. That's a deliberate Layer 6/7
  prerequisite, not an oversight: building it against synthetic data
  first, the same way Layer 4 built its registry against the real vault
  listing only once one was reachable, seemed like the more honest order.
- **No subject/grammatical-role detection.** Every extracted fact's
  `subject` is hard-coded to the file's resolved entity, regardless of the
  sentence's actual grammatical subject. Real-ecosystem validation caught
  this concretely: PostgreSQL's own note says "**UsageOS** depends on it
  for storage," but because the file resolves to the `Infrastructure/PostgreSQL`
  entity, the extractor emitted `Infrastructure/PostgreSQL → DEPENDS_ON →
  it for storage` — backwards, and with an unresolved pronoun. A real fix
  needs a lightweight subject parser, not a bigger regex.
- **Semicolon-joined clauses aren't sentence boundaries.** Only `. ! ?`
  and newlines split sentences; a clause like "...present in every ATLAS
  infrastructure inventory reviewed, alongside n8n" trailing after a
  semicolon gets pulled into the preceding fact's object as noise.
- **Sentence splitting is heuristic, not NLP-grade**, and will still
  occasionally over- or under-split on dense prose (nested clauses,
  citation-style brackets like `[OBSERVED, HIGH]`).
- Per spec, Layer 5 makes no canonical decision even when it detects a
  conflict — it always produces an escalation-ready packet and leaves the
  file at `NEEDS_REVIEW`. There is no Layer 6 yet to consume that packet.

## Original Phase 4B update (Layer 4: Entity Resolution)

## Update (Phase 4B — Layer 4: Entity Resolution)

Added `freya/entity_resolver.py`, wired into `pipeline.py` right after
classification. Schema extended additively in `state_store.py` (4 new
nullable columns: `entity_status`, `entity_candidates`, `entity_evidence`,
`multi_entities`) — verified the existing Layer 1-3 test suite still
passes unmodified (7/7) after the migration, before writing a line of
Layer 4 logic.

**Real-ecosystem validation** (read-only, via `mcp-tools-istefox:list_vault_files`
against the live vault — no files modified): all 17 real canonical vault
paths resolve correctly, including two edge cases that a naive implementation
gets wrong and that a first draft of this resolver actually did get wrong
until validation caught it:
- `Notion/ATLAS.md`, `Notion/Infrastructure/n8n.md`, etc. — top-level
  landing notes where the entity name is the filename stem, not a
  directory. First draft's path regexes only matched `name/` or exact
  end-of-string, so `ATLAS.md` resolved UNKNOWN. Fixed and reverified.
- `Notion/ClipOS Logs.md` and `Notion/StudioOS - Chapter 1 Scene Map/*` —
  space-separated filenames outside their entity's own folder.

**Known gap, stated honestly:** I could not inspect the real `enforcer.py`
in this pass — it's on local disk outside every tool available in this
session (no filesystem/MCP access beyond the Obsidian vault scope). The
`ENTITY_REGISTRY` in `entity_resolver.py` uses the same entity name
strings as your established canonical model, built from the live vault
listing, but reconciling it 1:1 against the enforcer's actual
`_ENTITY_SCOPE_REGISTRY` keys is a Layer 8 prerequisite that still needs
doing against the real file.

Run both suites: `python3 tests/test_layers_1_3.py && python3 tests/test_layer4_entity_resolution.py` — 20/20 passing.

No canonical writes were made in this phase (confirmed — every MCP call this session was `list_vault_files`, a read).

---

# Original Phase 4 build, part 1 (Layers 1–3)

## What's actually implemented and tested here

| Layer | Module | Status |
|---|---|---|
| 1. Watcher | `freya/watcher.py` | **Built + tested.** watchdog-based, debounced (1.5s), event-driven with a `reconcile()` safety-net pass. |
| 2. Persistent state/index | `freya/state_store.py` | **Built + tested.** SQLite ledger, idempotent by content hash, full audit log with `why(path)`. |
| 3. Classifier | `freya/classifier.py` | **Built + tested.** Rule-based category detection, secret-pattern scanner (detects, never leaks matched values), `.obsidian`/`Claude Data` zone flags. |
| Orchestration stub | `freya/pipeline.py` | Wires 1-3 together. Everything past classification is intentionally a `NEEDS_REVIEW` placeholder — see below. |

Run `python3 tests/test_layers_1_3.py` — 7/7 pass, verified in this session, not just asserted.

## What is NOT built yet (by design — see spec's "build one layer at a time")

- **Layer 4 — Entity resolver.** Needs to query your live vault (via `mcp-tools-istefox`) for existing canonical entities before it can decide "does this file belong to UsageOS, or is it something new?" This has to be built against your real vault content, not synthetic data.
- **Layer 5 — Knowledge extractor**
- **Layer 6 — Relationship engine**
- **Layer 7 — Canonical updater**
- **Layer 8 — Enforcer integration** (`enforcer.py` transaction lifecycle)
- **Layer 9 — Verification**
- **Layer 10 — Claude escalation mechanism** (the context-packet format from the spec)
- **Layer 11 — Health/diagnostics endpoint** (state_store.health() exists as the data source; no HTTP/CLI surface yet)
- **Layer 12 — Reconciliation loop** — the *mechanism* exists in `watcher.reconcile()` and is wired into `main.py`'s 15-minute loop; what's missing is layers 4-9 for reconcile to actually *do* something useful beyond re-classifying.

Every file that reaches classification right now stops at `NEEDS_REVIEW` with an honest audit entry saying why (`"entity resolution / extraction not yet implemented"`). Nothing is silently dropped, nothing fakes completion.

## Deployment decision (made, not guessed)

**Runs on the Mac, as a `launchd` service.** Not ATLAS, not hybrid. Two hard constraints forced this:
1. `/Users/yoboxo/Developer/Developer` is a Mac-local path.
2. The Obsidian MCP connector is bound to `127.0.0.1:27200` and only exists while Obsidian.app is running on that same machine.

Running the watcher anywhere else would require tunneling both of those back to wherever it ran, for zero benefit.

## Install (once you're ready to run it on your Mac)

```bash
mkdir -p ~/.local/share/knowledge-system/freya
# copy freya/, main.py, requirements.txt into that directory
cd ~/.local/share/knowledge-system/freya
pip3 install -r requirements.txt --break-system-packages

# test without installing the service first:
python3 main.py --reconcile-once

# then install as a real service:
cp deploy/com.yoboxo.freya.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.yoboxo.freya.plist

# check it's alive:
launchctl list | grep freya
tail -f ~/.local/share/knowledge-system/freya/freya.log
```

To stop it: `launchctl unload ~/Library/LaunchAgents/com.yoboxo.freya.plist`

## Next session

Build Layer 4 (entity resolver) against your real vault via `mcp-tools-istefox`, since I already confirmed that connector is live and current (108 notes, `Notion/**` = 44 files including `ATLAS.md`, `n8n.md`, `Caddy.md`, `PostgreSQL.md`, `ResearchOS.md`, `UsageOS.md`, `StudioOS.md`, `CompanionOS.md`, `PersonaOS.md` — all present as of this session, which supersedes the "no canonical entity notes yet" note for n8n/Caddy/PostgreSQL in earlier memory). Then Layer 8 (enforcer integration) needs the actual `enforcer.py` inspected for its current `_ENTITY_SCOPE_REGISTRY` shape before the canonical updater can safely target it.
