"""
FREYA — Relationship Engine (Layer 6)

Turns Layer 5's extracted facts into structured, evidence-backed graph
edges: "what connects to what," with direction, temporal status, and
confidence — never "what does Claude think connects to what."

Architectural boundary, enforced by omission same as Layer 5: nothing in
this module calls a canonical-write function, creates a wikilink, or
touches Notion/**. It reads/writes only FREYA's own relationship ledger
(a new SQLite table) and returns structured candidates/decisions. Layer 7
decides what (if anything) becomes a canonical wikilink or note edit.

Core design decision, and why: a relationship's identity (its fingerprint)
is (source, relationship_type, normalized_target) — deliberately WITHOUT
temporal_status baked in. That's what makes "same edge confirmed by a
second source" collapse to one row (dedup / EXISTING) instead of two, and
what makes supersession a first-class case rather than a special one: an
edge's temporal_status is mutable state on that one row, not part of its
identity. The one thing that genuinely needs a *different* row for the
"same relationship" conceptually (e.g. ResearchOS's n8n-orchestration era
vs its current ATLAS runtime) already gets one for free, because the
relationship_type and target actually differ (ORCHESTRATED_BY/n8n vs
RUNS_ON/ATLAS) — supersession here is about recognizing that a new
HISTORICAL-flavored fact about an old target should flip that OLD row's
status, not about merging rows.

No LLM call anywhere in this module, same as Layer 5.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from .entity_resolver import known_entity_names

RELATIONSHIP_TYPES = {
    "RUNS_ON", "HOSTED_ON", "USES", "DEPENDS_ON", "ORCHESTRATED_BY",
    "ROUTED_BY", "FEEDS", "PRODUCES", "CONSUMES", "INTEGRATES_WITH",
    "PART_OF", "IMPLEMENTED_AS", "RELATED_TO", "HISTORICAL",
}

# temporal_status values a relationship can carry are Layer 5's, reused
# as-is — no separate vocabulary to keep in sync.
CURRENT_FLAVORS = {"OBSERVED_CURRENT", "IMPLEMENTED", "DECIDED"}
HISTORICAL_FLAVORS = {"HISTORICAL", "SUPERSEDED"}
NON_CURRENT_FLAVORS = HISTORICAL_FLAVORS | {"PROPOSED", "UNKNOWN"}

# Layer 5 predicate -> Layer 6 relationship type. Only predicates that
# genuinely describe a connection between two named things (project,
# infra, service, runtime) become graph edges. Narrative/descriptive
# predicates never do — see _NON_RELATIONAL_PREDICATES below.
_PREDICATE_TO_RELATIONSHIP_TYPE = {
    "RUNS_ON": "RUNS_ON",
    "HOSTED_ON": "HOSTED_ON",
    "USES": "USES",
    "DEPENDS_ON": "DEPENDS_ON",
    "IMPLEMENTED_AS": "IMPLEMENTED_AS",
    "FEEDS_INTO": "FEEDS",
    "PREVIOUSLY_USED": "HISTORICAL",
    "SUPERSEDED_BY": "HISTORICAL",
}

# These Layer 5 predicates produce prose-fragment objects ("add specialist
# agents", "ATLAS resource constraints"), not named entities/infra — never
# graph edges, regardless of confidence. Includes DECISION predicates:
# "PostgreSQL was selected" is a decision fact, not by itself evidence
# that anything currently USES PostgreSQL (that still needs its own
# RUNS_ON/USES fact from implementation evidence).
_NON_RELATIONAL_PREDICATES = {
    "PURPOSE", "DECIDED_TO_USE", "REJECTED", "HAD_BUG", "FIXED_BY",
    "PLANNED", "UNKNOWN_STATUS", "CONSTRAINED_BY",
}

# Granularity filter for USES/DEPENDS_ON only (RUNS_ON/HOSTED_ON/
# IMPLEMENTED_AS are inherently ecosystem-significant by construction —
# Layer 5 only ever produces them from architecturally meaningful matches).
# Keeps "import requests"-style noise out of the ecosystem graph per spec:
# a target only becomes a graph node if it's a known project/entity name
# or a recognized piece of shared infrastructure.
_KNOWN_INFRA_TERMS = {
    "postgresql", "redis", "mongodb", "caddy", "celery", "uvicorn",
    "docker", "cloudflare tunnel", "fastapi", "react", "vue", "flask",
    "django", "express", "sqlalchemy", "n8n", "atlas", "telegram",
    "telegram bot api",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _is_known_target(obj: str) -> bool:
    """
    True if `obj` names a known project/entity or recognized shared infra
    component. Deliberately checks containment of a known term, not exact
    equality — real-ecosystem validation caught the exact-match version
    rejecting "PostgreSQL for storage" (Layer 5's bullet-derived object
    text often carries a trailing clause) even though "PostgreSQL" is
    unambiguously the actual target. Still conservative: a known term must
    appear as its own word, not as a substring of an unrelated word.
    """
    norm = _norm(obj)
    known_terms = _KNOWN_INFRA_TERMS | {_norm(name.split("/")[-1]) for name in known_entity_names()}
    for term in known_terms:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", norm):
            return True
    return False


def relationship_fingerprint(source: str, rel_type: str, target: str) -> str:
    key = f"{_norm(source)}|{rel_type}|{_norm(target)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class RelationshipCandidate:
    source: str
    rel_type: str
    target: str
    temporal_status: str
    confidence: str
    evidence: list = field(default_factory=list)
    source_files: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.rel_type not in RELATIONSHIP_TYPES:
            raise ValueError(f"invalid relationship_type: {self.rel_type}")


@dataclass
class EvaluationResult:
    action: str  # ADD | CONFIRM | SUPERSEDE_AND_ADD | CONFLICT | NEEDS_REVIEW
    fingerprint: str
    candidate: RelationshipCandidate
    existing: Optional[dict] = None
    supersedes: list = field(default_factory=list)   # list of existing dicts to flip to SUPERSEDED
    conflicts_with: list = field(default_factory=list)  # list of existing dicts that contradict


def facts_to_candidates(facts: list[dict]) -> list[RelationshipCandidate]:
    """
    Translate Layer 5 fact dicts (already entity-tagged via `subject`)
    into Layer 6 relationship candidates. Filters out non-relational
    predicates and applies the USES/DEPENDS_ON granularity gate. This is
    the ONLY place Layer 5 output is read — nothing else in this module
    knows about Layer 5's schema, which keeps the two layers loosely
    coupled the way Layers 4/5 already are.
    """
    candidates: list[RelationshipCandidate] = []
    for f in facts:
        predicate = f.get("predicate")
        if predicate in _NON_RELATIONAL_PREDICATES:
            continue
        rel_type = _PREDICATE_TO_RELATIONSHIP_TYPE.get(predicate)
        if rel_type is None:
            continue
        target = f["object"]
        if rel_type in ("USES", "DEPENDS_ON") and not _is_known_target(target):
            continue
        candidates.append(RelationshipCandidate(
            source=f["subject"], rel_type=rel_type, target=target,
            temporal_status=f["temporal_status"], confidence=f["confidence"],
            evidence=[{"predicate": predicate, "temporal_status": f["temporal_status"],
                       "evidence_type": f.get("evidence_type"), "source_excerpt": f.get("source_excerpt", "")}],
        ))
    return candidates


def evaluate_candidate(
    candidate: RelationshipCandidate,
    existing_exact: Optional[dict],
    existing_for_source: list[dict],
) -> EvaluationResult:
    """
    Pure decision function — no I/O. `existing_exact` is the stored row
    for this exact fingerprint, if any. `existing_for_source` is every
    ACTIVE stored relationship whose source matches this candidate's
    source (any type/target), needed for supersession and conflict
    checks that aren't fingerprint-exact.
    """
    fp = relationship_fingerprint(candidate.source, candidate.rel_type, candidate.target)

    # 1. Exact same edge already known -> confirm, never duplicate.
    if existing_exact is not None and existing_exact.get("status") == "ACTIVE":
        return EvaluationResult(action="CONFIRM", fingerprint=fp, candidate=candidate, existing=existing_exact)

    # 2. This candidate explicitly marks something historical (Layer 5's
    #    PREVIOUSLY_USED/SUPERSEDED_BY predicates land here). If an ACTIVE
    #    current-flavored record for the same source points at the SAME
    #    target under a DIFFERENT relationship_type (e.g. the old
    #    ORCHESTRATED_BY/n8n edge, now being described as historical),
    #    that old edge is superseded — flip it, keep it, add this one.
    if candidate.temporal_status in HISTORICAL_FLAVORS:
        to_supersede = [
            r for r in existing_for_source
            if r["status"] == "ACTIVE"
            and _norm(r["target"]) == _norm(candidate.target)
            and r["temporal_status"] in CURRENT_FLAVORS
        ]
        return EvaluationResult(action="SUPERSEDE_AND_ADD", fingerprint=fp, candidate=candidate,
                                 supersedes=to_supersede)

    # 3. This candidate claims current state, but an ACTIVE current-flavored
    #    record already exists for the same (source, rel_type) pointing at
    #    a DIFFERENT target. Two sources disagree about what's true right
    #    now — do not silently pick a winner.
    if candidate.temporal_status in CURRENT_FLAVORS:
        conflicting = [
            r for r in existing_for_source
            if r["status"] == "ACTIVE"
            and r["rel_type"] == candidate.rel_type
            and _norm(r["target"]) != _norm(candidate.target)
            and r["temporal_status"] in CURRENT_FLAVORS
        ]
        if conflicting:
            return EvaluationResult(action="CONFLICT", fingerprint=fp, candidate=candidate,
                                     conflicts_with=conflicting)

    # 4. Low-confidence candidates never become an automatic graph edge —
    #    per spec, LOW confidence is review-only, not canonical-track.
    if candidate.confidence == "LOW":
        return EvaluationResult(action="NEEDS_REVIEW", fingerprint=fp, candidate=candidate)

    return EvaluationResult(action="ADD", fingerprint=fp, candidate=candidate)


def build_relationship_conflict_packet(candidate: RelationshipCandidate, conflicts_with: list[dict],
                                        sources: list[str]) -> dict:
    other = conflicts_with[0]
    return {
        "status": "RELATIONSHIP_CONFLICT",
        "reason": f"CONFLICTING_{candidate.rel_type}",
        "source_entity": candidate.source,
        "relationship_type": candidate.rel_type,
        "canonical": {"target": other["target"], "confidence": other["confidence"]},
        "new_evidence": {"target": candidate.target, "confidence": candidate.confidence},
        "sources": sources,
        "question": f"Which {candidate.rel_type.replace('_', ' ').lower()} target is current for {candidate.source}?",
    }