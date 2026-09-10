"""
FREYA — Entity Resolver (Layer 4)

Given a classified file, determine which canonical entity (or entities) it
belongs to, with confidence and evidence. Deterministic-first: path rules
before content rules, content rules before ambiguity.

Registry note: this module's ENTITY_REGISTRY keys are meant to line up 1:1
with the enforcer's `_ENTITY_SCOPE_REGISTRY` keys. I could not inspect
enforcer.py in this pass (it lives on local disk outside every tool
available to me in this session — not a gap I'm papering over). The
registry below was instead built from the real, live vault via
`mcp-tools-istefox:list_vault_files`, which is ground truth for "what
canonical entities currently exist." Reconciling the two registries is a
Layer 8 prerequisite, not a Layer 4 one — flagged in the final report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------
# Entity registry — derived from the real vault listing (Notion/**), not
# invented. Each entity has:
#   path_markers   : regex patterns against the vault-relative path; a hit
#                     here is HIGH-confidence evidence.
#   content_aliases: bare names/phrases that may appear in file content.
#                    Matching one of these is only MEDIUM/LOW evidence on
#                    its own — see `_content_signal_strength`.
#   nickname_risk  : True if this name is known to double as a Claude.ai
#                     account nickname elsewhere in the ecosystem (per
#                     established knowledge). Content-only matches for
#                     these entities require extra corroborating context
#                     before they count as real evidence at all.
#   vault_note     : canonical landing note, for reference/audit only.
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class EntityDef:
    name: str
    path_markers: tuple[str, ...]
    content_aliases: tuple[str, ...] = ()
    nickname_risk: bool = False
    vault_note: Optional[str] = None


def _pm(token: str) -> str:
    """
    Build a path-marker regex for a bare entity token that matches it as a
    directory segment (`UsageOS/...`), a filename stem (`ATLAS.md`,
    `n8n.md`), or a space/underscore/hyphen-separated filename
    (`ClipOS Logs.md`, `ClipOS_Logs.md`) — not just "directory or exact
    end of string", which misses every top-level landing note in the real
    vault (this was caught by real-ecosystem validation, not guessed).
    """
    return rf"(?i)(^|/){token}(/|\.|[ _-]|$)"


ENTITY_REGISTRY: tuple[EntityDef, ...] = (
    EntityDef("ATLAS", path_markers=(_pm("atlas"),), content_aliases=("atlas server", "atlas host"),
              vault_note="Notion/ATLAS.md"),
    EntityDef("UsageOS", path_markers=(_pm("usageos"),), content_aliases=("usageos",),
              vault_note="Notion/UsageOS/UsageOS.md"),
    EntityDef("ResearchOS", path_markers=(_pm("researchos"),), content_aliases=("researchos",),
              nickname_risk=True, vault_note="Notion/ResearchOS/ResearchOS.md"),
    # StudioOS the software project (distinct from the scene-map collection below).
    # Negative lookahead excludes any path that also contains "scene map",
    # so the more specific entity below wins instead of both firing.
    EntityDef("StudioOS", path_markers=(rf"(?i)(^|/)studioos(/|\.|[ _-]|$)(?!.*scene map)",),
              content_aliases=("studioos",), nickname_risk=True, vault_note="Notion/StudioOS/StudioOS.md"),
    # The creative scene-map collection is a genuinely distinct referent per
    # established ecosystem knowledge — deliberately modeled as its own
    # candidate rather than folded into "StudioOS", per the disambiguation
    # that's already open in the ecosystem.
    EntityDef("StudioOS:SceneMap", path_markers=(r"(?i)studioos.*scene map",),
              content_aliases=("chapter 1 scene map",),
              vault_note="Notion/StudioOS - Chapter 1 Scene Map/StudioOS - Chapter 1 Scene Map.base"),
    EntityDef("ClipOS", path_markers=(_pm("clipos"),),
              content_aliases=("clipos",), nickname_risk=True, vault_note="Notion/ClipOS/ClipOS.md"),
    EntityDef("CompanionOS", path_markers=(_pm("companionos"),), content_aliases=("companionos",),
              vault_note="Notion/CompanionOS/CompanionOS.md"),
    EntityDef("PersonaOS", path_markers=(_pm("personaos"),), content_aliases=("personaos", "127.0.0.1:8765"),
              vault_note="Notion/PersonaOS/PersonaOS.md"),
    EntityDef("Infrastructure/n8n", path_markers=(_pm("n8n"),),
              content_aliases=("n8n workflow", "n8n-mcp", "n8nio", "n8n:"),
              vault_note="Notion/Infrastructure/n8n.md"),
    EntityDef("Infrastructure/PostgreSQL", path_markers=(_pm(r"postgres(ql)?"),),
              content_aliases=("postgresql", "psql", "pg_dump"), vault_note="Notion/Infrastructure/PostgreSQL.md"),
    EntityDef("Infrastructure/Caddy", path_markers=(_pm("caddy"), r"(?i)caddyfile$"),
              content_aliases=("caddyfile", "reverse proxy"), vault_note="Notion/Infrastructure/Caddy.md"),
)

_BY_NAME = {e.name: e for e in ENTITY_REGISTRY}

# Phrases that mean "this mention is an account label, not a software
# reference" — used to suppress false-positive content matches for
# nickname_risk entities (ResearchOS / StudioOS / ClipOS also being Claude
# account nicknames tracked inside UsageOS).
ACCOUNT_NICKNAME_CONTEXT = re.compile(
    r"(?i)\b(account|nickname|subscription[_ ]?tier|claude\.ai|workspace|seat|api[_ ]?key label)\b"
)

MULTI_ENTITY_HINT_FILES = re.compile(r"(?i)(docker-compose|compose\.ya?ml|caddyfile|\.env\.shared|orchestrat)")

HIGH_THRESHOLD = 0.75
AMBIGUITY_GAP = 0.15   # if top1 - top2 < this, and both are non-trivial, it's AMBIGUOUS
LOW_FLOOR = 0.20        # candidates below this are not reported at all


@dataclass
class EntityResolution:
    status: str                      # RESOLVED | AMBIGUOUS | UNKNOWN | MULTI_ENTITY
    entity: Optional[str] = None     # set when RESOLVED
    confidence: Optional[str] = None  # HIGH | MEDIUM | LOW, set when RESOLVED
    candidates: dict[str, float] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    multi_entities: list[str] = field(default_factory=list)  # set when MULTI_ENTITY


def _path_signal(rel_path: str) -> dict[str, tuple[float, dict]]:
    scores: dict[str, tuple[float, dict]] = {}
    for e in ENTITY_REGISTRY:
        for pattern in e.path_markers:
            if re.search(pattern, rel_path):
                # StudioOS:SceneMap path marker is more specific than plain
                # StudioOS, so when both could fire, the specific one wins
                # by being scored higher — no special-casing needed beyond
                # the regex itself already excluding scene-map paths from
                # the plain StudioOS pattern.
                prev = scores.get(e.name, (0.0, {}))
                if 0.95 > prev[0]:
                    scores[e.name] = (0.95, {"signal": "path_marker", "pattern": pattern, "path": rel_path})
    return scores


def _content_signal(rel_path: str, content: Optional[str]) -> dict[str, tuple[float, dict]]:
    scores: dict[str, tuple[float, dict]] = {}
    if not content:
        return scores
    lowered = content.lower()
    for e in ENTITY_REGISTRY:
        for alias in e.content_aliases:
            if alias.lower() not in lowered:
                continue
            # find a representative line for evidence + nickname-context check
            for line in content.splitlines():
                if alias.lower() in line.lower():
                    if e.nickname_risk and ACCOUNT_NICKNAME_CONTEXT.search(line):
                        # This is very likely a Claude account label, not a
                        # reference to the software project. Suppress —
                        # do not let it contribute evidence at all.
                        continue
                    weight = 0.55 if not e.nickname_risk else 0.40
                    prev = scores.get(e.name, (0.0, {}))
                    if weight > prev[0]:
                        scores[e.name] = (weight, {"signal": "content_alias", "alias": alias,
                                                    "line_excerpt": line.strip()[:160]})
                    break
    return scores


def resolve_entity(rel_path: str, content: Optional[str] = None) -> EntityResolution:
    """
    Pure function: no I/O, no state-store access, no LLM calls. Cheap enough
    to run on every classified file. `content` is optional — pass it when
    the classifier has already read the file (e.g. for docs/config/code);
    omit it for anything binary or oversized.
    """
    evidence: list[dict] = []
    path_scores = _path_signal(rel_path)
    for name, (score, ev) in path_scores.items():
        evidence.append({**ev, "entity": name, "weight": score})

    combined: dict[str, float] = dict.fromkeys(path_scores, 0.0)
    for name, (score, _) in path_scores.items():
        combined[name] = max(combined.get(name, 0.0), score)

    # Only fall back to content signals for entities that didn't already
    # get a strong path hit — content is corroboration, not an override.
    content_scores = _content_signal(rel_path, content)
    for name, (score, ev) in content_scores.items():
        evidence.append({**ev, "entity": name, "weight": score})
        combined[name] = max(combined.get(name, 0.0), score)

    # Multi-entity heuristic: shared infra filenames that hit >= 2 distinct
    # entities with meaningful evidence.
    significant = {k: v for k, v in combined.items() if v >= LOW_FLOOR}
    if MULTI_ENTITY_HINT_FILES.search(Path(rel_path).name) and len(significant) >= 2:
        return EntityResolution(
            status="MULTI_ENTITY",
            candidates=significant,
            evidence=evidence,
            multi_entities=sorted(significant, key=significant.get, reverse=True),
        )

    if not significant:
        return EntityResolution(status="UNKNOWN", candidates={}, evidence=evidence)

    ranked = sorted(significant.items(), key=lambda kv: kv[1], reverse=True)
    top_name, top_score = ranked[0]
    runner_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if len(ranked) > 1 and (top_score - runner_score) < AMBIGUITY_GAP and runner_score >= LOW_FLOOR:
        return EntityResolution(status="AMBIGUOUS", candidates=significant, evidence=evidence)

    if top_score >= HIGH_THRESHOLD:
        confidence = "HIGH"
    elif top_score >= 0.40:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    if confidence == "LOW":
        # Per policy: LOW confidence never auto-resolves canonical ownership.
        return EntityResolution(status="AMBIGUOUS", candidates=significant, evidence=evidence)

    return EntityResolution(status="RESOLVED", entity=top_name, confidence=confidence,
                             candidates=significant, evidence=evidence)


def known_entity_names() -> list[str]:
    return [e.name for e in ENTITY_REGISTRY]


def canonical_path_for_entity(entity: str) -> Optional[str]:
    """Return the registered canonical vault path for a known entity name,
    or None if the entity has no registered canonical target. Reads the
    same ENTITY_REGISTRY Layer 4 already uses (the vault_note field) --
    no filename-similarity guessing, no invented paths. An entity absent
    from ENTITY_REGISTRY, or present but with vault_note=None, returns
    None -- callers (Layer 7) must treat that as ESCALATE/NO_CANONICAL_TARGET,
    never as license to invent a path."""
    e = _BY_NAME.get(entity)
    if e is None:
        return None
    return e.vault_note
