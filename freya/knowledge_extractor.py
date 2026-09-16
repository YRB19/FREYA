"""
FREYA — Knowledge Extractor (Layer 5)

Given a classified, entity-resolved file, determine what meaningful
ecosystem knowledge it contains: structured facts with a fact type, a
temporal classification, an evidence type, and a confidence — NOT prose,
NOT a copy of the file, NOT a canonical write.

Architectural boundary (per spec, enforced here by omission, not by
comment): this module has no code path that writes to Notion/**, creates
wikilinks, or calls the enforcer. It returns a plain, JSON-serializable
ExtractionResult; Layer 6/7 decide what (if anything) becomes canonical.

Extraction strategy, cheapest first (per spec — Claude is not the default
extraction engine):
    1. deterministic parsing      (AST for Python imports, YAML-ish regex
                                    for docker-compose/Caddyfile service
                                    names — never executes source code)
    2. targeted sentence heuristics for prose (docs / specs)
    3. (not built here) canonical-context comparison, via an optional
       injected `canonical_lookup` callable — Layer 5 works standalone
       without it; pipeline.py may wire a real one when a live vault
       connection is available. No live wiring exists in this pass (see
       README) because it needs `mcp-tools-istefox`, which is out of
       reach of this build session — same honestly-stated gap Layer 4
       already flagged for the enforcer registry.
    4. anything genuinely ambiguous becomes an escalation-ready packet,
       never an automatic canonical decision (that's Claude's job, later,
       in Layer 10).

No LLM call anywhere in this module.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

from .classifier import redact_for_extraction

EXTRACTOR_VERSION = 1

FACT_TYPES = {
    "PURPOSE", "ARCHITECTURE", "IMPLEMENTATION", "RUNTIME", "DEPENDENCY",
    "INFRASTRUCTURE", "API", "WORKFLOW", "DECISION", "CONSTRAINT",
    "BUG", "FIX", "PLAN", "UNKNOWN",
}

TEMPORAL_STATUSES = {
    "PROPOSED", "DECIDED", "IMPLEMENTED", "OBSERVED_CURRENT",
    "HISTORICAL", "SUPERSEDED", "UNKNOWN",
}

EVIDENCE_TYPES = {
    "USER_STATEMENT", "AI_SUGGESTION", "AI_CLAIM", "USER_ACCEPTANCE",
    "IMPLEMENTED_BEHAVIOR", "OBSERVED_CURRENT_STATE", "HISTORICAL_STATE",
}

CONFIDENCE_LEVELS = {"HIGH", "MEDIUM", "LOW"}

# Predicate -> default temporal status when no hedge cue in the sentence
# overrides it. A doc/code artifact making a bare present-tense claim
# ("X runs on Y") is read as OBSERVED_CURRENT for runtime/hosting claims,
# and IMPLEMENTED for structural claims (what it's built from / depends
# on) — running is a state claim, being-built-from is a structural one.
_PREDICATE_DEFAULT_TEMPORAL = {
    "RUNS_ON": "OBSERVED_CURRENT",
    "HOSTED_ON": "OBSERVED_CURRENT",
    "CONSTRAINED_BY": "OBSERVED_CURRENT",
    "IMPLEMENTED_AS": "IMPLEMENTED",
    "USES": "IMPLEMENTED",
    "DEPENDS_ON": "IMPLEMENTED",
    "FEEDS_INTO": "IMPLEMENTED",
    "PURPOSE": "OBSERVED_CURRENT",
    "DECIDED_TO_USE": "DECIDED",
    "REJECTED": "DECIDED",
    "HAD_BUG": "HISTORICAL",
    "FIXED_BY": "IMPLEMENTED",
    "PLANNED": "PROPOSED",
    "PREVIOUSLY_USED": "HISTORICAL",
    "SUPERSEDED_BY": "SUPERSEDED",
    "UNKNOWN_STATUS": "UNKNOWN",
}

# Hedge cues override the predicate default whenever present. Checked in
# this order because some phrases are more specific than others
# ("was replaced" beats a generic present-tense reading of the same
# sentence). Do not collapse these into one bucket — that's the whole
# point of temporal classification per spec.
_HEDGE_CUES: list[tuple[str, re.Pattern]] = [
    ("SUPERSEDED", re.compile(r"\b(was replaced|has been replaced|superseded|no longer used)\b", re.I)),
    ("UNKNOWN", re.compile(r"\b(unclear|unknown|not sure|tbd|to be determined)\b", re.I)),
    ("PROPOSED", re.compile(r"\b(we plan to|plan to|should|we should|going to|proposed|considering)\b", re.I)),
    ("DECIDED", re.compile(r"\b(was selected|has been selected|was chosen|decided (to|on)|selected instead of)\b", re.I)),
    ("HISTORICAL", re.compile(r"\b(previously|used to|formerly|old architecture)\b", re.I)),
    ("OBSERVED_CURRENT", re.compile(r"\b(is currently|currently runs|is running|currently running)\b", re.I)),
]


def _hedge_temporal(sentence: str) -> Optional[str]:
    for status, pattern in _HEDGE_CUES:
        if pattern.search(sentence):
            return status
    return None


def fingerprint(entity: str, subject: str, predicate: str, obj: str, temporal_status: str) -> str:
    """Stable fingerprint for cross-source deduplication. Same fact
    mentioned in README + code + build log collapses to one fingerprint."""
    norm_obj = re.sub(r"\s+", " ", obj.strip().lower())
    key = f"{entity}|{subject}|{predicate}|{norm_obj}|{temporal_status}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


@dataclass
class Fact:
    fact_type: str
    subject: str
    predicate: str
    object: str
    temporal_status: str
    evidence_type: str
    confidence: str
    source_excerpt: str = ""
    fp: str = ""

    def __post_init__(self) -> None:
        if self.fact_type not in FACT_TYPES:
            raise ValueError(f"invalid fact_type: {self.fact_type}")
        if self.temporal_status not in TEMPORAL_STATUSES:
            raise ValueError(f"invalid temporal_status: {self.temporal_status}")
        if self.evidence_type not in EVIDENCE_TYPES:
            raise ValueError(f"invalid evidence_type: {self.evidence_type}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"invalid confidence: {self.confidence}")


@dataclass
class Conflict:
    entity: str
    predicate: str
    canonical_object: str
    new_object: str
    canonical_source: str
    new_source: str
    new_confidence: str


@dataclass
class ExtractionResult:
    entity: str
    source_path: str
    source_hash: str
    facts: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    unknowns: list = field(default_factory=list)
    confidence: str = "LOW"
    status: str = "COMPLETE"          # COMPLETE | NEEDS_REVIEW
    escalation: Optional[dict] = None
    new_fact_count: int = 0
    duplicate_fact_count: int = 0
    extractor_version: int = EXTRACTOR_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def _make_fact(entity: str, fact_type: str, predicate: str, obj: str, *,
               temporal_override: Optional[str], evidence_type: str, confidence: str,
               excerpt: str, sentence: Optional[str] = None) -> Fact:
    temporal = temporal_override or _hedge_temporal(sentence or excerpt) \
        or _PREDICATE_DEFAULT_TEMPORAL.get(predicate, "UNKNOWN")
    clean_obj = re.sub(r"\.+$", "", obj.strip()).strip()
    return Fact(
        fact_type=fact_type, subject=entity, predicate=predicate, object=clean_obj,
        temporal_status=temporal, evidence_type=evidence_type, confidence=confidence,
        source_excerpt=excerpt.strip()[:200],
    )


def _strip_generic_noun(text: str) -> str:
    m = re.match(r"(?i)^(?P<head>.+?)\s+(script|application|app|service|program|process)$", text.strip())
    return m.group("head") if m else text.strip()


# ---------------------------------------------------------------------
# Prose extraction (documentation / project_specification)
# ---------------------------------------------------------------------

_ARROW_SPLIT = re.compile(r"\s*(?:->|→)\s*")


def _split_sentences(text: str) -> list[str]:
    """Cheap heuristic splitter — good enough for pattern matching, not
    intended as NLP-grade sentence segmentation. Newlines (bullet items,
    line-wrapped prose) are treated as sentence boundaries too — flattening
    them to plain spaces was caught by real-ecosystem validation glomming
    an entire bullet list into one unparseable "sentence"."""
    with_boundaries = re.sub(r"[ \t]*\n[ \t]*", ". ", text)
    parts = re.split(r"(?<=[.!?])\s+", with_boundaries)
    return [p.strip(" .") for p in parts if p.strip(" .")]


_NEGATION_WINDOW = re.compile(r"(?i)\b(not|isn't|doesn't|don't|no longer|never)\b")


def _is_negated(sentence: str, match_start: int, lookback: int = 40) -> bool:
    """Guards RUNS_ON/HOSTED_ON-style claims against sentences like 'It is
    not currently hosted on ATLAS.' Real-ecosystem validation against a
    live vault note caught this as a genuine false positive — without this
    guard the extractor would confidently claim a negated statement as an
    OBSERVED_CURRENT fact, exactly the kind of error Layer 5 must not make."""
    window = sentence[max(0, match_start - lookback):match_start]
    return bool(_NEGATION_WINDOW.search(window))


def _extract_from_prose(entity: str, content: str, category: str) -> list[Fact]:
    facts: list[Fact] = []
    ev = "USER_STATEMENT"
    conf = "MEDIUM"

    for sentence in _split_sentences(content):
        if "->" in sentence or "→" in sentence:
            parts = [p.strip() for p in _ARROW_SPLIT.split(sentence) if p.strip()]
            for i in range(len(parts) - 1):
                facts.append(_make_fact(entity, "ARCHITECTURE", "FEEDS_INTO", parts[i + 1],
                                         temporal_override=None, evidence_type=ev, confidence=conf,
                                         excerpt=sentence, sentence=sentence))
            continue

        m = re.search(r"(?i)\btracks\s+(?P<obj>.+?)\.?$", sentence)
        if m:
            facts.append(_make_fact(entity, "PURPOSE", "PURPOSE", m.group("obj"),
                                     temporal_override="OBSERVED_CURRENT", evidence_type=ev, confidence=conf,
                                     excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)\bis (?:a|an) (?P<stack>[^.]+?) (?:application|app|stack)\b(?:\s+using\s+(?P<dep>[^.]+))?", sentence)
        if m:
            for tok in re.split(r"\s*\+\s*|\s*,\s*", m.group("stack")):
                tok = tok.strip()
                if tok:
                    facts.append(_make_fact(entity, "IMPLEMENTATION", "IMPLEMENTED_AS", tok,
                                             temporal_override=None, evidence_type=ev, confidence=conf,
                                             excerpt=sentence, sentence=sentence))
            if m.group("dep"):
                facts.append(_make_fact(entity, "DEPENDENCY", "USES", m.group("dep"),
                                         temporal_override=None, evidence_type=ev, confidence=conf,
                                         excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)\bruns\s+as\s+(?:a|an)\s+(?P<impl>[^.]+?)\s+on\s+(?P<host>[^.]+?)\.?$", sentence)
        if m:
            facts.append(_make_fact(entity, "IMPLEMENTATION", "IMPLEMENTED_AS", _strip_generic_noun(m.group("impl")),
                                     temporal_override=None, evidence_type=ev, confidence=conf,
                                     excerpt=sentence, sentence=sentence))
            facts.append(_make_fact(entity, "RUNTIME", "RUNS_ON", m.group("host"),
                                     temporal_override=None, evidence_type=ev, confidence=conf,
                                     excerpt=sentence, sentence=sentence))
        else:
            m = re.search(r"(?i)\bruns\s+(?:locally\s+)?on\s+(?P<host>[^.]+?)\.?$", sentence)
            if m and not _is_negated(sentence, m.start()):
                facts.append(_make_fact(entity, "RUNTIME", "RUNS_ON", m.group("host"),
                                         temporal_override=None, evidence_type=ev, confidence=conf,
                                         excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)\bhosted on\s+(?P<host>[^.]+?)\.?$", sentence)
        if m and not _is_negated(sentence, m.start()):
            facts.append(_make_fact(entity, "INFRASTRUCTURE", "HOSTED_ON", m.group("host"),
                                     temporal_override=None, evidence_type=ev, confidence=conf,
                                     excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)\bdepends on\s+(?P<dep>[^.]+?)\.?$", sentence)
        if m:
            facts.append(_make_fact(entity, "DEPENDENCY", "DEPENDS_ON", m.group("dep"),
                                     temporal_override=None, evidence_type=ev, confidence=conf,
                                     excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)^(?P<obj>[\w.\-]+)\s+was selected(?:\s+instead of\s+(?P<alt>[\w.\-]+))?", sentence)
        if m:
            facts.append(_make_fact(entity, "DECISION", "DECIDED_TO_USE", m.group("obj"),
                                     temporal_override="DECIDED", evidence_type=ev, confidence="HIGH",
                                     excerpt=sentence, sentence=sentence))
            if m.group("alt"):
                facts.append(_make_fact(entity, "DECISION", "REJECTED", m.group("alt"),
                                         temporal_override="DECIDED", evidence_type=ev, confidence=conf,
                                         excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)remains on\s+(?P<loc>[\w\s]+?)\s+due to\s+(?P<reason>[^.]+?)\.?$", sentence)
        if m:
            facts.append(_make_fact(entity, "CONSTRAINT", "CONSTRAINED_BY", m.group("reason"),
                                     temporal_override=None, evidence_type=ev, confidence=conf,
                                     excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)(?:the\s+)?(?P<bug>.+?)\s+bug was fixed by\s+(?P<fix>.+?)\.?$", sentence)
        if m:
            facts.append(_make_fact(entity, "BUG", "HAD_BUG", m.group("bug"),
                                     temporal_override="HISTORICAL", evidence_type=ev, confidence=conf,
                                     excerpt=sentence, sentence=sentence))
            facts.append(_make_fact(entity, "FIX", "FIXED_BY", m.group("fix"),
                                     temporal_override="IMPLEMENTED", evidence_type=ev, confidence=conf,
                                     excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)(?:we\s+)?plan to\s+(?P<obj>[^.]+?)\.?$", sentence)
        if m:
            facts.append(_make_fact(entity, "PLAN", "PLANNED", m.group("obj"),
                                     temporal_override="PROPOSED", evidence_type=ev, confidence=conf,
                                     excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)^(?P<subj>[\w\s]+?)\s+is\s+(?:currently\s+)?unclear\.?$", sentence)
        if m:
            facts.append(_make_fact(entity, "UNKNOWN", "UNKNOWN_STATUS", "UNKNOWN",
                                     temporal_override="UNKNOWN", evidence_type=ev, confidence="LOW",
                                     excerpt=sentence, sentence=sentence))

        m = re.search(r"(?i)previously\s+(?:used|had|ran)\s+(?P<obj>[^.]+?)\.?$", sentence)
        if m:
            facts.append(_make_fact(entity, "ARCHITECTURE", "PREVIOUSLY_USED", m.group("obj"),
                                     temporal_override="HISTORICAL", evidence_type="HISTORICAL_STATE", confidence=conf,
                                     excerpt=sentence, sentence=sentence))

        # Broader ADR-style historical marker: "X originally specified/used
        # Y[-based] ..." — a generalizable pattern (common in decision-
        # record prose), not narrowly fit to one sentence. Caught missing
        # by real-ecosystem validation: the live vault's actual ResearchOS
        # note uses exactly this phrasing ("ADR-001 originally specified
        # n8n-based hosting/orchestration..."), which the narrower
        # "previously used X" pattern above didn't match at all — a real
        # false negative on the flagship supersession example.
        m = re.search(r"(?i)originally\s+(?:specified|used|had)\s+(?P<obj>[\w][\w\-]*?)(?:-based)?\b", sentence)
        if m and re.search(r"(?i)\b(historical|superseded)\b", sentence):
            facts.append(_make_fact(entity, "ARCHITECTURE", "PREVIOUSLY_USED", m.group("obj"),
                                     temporal_override="HISTORICAL", evidence_type="HISTORICAL_STATE", confidence=conf,
                                     excerpt=sentence, sentence=sentence))

        if re.search(r"(?i)\bwas replaced\b", sentence):
            facts.append(_make_fact(entity, "ARCHITECTURE", "SUPERSEDED_BY", sentence,
                                     temporal_override="SUPERSEDED", evidence_type="HISTORICAL_STATE", confidence=conf,
                                     excerpt=sentence, sentence=sentence))

    return facts


# ---------------------------------------------------------------------
# Source code extraction — inspection only, never execution.
# ---------------------------------------------------------------------

_FRAMEWORK_IMPORT_MAP = {
    "fastapi": ("FastAPI", "IMPLEMENTATION", "IMPLEMENTED_AS"),
    "flask": ("Flask", "IMPLEMENTATION", "IMPLEMENTED_AS"),
    "django": ("Django", "IMPLEMENTATION", "IMPLEMENTED_AS"),
    "express": ("Express", "IMPLEMENTATION", "IMPLEMENTED_AS"),
    "react": ("React", "IMPLEMENTATION", "IMPLEMENTED_AS"),
    "vue": ("Vue", "IMPLEMENTATION", "IMPLEMENTED_AS"),
    "sqlalchemy": ("SQLAlchemy", "IMPLEMENTATION", "IMPLEMENTED_AS"),
    "psycopg2": ("PostgreSQL", "DEPENDENCY", "USES"),
    "asyncpg": ("PostgreSQL", "DEPENDENCY", "USES"),
    "pymongo": ("MongoDB", "DEPENDENCY", "USES"),
    "redis": ("Redis", "DEPENDENCY", "USES"),
    "celery": ("Celery", "DEPENDENCY", "USES"),
    "uvicorn": ("Uvicorn", "DEPENDENCY", "USES"),
}


def _extract_from_code(entity: str, content: str, rel_path: str) -> list[Fact]:
    facts: list[Fact] = []
    suffix = Path(rel_path).suffix.lower()
    imports: set[str] = set()

    if suffix == ".py":
        try:
            tree = ast.parse(content)  # parsed as data, never executed
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split(".")[0].lower())
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0].lower())
        except SyntaxError:
            pass  # malformed/partial file — extract nothing rather than guess
    else:
        for m in re.finditer(r"(?i)(?:from\s+|require\()\s*['\"]([\w@/.\-]+)['\"]", content):
            token = m.group(1).lstrip("./").split("/")[0].lstrip("@")
            imports.add(token.lower())

    for imp in imports:
        if imp in _FRAMEWORK_IMPORT_MAP:
            obj, fact_type, predicate = _FRAMEWORK_IMPORT_MAP[imp]
            facts.append(_make_fact(entity, fact_type, predicate, obj,
                                     temporal_override=None, evidence_type="IMPLEMENTED_BEHAVIOR",
                                     confidence="HIGH", excerpt=f"import {imp}"))
    return facts


# ---------------------------------------------------------------------
# Configuration extraction — service/infra names only, never secret values.
# ---------------------------------------------------------------------

def _extract_from_config(entity: str, content: str, rel_path: str) -> list[Fact]:
    facts: list[Fact] = []
    name = Path(rel_path).name.lower()

    if "docker-compose" in name or name.endswith(("compose.yml", "compose.yaml")):
        in_services = False
        for line in content.splitlines():
            if re.match(r"(?i)^services:\s*$", line):
                in_services = True
                continue
            if in_services:
                m = re.match(r"^\s{2}([\w\-]+):\s*$", line)
                if m:
                    facts.append(_make_fact(entity, "INFRASTRUCTURE", "USES", m.group(1),
                                             temporal_override="IMPLEMENTED", evidence_type="IMPLEMENTED_BEHAVIOR",
                                             confidence="MEDIUM", excerpt=f"docker-compose service: {m.group(1)}"))
                elif line and not line.startswith((" ", "\t")):
                    in_services = False  # left the services block

    if name in ("caddyfile",) or name.endswith(".caddyfile"):
        facts.append(_make_fact(entity, "INFRASTRUCTURE", "USES", "Caddy",
                                 temporal_override="IMPLEMENTED", evidence_type="IMPLEMENTED_BEHAVIOR",
                                 confidence="HIGH", excerpt="Caddyfile present"))

    return facts


# ---------------------------------------------------------------------
# Dedup / conflict / escalation
# ---------------------------------------------------------------------

def _overall_confidence(facts: list[Fact]) -> str:
    if not facts:
        return "LOW"
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in facts:
        counts[f.confidence] += 1
    if counts["HIGH"] > 0 and counts["HIGH"] >= counts["MEDIUM"] and counts["HIGH"] >= counts["LOW"]:
        return "HIGH"
    if counts["MEDIUM"] >= counts["LOW"]:
        return "MEDIUM"
    return "LOW"


def detect_conflicts(entity: str, facts: list[Fact],
                      canonical_lookup: Optional[Callable[[str, str], Optional[str]]],
                      source_path: str) -> list[Conflict]:
    """
    canonical_lookup(entity, predicate) -> known canonical object value, or
    None if nothing canonical is known for that predicate yet. Optional —
    Layer 5 must work with no live vault connection at all (see module
    docstring). Only current-state-flavored temporal statuses are checked;
    a PLAN or a HISTORICAL mention contradicting canonical truth is not a
    conflict, it's exactly the kind of "three different facts" the spec
    asks Layer 5 to keep separate.
    """
    conflicts: list[Conflict] = []
    if canonical_lookup is None:
        return conflicts
    for f in facts:
        if f.temporal_status not in ("OBSERVED_CURRENT", "IMPLEMENTED"):
            continue
        canonical_obj = canonical_lookup(entity, f.predicate)
        if canonical_obj is None:
            continue
        if _normalize(canonical_obj) != _normalize(f.object):
            conflicts.append(Conflict(
                entity=entity, predicate=f.predicate,
                canonical_object=canonical_obj, new_object=f.object,
                canonical_source="canonical_context", new_source=source_path,
                new_confidence=f.confidence,
            ))
    return conflicts


def build_escalation_packet(entity: str, conflict: Conflict, sources: list[str]) -> dict:
    return {
        "status": "NEEDS_REVIEW",
        "reason": f"CONFLICTING_{conflict.predicate}",
        "entity": entity,
        "canonical": {conflict.predicate: conflict.canonical_object},
        "new_evidence": {conflict.predicate: conflict.new_object},
        "sources": sources,
        "question": f"Which {conflict.predicate.replace('_', ' ').lower()} is current for {entity}?",
    }


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------

def extract_knowledge(
    entity: str,
    rel_path: str,
    content_hash: str,
    content: Optional[str],
    category: str,
    *,
    canonical_lookup: Optional[Callable[[str, str], Optional[str]]] = None,
    known_fingerprints: Optional[set[str]] = None,
) -> ExtractionResult:
    """
    Pure-ish function (no I/O beyond what's already passed in). Never
    writes canonical knowledge, never calls an LLM, never executes the
    content it's given.
    """
    known_fingerprints = known_fingerprints or set()
    safe_content = redact_for_extraction(content) if content else ""

    if category in ("documentation", "project_specification"):
        raw_facts = _extract_from_prose(entity, safe_content, category)
    elif category == "source_code":
        raw_facts = _extract_from_code(entity, safe_content, rel_path)
    elif category == "configuration":
        raw_facts = _extract_from_config(entity, safe_content, rel_path)
    else:
        raw_facts = []

    kept: list[Fact] = []
    new_count = 0
    dup_count = 0
    for f in raw_facts:
        fp = fingerprint(entity, f.subject, f.predicate, f.object, f.temporal_status)
        f.fp = fp
        if fp in known_fingerprints:
            dup_count += 1
            continue
        new_count += 1
        kept.append(f)

    conflicts = detect_conflicts(entity, kept, canonical_lookup, rel_path)
    escalation = None
    status = "COMPLETE"
    if conflicts:
        status = "NEEDS_REVIEW"
        escalation = build_escalation_packet(entity, conflicts[0], [rel_path])

    relationships = [
        {"subject": f.subject, "predicate": f.predicate, "object": f.object,
         "temporal_status": f.temporal_status, "confidence": f.confidence}
        for f in kept
    ]
    unknowns = [asdict(f) for f in kept if f.fact_type == "UNKNOWN"]

    return ExtractionResult(
        entity=entity,
        source_path=rel_path,
        source_hash=content_hash,
        facts=[asdict(f) for f in kept],
        relationships=relationships,
        conflicts=[asdict(c) for c in conflicts],
        unknowns=unknowns,
        confidence=_overall_confidence(kept),
        status=status,
        escalation=escalation,
        new_fact_count=new_count,
        duplicate_fact_count=dup_count,
    )