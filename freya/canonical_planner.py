"""
FREYA — Canonical Change Planner (Layer 7)

PLAN ONLY. Nothing in this module — or anywhere reachable from it — writes
to Notion/**, Claude Data/**, or .obsidian/**, calls Claude, or makes a
network call. Existing canonical note content is read only through an
injected `canonical_reader` callable: same optional-hook pattern Layer
5/6 already established (CANONICAL_LOOKUP / CANONICAL_RELATIONSHIP_LOOKUP),
for the same reason — this module must produce a correct, safe plan even
with zero live vault connection, and the real production pipeline (a
background daemon on the user's Mac, outside any Claude session) has no
live reader wired in by default. See module-level note further down for
exactly why a live wiring wasn't attempted this phase.

Answers "what SHOULD change in Notion," not "what COULD we write." A
proposal only exists when Layer 5/6 evidence justifies it AND comparing
against existing canonical text (when readable) shows it isn't already
there. Absence of evidence is never evidence of deletion — see BLOCKED
handling below.

No LLM call anywhere in this module, same as Layers 5 and 6.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from .classifier import redact_for_extraction
from .entity_resolver import canonical_path_for_entity

CHANGE_TYPES = {
    "CREATE", "UPDATE", "APPEND", "REPLACE_SECTION", "ADD_RELATIONSHIP",
    "REMOVE_RELATIONSHIP", "MARK_SUPERSEDED", "MARK_HISTORICAL",
    "NO_CHANGE", "ESCALATE", "BLOCKED",
}

RISK_LEVELS = {"LOW", "MEDIUM", "HIGH"}

CANONICAL_SECTIONS = [
    "Purpose", "Current Status", "Architecture", "Implementation", "Runtime",
    "Infrastructure", "Dependencies", "APIs", "Workflows", "Decisions",
    "Constraints", "Bugs", "Fixes", "Plans", "Relationships", "History",
    "Unknowns", "Provenance",
]

# Layer 5 fact_type -> preferred canonical section. Relationship-shaped
# facts (handled by plan_relationship, not plan_fact) aren't here.
_FACT_TYPE_TO_SECTION = {
    "PURPOSE": "Purpose", "ARCHITECTURE": "Architecture", "IMPLEMENTATION": "Implementation",
    "RUNTIME": "Runtime", "DEPENDENCY": "Dependencies", "INFRASTRUCTURE": "Infrastructure",
    "API": "APIs", "WORKFLOW": "Workflows", "DECISION": "Decisions", "CONSTRAINT": "Constraints",
    "BUG": "Bugs", "FIX": "Fixes", "PLAN": "Plans", "UNKNOWN": "Unknowns",
}

# Evidence-priority ladder, highest first — mirrors the spec's list
# exactly. Layer 5's own evidence_type vocabulary maps onto it; FREYA's
# current file-based extraction never actually produces AI_SUGGESTION or
# AI_CLAIM (those are reserved for a hypothetical future conversation-
# transcript extraction source that isn't built), so those branches are
# unit-tested directly rather than observed in real-ecosystem validation
# — documented as a known limitation, not silently assumed impossible.
_EVIDENCE_PRIORITY = {
    "OBSERVED_CURRENT_STATE": 7,
    "IMPLEMENTED_BEHAVIOR": 6,
    "USER_ACCEPTANCE": 5,
    "USER_STATEMENT": 4,
    "HISTORICAL_STATE": 3,
    "AI_CLAIM": 2,
    "AI_SUGGESTION": 1,
}

_NEVER_ALONE_CANONICAL = {"AI_SUGGESTION", "AI_CLAIM"}

_CURRENT_FLAVORS = {"OBSERVED_CURRENT", "IMPLEMENTED", "DECIDED"}
_HISTORICAL_FLAVORS = {"HISTORICAL", "SUPERSEDED"}
_NEVER_CURRENT_TEMPORAL = {"PROPOSED", "UNKNOWN"} | _HISTORICAL_FLAVORS


def _normalize_text(s: str) -> str:
    """Wikilinks/markdown-stripped, whitespace-collapsed, lowercased —
    good enough for containment-based duplicate detection, not a general
    Markdown parser."""
    s = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", s)
    s = re.sub(r"[`*_#>]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def split_canonical_sections(canonical_text: str) -> dict[str, str]:
    """Best-effort split of a canonical note into heading -> body. Doesn't
    attempt to parse nested heading levels beyond top-level grouping —
    enough to find "does a Runtime/Relationships/etc. section already
    exist" without a full Markdown AST."""
    sections: dict[str, str] = {}
    current = "Preamble"
    buf: list[str] = []
    for line in canonical_text.splitlines():
        m = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if m:
            sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    sections[current] = "\n".join(buf).strip()
    return sections


def _fact_already_present(canonical_text: str, subject: str, obj: str) -> bool:
    norm_doc = _normalize_text(canonical_text)
    norm_obj = _normalize_text(obj)
    if not norm_obj:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(norm_obj)}(?!\w)", norm_doc))


def _target_already_mentioned(canonical_text: str, target: str) -> bool:
    """Coarse, deliberately conservative check: does the relationship's
    target appear ANYWHERE in canonical text at all? This can produce a
    false "already exists" (target mentioned in an unrelated context) or
    a false negative (an existing relationship phrased with different
    wording than the target string) — documented honestly, not silently
    assumed precise. Given the "minimal change, don't duplicate" principle,
    erring toward NO_CHANGE over a duplicate ADD is the safer default;
    Layer 8 re-verifies before any actual write regardless."""
    norm_doc = _normalize_text(canonical_text)
    norm_target = _normalize_text(target)
    if not norm_target:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(norm_target)}(?!\w)", norm_doc))


def _proposal_fingerprint(canonical_path: str, action: str, entity: str, section: str, content_key: str) -> str:
    key = f"{canonical_path}|{action}|{entity}|{section}|{_normalize_text(content_key)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class ChangeProposal:
    proposal_id: str
    action: str
    entity: str
    canonical_path: Optional[str]
    section: str
    current_content: Optional[str]
    proposed_content: str
    reason: str
    evidence: list
    provenance: dict
    temporal_state: str
    confidence: str
    risk: str
    requires_escalation: bool
    fingerprint: str
    depends_on: list = field(default_factory=list)
    escalation_packet: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.action not in CHANGE_TYPES:
            raise ValueError(f"invalid action: {self.action}")
        if self.risk not in RISK_LEVELS:
            raise ValueError(f"invalid risk: {self.risk}")

    def to_dict(self) -> dict:
        return asdict(self)


def _redact(text: str) -> str:
    return redact_for_extraction(text) if text else text


def _build_provenance(source_file: str, evidence_type: Optional[str], temporal_status: str,
                       confidence: str, excerpt: str) -> dict:
    return {
        "source_file": source_file,
        "source_kind": "vault_file",
        "conversation_or_document_identity": source_file,
        "timestamp": time.time(),
        "evidence_classification": evidence_type,
        "temporal_state": temporal_status,
        "confidence": confidence,
        "evidence_summary": _redact(excerpt)[:200],
    }


def _escalation_packet(question: str, entity: str, proposed: str, existing: Optional[str],
                        conflicting_evidence: Optional[dict], why_unsafe: str, options: list[str]) -> dict:
    return {
        "status": "NEEDS_REVIEW",
        "question": question,
        "affected_entity": entity,
        "proposed_change": proposed,
        "existing_canonical_state": existing,
        "conflicting_evidence": conflicting_evidence,
        "why_deterministic_resolution_is_unsafe": why_unsafe,
        "possible_options": options,
        "recommended_next_decision": None,
    }


def plan_fact(
    fact: dict,
    source_file: str,
    *,
    canonical_reader: Optional[Callable[[str], Optional[str]]] = None,
) -> ChangeProposal:
    """
    One Layer 5 fact -> one ChangeProposal. Only handles non-relational
    fact types (PURPOSE, DECISION, BUG, FIX, PLAN, CONSTRAINT, UNKNOWN,
    ARCHITECTURE-as-prose, etc.) — relationship-shaped facts go through
    plan_relationship instead, operating on Layer 6's resolved output
    rather than the raw fact, since Layer 6 already did conflict/
    supersession resolution at the FREYA-belief-graph level.
    """
    entity = fact["subject"]
    fact_type = fact["fact_type"]
    temporal = fact["temporal_status"]
    confidence = fact["confidence"]
    evidence_type = fact.get("evidence_type")
    excerpt = fact.get("source_excerpt", "")
    section = _FACT_TYPE_TO_SECTION.get(fact_type, "Unknowns")
    canonical_path = canonical_path_for_entity(entity)
    provenance = _build_provenance(source_file, evidence_type, temporal, confidence, excerpt)
    evidence = [{"predicate": fact.get("predicate"), "temporal_status": temporal,
                 "evidence_type": evidence_type, "excerpt": _redact(excerpt)[:200]}]

    def _mk(action: str, *, content: str = "", reason: str, risk: str, requires_escalation: bool = False,
             escalation_packet: Optional[dict] = None, canonical_content: Optional[str] = None) -> ChangeProposal:
        fp = _proposal_fingerprint(canonical_path or f"UNRESOLVED:{entity}", action, entity, section,
                                    fact.get("object", ""))
        return ChangeProposal(
            proposal_id=fp, action=action, entity=entity, canonical_path=canonical_path, section=section,
            current_content=canonical_content, proposed_content=_redact(content), reason=reason,
            evidence=evidence, provenance=provenance, temporal_state=temporal, confidence=confidence,
            risk=risk, requires_escalation=requires_escalation, fingerprint=fp,
            escalation_packet=escalation_packet,
        )

    # 1. No trustworthy canonical target -> cannot even decide where this
    #    would go. Never invent one.
    if canonical_path is None:
        packet = _escalation_packet(
            f"No canonical note is registered for entity '{entity}' — where should this go?",
            entity, fact.get("object", ""), None, None,
            "canonical_path_for_entity() returned no registered target; inventing a path is exactly "
            "what the spec forbids.", ["register a canonical note for this entity", "treat as out of scope"],
        )
        return _mk("ESCALATE", reason="NO_CANONICAL_TARGET", risk="HIGH", requires_escalation=True,
                    escalation_packet=packet)

    # 2. AI_SUGGESTION / AI_CLAIM never become a canonical decision alone.
    if evidence_type in _NEVER_ALONE_CANONICAL:
        packet = _escalation_packet(
            f"'{fact.get('object','')}' for {entity} is only supported by {evidence_type} — "
            "is there independent corroborating evidence?",
            entity, fact.get("object", ""), None, evidence,
            f"{evidence_type} alone is explicitly excluded from becoming a canonical fact per evidence policy.",
            ["find corroborating evidence and re-run planning", "discard"],
        )
        return _mk("ESCALATE", reason=f"INSUFFICIENT_EVIDENCE_{evidence_type}", risk="HIGH",
                    requires_escalation=True, escalation_packet=packet)

    # 3. Never let a non-current temporal state become a current-state
    #    canonical write. PLAN/UNKNOWN/HISTORICAL/SUPERSEDED facts are
    #    still worth recording — just in the History/Plans/Unknowns
    #    section, at LOW risk, never framed as current.
    proposed_line = f"- {fact['predicate'].replace('_', ' ').title()}: {fact.get('object', '')} " \
                     f"[{temporal}, {confidence}] (source: {source_file})"

    canonical_content = canonical_reader(canonical_path) if canonical_reader else None

    if canonical_content is None:
        # Can't verify dedup/conflict without reading canonical state —
        # per spec, proceeding blind risks a duplicate. Escalate rather
        # than guess.
        packet = _escalation_packet(
            f"Cannot read canonical note at {canonical_path} to check for duplicates/conflicts.",
            entity, proposed_line, None, None,
            "No canonical_reader was available for this planning pass; proposing without reading "
            "existing state risks a duplicate append, which the spec explicitly forbids.",
            ["retry with a live vault connection", "manually verify and approve"],
        )
        return _mk("ESCALATE", reason="CANONICAL_STATE_UNVERIFIABLE", risk="MEDIUM", requires_escalation=True,
                    escalation_packet=packet, canonical_content=None)

    if _fact_already_present(canonical_content, entity, fact.get("object", "")):
        return _mk("NO_CHANGE", content=proposed_line, reason="ALREADY_PRESENT_IN_CANONICAL_NOTE", risk="LOW",
                    canonical_content=canonical_content)

    if temporal in _NEVER_CURRENT_TEMPORAL:
        section_for_temporal = "History" if temporal in _HISTORICAL_FLAVORS else section
        action = "MARK_HISTORICAL" if temporal in _HISTORICAL_FLAVORS else "APPEND"
        return _mk(action, content=proposed_line, reason="NON_CURRENT_TEMPORAL_STATE_RECORDED_AS_SUCH",
                   risk="LOW", canonical_content=canonical_content)

    # Low confidence current-flavored claims still don't get auto-appended.
    if confidence == "LOW":
        packet = _escalation_packet(
            f"LOW-confidence current-state claim for {entity}: '{proposed_line}'.",
            entity, proposed_line, None, evidence,
            "LOW confidence is explicitly review-only per policy, never an automatic canonical write.",
            ["gather stronger evidence", "manually approve"],
        )
        return _mk("ESCALATE", reason="LOW_CONFIDENCE_CURRENT_CLAIM", risk="MEDIUM", requires_escalation=True,
                    escalation_packet=packet, canonical_content=canonical_content)

    return _mk("APPEND", content=proposed_line, reason="NEW_EVIDENCE_BACKED_FACT", risk="LOW",
               canonical_content=canonical_content)


def plan_relationship(
    rel_record: dict,
    *,
    canonical_reader: Optional[Callable[[str], Optional[str]]] = None,
) -> ChangeProposal:
    """
    One Layer 6 relationship record (already resolved to ACTIVE/SUPERSEDED
    by Layer 6's own conflict/supersession logic) -> one ChangeProposal
    comparing FREYA's belief graph against actual canonical text.
    """
    entity = rel_record["source_entity"]
    rel_type = rel_record["rel_type"]
    target = rel_record["target"]
    temporal = rel_record["temporal_status"]
    confidence = rel_record["confidence"]
    status = rel_record["status"]
    source_files = rel_record.get("source_files") or []
    source_file = source_files[-1] if source_files else "unknown"
    evidence = rel_record.get("evidence") or []
    evidence_type = evidence[0].get("evidence_type") if evidence else None
    section = "Relationships"
    canonical_path = canonical_path_for_entity(entity)
    provenance = _build_provenance(source_file, evidence_type, temporal, confidence,
                                    evidence[0].get("source_excerpt", "") if evidence else "")
    proposed_line = f"- {entity} → {rel_type} → {target} [{temporal}, {confidence}]"

    def _mk(action: str, *, content: str = "", reason: str, risk: str, requires_escalation: bool = False,
             escalation_packet: Optional[dict] = None, canonical_content: Optional[str] = None) -> ChangeProposal:
        fp = _proposal_fingerprint(canonical_path or f"UNRESOLVED:{entity}", action, entity, section,
                                    f"{rel_type}|{target}")
        return ChangeProposal(
            proposal_id=fp, action=action, entity=entity, canonical_path=canonical_path, section=section,
            current_content=canonical_content, proposed_content=_redact(content), reason=reason,
            evidence=evidence, provenance=provenance, temporal_state=temporal, confidence=confidence,
            risk=risk, requires_escalation=requires_escalation, fingerprint=fp,
            escalation_packet=escalation_packet,
        )

    if canonical_path is None:
        packet = _escalation_packet(
            f"No canonical note registered for '{entity}' — where should this relationship go?",
            entity, proposed_line, None, None,
            "canonical_path_for_entity() returned no registered target.",
            ["register a canonical note for this entity", "treat as out of scope"],
        )
        return _mk("ESCALATE", reason="NO_CANONICAL_TARGET", risk="HIGH", requires_escalation=True,
                    escalation_packet=packet)

    if evidence_type in _NEVER_ALONE_CANONICAL:
        packet = _escalation_packet(
            f"Relationship {entity} {rel_type} {target} is only supported by {evidence_type}.",
            entity, proposed_line, None, evidence,
            f"{evidence_type} alone is explicitly excluded from becoming a canonical relationship.",
            ["find corroborating evidence", "discard"],
        )
        return _mk("ESCALATE", reason=f"INSUFFICIENT_EVIDENCE_{evidence_type}", risk="HIGH",
                    requires_escalation=True, escalation_packet=packet)

    canonical_content = canonical_reader(canonical_path) if canonical_reader else None
    if canonical_content is None:
        packet = _escalation_packet(
            f"Cannot read canonical note at {canonical_path} to check for duplicates/conflicts.",
            entity, proposed_line, None, None,
            "No canonical_reader was available for this planning pass.",
            ["retry with a live vault connection", "manually verify and approve"],
        )
        return _mk("ESCALATE", reason="CANONICAL_STATE_UNVERIFIABLE", risk="MEDIUM", requires_escalation=True,
                    escalation_packet=packet)

    already_mentioned = _target_already_mentioned(canonical_content, target)

    if status == "SUPERSEDED":
        if already_mentioned:
            return _mk("MARK_SUPERSEDED", content=proposed_line, reason="RELATIONSHIP_SUPERSEDED_IN_FREYA_GRAPH",
                       risk="MEDIUM", canonical_content=canonical_content)
        return _mk("NO_CHANGE", content=proposed_line, reason="SUPERSEDED_RELATIONSHIP_NOT_IN_CANONICAL_NOTE",
                   risk="LOW", canonical_content=canonical_content)

    # status == "ACTIVE" from here
    if confidence == "LOW":
        packet = _escalation_packet(
            f"LOW-confidence relationship: {proposed_line}.",
            entity, proposed_line, None, evidence,
            "LOW confidence is review-only per policy.",
            ["gather stronger evidence", "manually approve"],
        )
        return _mk("ESCALATE", reason="LOW_CONFIDENCE_RELATIONSHIP", risk="MEDIUM", requires_escalation=True,
                    escalation_packet=packet, canonical_content=canonical_content)

    if already_mentioned:
        return _mk("NO_CHANGE", content=proposed_line, reason="RELATIONSHIP_TARGET_ALREADY_IN_CANONICAL_NOTE",
                   risk="LOW", canonical_content=canonical_content)

    return _mk("ADD_RELATIONSHIP", content=proposed_line, reason="NEW_EVIDENCE_BACKED_RELATIONSHIP", risk="LOW",
               canonical_content=canonical_content)


def build_plan(
    plan_id: str,
    facts: list[dict],
    relationships: list[dict],
    source_file: str,
    *,
    canonical_reader: Optional[Callable[[str], Optional[str]]] = None,
    origin: Optional[str] = None,
) -> dict:
    """
    Top-level entry point: facts (Layer 5) + relationships (Layer 6,
    already resolved) -> one atomic plan object. Deterministic — same
    inputs always produce proposals with the same fingerprints, so
    running this twice on unchanged inputs is a no-op at the Layer 8
    apply stage (fingerprint collision = already planned).
    """
    proposals: list[ChangeProposal] = []
    for fact in facts:
        # Relationship-shaped predicates are Layer 6's job, not Layer 7's
        # fact-planning path — skip them here to avoid double-proposing
        # the same underlying evidence through two different code paths.
        if fact.get("predicate") in {
            "RUNS_ON", "HOSTED_ON", "USES", "DEPENDS_ON", "IMPLEMENTED_AS",
            "FEEDS_INTO", "PREVIOUSLY_USED", "SUPERSEDED_BY",
        }:
            continue
        proposals.append(plan_fact(fact, source_file, canonical_reader=canonical_reader))

    for rel in relationships:
        proposals.append(plan_relationship(rel, canonical_reader=canonical_reader))

    escalations = [p.to_dict() for p in proposals if p.action == "ESCALATE"]
    blocked = [p.to_dict() for p in proposals if p.action == "BLOCKED"]
    summary: dict[str, int] = {}
    for p in proposals:
        summary[p.action] = summary.get(p.action, 0) + 1

    # Normalized here for serialization consistency only. The persisted
    # `plans.origin` DB column (see state_store.record_plan/get_plan) is
    # the authoritative value canonical_executor's origin gate actually
    # checks at execution time -- never this JSON copy alone. Never
    # defaults an unrecognized/missing value to PRODUCTION.
    normalized_origin = origin if origin in ("PRODUCTION", "VALIDATION") else "UNKNOWN"

    return {
        "plan_id": plan_id,
        "generated_at": time.time(),
        "origin": normalized_origin,
        "input_snapshot": {"source_file": source_file, "fact_count": len(facts),
                            "relationship_count": len(relationships)},
        "proposals": [p.to_dict() for p in proposals],
        "escalations": escalations,
        "blocked": blocked,
        "summary": summary,
    }