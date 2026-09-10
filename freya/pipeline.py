"""
FREYA — Pipeline orchestrator (partial: wires Layers 1-3 together)

This is the hand-off point. Layers 4+ (entity resolution, knowledge
extraction, relationship engine, canonical updater, enforcer integration,
verification, Claude escalation) plug into `handle_file()` below in that
order — each one is a separate, independently testable module, per the
spec's build order. Not implemented yet in this pass; each stage currently
just records its intended state transition so the ledger stays honest
about what has and hasn't actually happened.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .classifier import classify, scan_for_secrets
from .entity_resolver import resolve_entity
from .knowledge_extractor import extract_knowledge, EXTRACTOR_VERSION
from .state_store import FreyaStateStore

log = logging.getLogger("freya.pipeline")

# Content is only read for classes where it's cheap and meaningful to do so.
# Never read+scan huge/binary files just for entity resolution.
_READABLE_CATEGORIES = {"source_code", "documentation", "project_specification", "configuration", "unknown"}
_MAX_CONTENT_BYTES = 500_000

# Optional hook: real Layer 6+/enforcer wiring can set this to a callable
# (entity, predicate) -> known canonical object value, so Layer 5 can flag
# CONFLICT against live vault truth. Left None by default — Layer 5 must
# fully function with zero live vault connection (see knowledge_extractor
# module docstring for why none is wired in this build pass).
CANONICAL_LOOKUP = None


def _read_content_if_safe(abspath: Path, category: str) -> str | None:
    if category not in _READABLE_CATEGORIES:
        return None
    try:
        if abspath.stat().st_size > _MAX_CONTENT_BYTES:
            return None
        return abspath.read_text(errors="ignore")
    except OSError:
        return None


def _run_extraction(rel: str, entities: list[str], category: str, content: Optional[str],
                     content_hash: Optional[str], store: FreyaStateStore) -> None:
    """
    Layer 5 hook. Runs extraction for one entity, or fans out and merges
    for a MULTI_ENTITY file. Skips re-extraction when this exact content
    was already fully extracted (idempotency, mirrors Layer 1-4 pattern).
    Leaves status at NEEDS_REVIEW regardless of outcome — Layer 5 makes no
    canonical decision; that's Layer 6/7's job. escalated=True on the
    record signals a conflict is waiting for review.
    """
    existing = store.get(rel)
    if (existing.extraction_status == "COMPLETE"
            and existing.extracted_content_hash == content_hash
            and content_hash):
        store.audit(rel, existing.entity, "extraction_skipped_unchanged",
                    {"content_hash": content_hash})
        store.set_status(rel, "NEEDS_REVIEW",
                          escalated=bool(existing.escalation_packet),
                          escalation_reason=existing.escalation_reason or "awaiting Layer 6 (relationship engine)")
        return

    merged_facts: list[dict] = []
    merged_relationships: list[dict] = []
    merged_conflicts: list[dict] = []
    merged_unknowns: list[dict] = []
    escalation_packet = None
    overall_status = "COMPLETE"
    confidences = []

    for entity in entities:
        known_fps = store.known_fingerprints_for_entity(entity)
        result = extract_knowledge(
            entity, rel, content_hash or "", content, category,
            canonical_lookup=CANONICAL_LOOKUP, known_fingerprints=known_fps,
        )
        new_fps = [f["fp"] for f in result.facts]
        if new_fps:
            store.record_fingerprints(entity, new_fps, rel)

        merged_facts.extend(result.facts)
        merged_relationships.extend(result.relationships)
        merged_conflicts.extend(result.conflicts)
        merged_unknowns.extend(result.unknowns)
        confidences.append(result.confidence)
        if result.status == "NEEDS_REVIEW":
            overall_status = "NEEDS_REVIEW"
            escalation_packet = escalation_packet or result.escalation

    conf_rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    overall_confidence = max(confidences, key=lambda c: conf_rank[c]) if confidences else "LOW"

    extracted_knowledge = {
        "facts": merged_facts,
        "unknowns": merged_unknowns,
        "entities": entities,
        "extractor_version": EXTRACTOR_VERSION,
    }

    store.set_extraction_result(
        rel,
        extraction_status=overall_status,
        extracted_knowledge=extracted_knowledge,
        relationships=merged_relationships,
        conflicts=merged_conflicts,
        escalation_packet=escalation_packet,
        confidence=overall_confidence,
        extracted_content_hash=content_hash or "",
        extraction_version=EXTRACTOR_VERSION,
    )
    store.set_status(
        rel, "NEEDS_REVIEW",
        escalated=bool(escalation_packet),
        escalation_reason=(escalation_packet.get("reason") if escalation_packet
                            else "awaiting Layer 6 (relationship engine)"),
    )


def handle_file(abspath: Path, rel: str, event_kind: str, store: FreyaStateStore) -> None:
    if event_kind == "deleted":
        # state_store.mark_unavailable already called by the watcher.
        return

    cls = classify(abspath)
    store.set_status(rel, "CLASSIFIED", classification=cls.category)
    store.audit(rel, None, "classified", {"category": cls.category, "reason": cls.reason})

    if cls.ignorable:
        store.set_status(rel, "IGNORED")
        return

    if cls.category == "secret_bearing":
        store.set_status(rel, "IGNORED", last_error="secret-bearing filename; never ingested")
        store.audit(rel, None, "secret_file_skipped", {})
        return

    secret_hits = scan_for_secrets(abspath)
    if secret_hits:
        store.audit(rel, None, "secrets_detected_inline", {"count": len(secret_hits), "lines": [h[0] for h in secret_hits]})
        # Not an auto-ignore: a code file CAN contain a hardcoded key and still
        # be meaningful source material. Downstream extraction (Layer 5, not
        # yet wired) is responsible for redacting before anything is written
        # to canonical notes. We just flag it here for the audit trail.

    # --- Layer 4: entity resolution -------------------------------------
    content = _read_content_if_safe(abspath, cls.category)
    if secret_hits:
        # Never let secret-shaped substrings ride along into entity-resolution
        # evidence strings (which land in the audit log).
        from .classifier import redact_for_extraction
        content = redact_for_extraction(content) if content else content

    result = resolve_entity(rel, content=content)

    if result.status == "RESOLVED":
        store.set_entity_resolution(
            rel, entity_status="RESOLVED", entity=result.entity, confidence=result.confidence,
            candidates=result.candidates, evidence=result.evidence,
        )
        content_hash = store.get(rel).content_hash
        _run_extraction(rel, [result.entity], cls.category, content, content_hash, store)
        return

    if result.status == "MULTI_ENTITY":
        store.set_entity_resolution(
            rel, entity_status="MULTI_ENTITY", candidates=result.candidates,
            evidence=result.evidence, multi_entities=result.multi_entities,
        )
        content_hash = store.get(rel).content_hash
        _run_extraction(rel, result.multi_entities, cls.category, content, content_hash, store)
        return

    if result.status == "AMBIGUOUS":
        store.set_entity_resolution(
            rel, entity_status="AMBIGUOUS", candidates=result.candidates, evidence=result.evidence,
        )
        store.set_status(rel, "NEEDS_REVIEW",
                          escalated=True,
                          escalation_reason="ambiguous entity resolution; candidate for Claude escalation packet")
        return

    # UNKNOWN
    store.set_entity_resolution(rel, entity_status="UNKNOWN", candidates={}, evidence=result.evidence)
    store.set_status(rel, "NEEDS_REVIEW",
                      escalated=False,
                      escalation_reason="no entity signal found; not necessarily an error — may be genuinely unowned")
