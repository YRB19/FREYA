"""
FREYA — Persistent State Store (Layer 2)

SQLite-backed idempotency ledger. Every file FREYA has ever seen gets a row.
Re-encountering the same content (same hash) is a no-op. This is what makes
the whole pipeline survive restarts, duplicate fs events, and crashes without
double-writing canonical knowledge.

States (see spec "STATE / QUEUE SYSTEM"):
    NEW, DISCOVERED, CLASSIFIED, PROCESSING, NEEDS_REVIEW,
    READY_TO_WRITE, WRITING, VERIFIED, IGNORED, FAILED
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Iterator

VALID_STATES = {
    "NEW", "DISCOVERED", "CLASSIFIED", "PROCESSING", "NEEDS_REVIEW",
    "READY_TO_WRITE", "WRITING", "VERIFIED", "IGNORED", "FAILED",
    "UNAVAILABLE",  # source file deleted; canonical knowledge preserved
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path                TEXT PRIMARY KEY,
    content_hash        TEXT,
    last_seen_ts        REAL NOT NULL,
    last_processed_ts   REAL,
    classification      TEXT,
    entity              TEXT,
    confidence          TEXT,
    status              TEXT NOT NULL DEFAULT 'NEW',
    extracted_knowledge TEXT,       -- JSON blob
    relationships       TEXT,       -- JSON blob (list of {subject,predicate,object,evidence})
    canonical_notes     TEXT,       -- JSON list of vault-relative paths affected
    escalated           INTEGER NOT NULL DEFAULT 0,
    escalation_reason   TEXT,
    last_error          TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    processing_version  INTEGER NOT NULL DEFAULT 1,
    tx_id               TEXT        -- enforcer transaction id, if any
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    path            TEXT,
    entity          TEXT,
    event           TEXT NOT NULL,   -- e.g. 'classified', 'escalated', 'canonical_write', 'verified'
    detail          TEXT,            -- JSON blob: why, previous state, new state, evidence, claude_consulted, tx_id
    confidence      TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_entity ON files(entity);
CREATE INDEX IF NOT EXISTS idx_audit_path ON audit_log(path);

-- Layer 5 (knowledge extraction) cross-file fact dedup ledger.
-- Additive: a brand-new table, touches nothing from Layers 1-4.
CREATE TABLE IF NOT EXISTS fact_fingerprints (
    entity          TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    source_path     TEXT,
    first_seen_ts   REAL NOT NULL,
    PRIMARY KEY (entity, fingerprint)
);

-- Layer 6 (relationship engine) graph ledger. Additive: brand-new tables,
-- touches nothing from Layers 1-5. Fingerprint is (source_entity, rel_type,
-- normalized_target) per relationship_engine.relationship_fingerprint --
-- deliberately WITHOUT temporal_status, so a second source confirming the
-- same edge collapses onto one row (CONFIRM) instead of duplicating, and
-- supersession is a status flip on the existing row, not a new row.
CREATE TABLE IF NOT EXISTS relationships (
    fingerprint       TEXT PRIMARY KEY,
    source_entity     TEXT NOT NULL,
    rel_type          TEXT NOT NULL,
    target            TEXT NOT NULL,
    temporal_status   TEXT NOT NULL,
    confidence        TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | SUPERSEDED
    evidence          TEXT,     -- JSON list
    source_files      TEXT,     -- JSON list
    confirm_count     INTEGER NOT NULL DEFAULT 1,
    discovered_at     REAL NOT NULL,
    last_confirmed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_entity);
CREATE INDEX IF NOT EXISTS idx_relationships_status ON relationships(status);

-- One row per Layer 6 RELATIONSHIP_CONFLICT decision (evaluate_candidate
-- action=CONFLICT). Never auto-resolved -- Layer 6 does not silently pick
-- a winner between two current-flavored claims for the same (source,
-- rel_type) pointing at different targets.
CREATE TABLE IF NOT EXISTS relationship_conflicts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_entity  TEXT NOT NULL,
    rel_type       TEXT NOT NULL,
    packet_json    TEXT NOT NULL,   -- full build_relationship_conflict_packet() dict
    status         TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | RESOLVED
    created_at     REAL NOT NULL,
    resolved_at    REAL
);

CREATE INDEX IF NOT EXISTS idx_relconflicts_status ON relationship_conflicts(status);
CREATE INDEX IF NOT EXISTS idx_relconflicts_entity ON relationship_conflicts(source_entity);

-- Layer 7 (canonical change planner) output. One row per build_plan() call.
CREATE TABLE IF NOT EXISTS plans (
    plan_id        TEXT PRIMARY KEY,
    source_file    TEXT,
    plan_json      TEXT NOT NULL,   -- full build_plan() dict, including all proposals
    created_at     REAL NOT NULL,
    origin         TEXT NOT NULL DEFAULT 'UNKNOWN'  -- PRODUCTION / VALIDATION / UNKNOWN (never inferred as PRODUCTION); enforced at execution by canonical_executor's origin gate
);

CREATE INDEX IF NOT EXISTS idx_plans_source_file ON plans(source_file);

-- One row per distinct ChangeProposal fingerprint (== proposal_id).
-- Upserted by fingerprint: re-planning identical inputs increments
-- seen_count on the same row rather than duplicating -- idempotency
-- enforced at the storage layer, not just at fingerprint-computation time.
CREATE TABLE IF NOT EXISTS proposals (
    fingerprint     TEXT PRIMARY KEY,
    plan_id         TEXT,
    action          TEXT,
    entity          TEXT,
    canonical_path  TEXT,
    proposal_json   TEXT NOT NULL,  -- full ChangeProposal.to_dict()
    seen_count      INTEGER NOT NULL DEFAULT 1,
    first_seen_at   REAL NOT NULL,
    last_seen_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proposals_entity ON proposals(entity);
CREATE INDEX IF NOT EXISTS idx_proposals_path ON proposals(canonical_path);
CREATE INDEX IF NOT EXISTS idx_proposals_action ON proposals(action);

-- Phase 8 (canonical write executor) transaction ledger. Additive: one
-- row per attempted proposal execution (DRY_RUN or APPLY). `state` is
-- FREYA's own finer-grained status; `enforcer_state` mirrors the real
-- knowledge-enforcer's manifest state where a transaction reached one.
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id        TEXT PRIMARY KEY,
    plan_id                TEXT,
    proposal_fingerprint   TEXT NOT NULL,
    entity                 TEXT,
    canonical_path         TEXT,
    action                 TEXT,
    mode                   TEXT NOT NULL,
    state                  TEXT NOT NULL,
    enforcer_state          TEXT,
    pre_hash                TEXT,
    post_hash                TEXT,
    error                    TEXT,
    created_at               REAL NOT NULL,
    updated_at               REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transactions_fp ON transactions(proposal_fingerprint);
CREATE INDEX IF NOT EXISTS idx_transactions_state ON transactions(state);
CREATE INDEX IF NOT EXISTS idx_transactions_path ON transactions(canonical_path);

CREATE TABLE IF NOT EXISTS transaction_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  TEXT NOT NULL,
    event           TEXT NOT NULL,
    result          TEXT,
    detail_json     TEXT,
    created_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_txevents_tx ON transaction_events(transaction_id);

-- Per-canonical-path advisory lock. A row present means a transaction
-- currently holds that path; PRIMARY KEY collision is the contention
-- signal a second concurrent transaction fails on.
CREATE TABLE IF NOT EXISTS path_locks (
    canonical_path  TEXT PRIMARY KEY,
    transaction_id  TEXT NOT NULL,
    locked_at       REAL NOT NULL
);
"""


_CONFIDENCE_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}

def hash_file(path: Path, chunk_size: int = 1 << 16) -> str:
    """Stable content hash. Used for idempotency — NOT for secret detection."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class FileRecord:
    path: str
    content_hash: Optional[str] = None
    last_seen_ts: float = field(default_factory=time.time)
    last_processed_ts: Optional[float] = None
    classification: Optional[str] = None
    entity: Optional[str] = None
    confidence: Optional[str] = None
    status: str = "NEW"
    extracted_knowledge: Optional[dict] = None
    relationships: Optional[list] = None
    canonical_notes: Optional[list] = None
    escalated: bool = False
    escalation_reason: Optional[str] = None
    last_error: Optional[str] = None
    retry_count: int = 0
    processing_version: int = 1
    tx_id: Optional[str] = None
    # Layer 4 (entity resolution) fields — additive, default None/empty for
    # rows written by the Layer 1-3-only pipeline.
    entity_status: Optional[str] = None
    entity_candidates: Optional[dict] = None
    entity_evidence: Optional[list] = None
    multi_entities: Optional[list] = None
    # Layer 5 (knowledge extraction) fields — additive, default None/empty
    # for rows written by the Layer 1-4-only pipeline.
    extraction_status: Optional[str] = None      # COMPLETE | NEEDS_REVIEW | FAILED
    extraction_version: Optional[int] = None
    extracted_content_hash: Optional[str] = None  # hash as-of last successful extraction
    conflicts: Optional[list] = None
    escalation_packet: Optional[dict] = None
    # Deliberately separate from `confidence` (Layer 4's entity-resolution
    # confidence). Sharing one column was a real bug caught by regression
    # testing: Layer 5 was clobbering Layer 4's confidence value.
    extraction_confidence: Optional[str] = None


class FreyaStateStore:
    """
    Thread-safe-ish (single-writer assumption; sqlite handles WAL) idempotent
    state ledger for FREYA. One instance per running FREYA process.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate_layer4_columns()
        self._migrate_layer5_columns()
        self._migrate_layer8_origin_column()

    def _migrate_layer5_columns(self) -> None:
        """
        Additive-only migration for Layer 5 (knowledge extraction) output.
        Existing columns/data from Layers 1-4 are untouched.
        """
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(files)").fetchall()}
        new_columns = {
            "extraction_status": "TEXT",
            "extraction_version": "INTEGER",
            "extracted_content_hash": "TEXT",
            "conflicts": "TEXT",
            "escalation_packet": "TEXT",
            "extraction_confidence": "TEXT",
        }
        for col, coltype in new_columns.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE files ADD COLUMN {col} {coltype}")
        self._conn.commit()

    def _migrate_layer4_columns(self) -> None:
        """
        Additive-only migration for Layer 4 (entity resolution) output.
        Existing columns/data from Layers 1-3 are untouched; this only adds
        new nullable columns if they don't already exist, so old DBs (and
        the existing test suite) keep working unchanged.
        """
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(files)").fetchall()}
        new_columns = {
            "entity_status": "TEXT",       # RESOLVED / AMBIGUOUS / UNKNOWN / MULTI_ENTITY
            "entity_candidates": "TEXT",   # JSON: {entity_name: score, ...}
            "entity_evidence": "TEXT",     # JSON: [{signal, detail, entity, weight}, ...]
            "multi_entities": "TEXT",      # JSON list, only populated when entity_status == MULTI_ENTITY
        }
        for col, coltype in new_columns.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE files ADD COLUMN {col} {coltype}")
        self._conn.commit()

    def _migrate_layer8_origin_column(self) -> None:
        """
        Additive-only migration for the Phase 8 plan-origin safety gate.
        Adds `plans.origin` if missing, defaulting existing rows to
        'UNKNOWN' -- never 'PRODUCTION' -- so plans that predate this
        column are never silently treated as production-authorized.
        Existing plans/proposals rows and all other tables are untouched.
        """
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(plans)").fetchall()}
        if "origin" not in existing:
            self._conn.execute("ALTER TABLE plans ADD COLUMN origin TEXT NOT NULL DEFAULT 'UNKNOWN'")
        self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # ---- core idempotency primitive ----------------------------------

    def get(self, path: str) -> Optional[FileRecord]:
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        for k in ("extracted_knowledge", "relationships", "canonical_notes",
                  "entity_candidates", "entity_evidence", "multi_entities",
                  "conflicts", "escalation_packet"):
            default = None if k in ("extracted_knowledge", "entity_candidates", "escalation_packet") else []
            d[k] = json.loads(d[k]) if d.get(k) else default
        d["escalated"] = bool(d["escalated"])
        # tolerate rows from a pre-migration schema (defensive; migration is
        # idempotent and additive so this shouldn't normally trigger)
        return FileRecord(**{k: v for k, v in d.items() if k in FileRecord.__dataclass_fields__})

    def touch_seen(self, path: str, content_hash: str) -> tuple[FileRecord, bool]:
        """
        Called by the watcher on every event. Returns (record, changed).
        changed=False means this exact content was already fully processed —
        the caller should skip the pipeline entirely (idempotency guarantee).
        """
        existing = self.get(path)
        now = time.time()
        if existing is None:
            rec = FileRecord(path=path, content_hash=content_hash, last_seen_ts=now, status="NEW")
            self._upsert(rec)
            return rec, True

        if existing.content_hash == content_hash and existing.status in ("VERIFIED", "IGNORED"):
            # Same bytes, already terminally handled. Do nothing.
            existing.last_seen_ts = now
            self._upsert(existing, fields=("last_seen_ts",))
            return existing, False

        # Content changed (or was left mid-pipeline) — needs (re)processing.
        existing.content_hash = content_hash
        existing.last_seen_ts = now
        existing.status = "NEW"
        existing.last_error = None
        self._upsert(existing)
        return existing, True

    def mark_unavailable(self, path: str) -> None:
        """File deleted from disk. Per spec: never auto-delete canonical knowledge."""
        rec = self.get(path)
        if rec is None:
            return
        rec.status = "UNAVAILABLE"
        rec.last_seen_ts = time.time()
        self._upsert(rec)
        self.audit(path=path, entity=rec.entity, event="source_unavailable",
                   detail={"note": "source deleted; canonical knowledge preserved, queued for optional review"})

    def set_status(self, path: str, status: str, **updates) -> None:
        if status not in VALID_STATES:
            raise ValueError(f"invalid status: {status}")
        rec = self.get(path)
        if rec is None:
            raise KeyError(path)
        rec.status = status
        for k, v in updates.items():
            setattr(rec, k, v)
        if status == "VERIFIED":
            rec.last_processed_ts = time.time()
        self._upsert(rec)

    def set_entity_resolution(
        self,
        path: str,
        *,
        entity_status: str,
        entity: Optional[str] = None,
        confidence: Optional[str] = None,
        candidates: Optional[dict] = None,
        evidence: Optional[list] = None,
        multi_entities: Optional[list] = None,
    ) -> None:
        """Layer 4 writes its result through this single, explicit method
        rather than poking columns directly, so the audit trail and the
        record stay consistent."""
        if entity_status not in {"RESOLVED", "AMBIGUOUS", "UNKNOWN", "MULTI_ENTITY"}:
            raise ValueError(f"invalid entity_status: {entity_status}")
        rec = self.get(path)
        if rec is None:
            raise KeyError(path)
        rec.entity = entity
        rec.confidence = confidence
        rec.entity_status = entity_status
        rec.entity_candidates = candidates or {}
        rec.entity_evidence = evidence or []
        rec.multi_entities = multi_entities or []
        self._upsert(rec)
        self.audit(
            path, entity, "entity_resolved",
            {"status": entity_status, "candidates": candidates or {}, "evidence": evidence or [],
             "multi_entities": multi_entities or []},
            confidence=confidence,
        )

    def set_extraction_result(
        self,
        path: str,
        *,
        extraction_status: str,
        extracted_knowledge: dict,
        relationships: list,
        conflicts: list,
        escalation_packet: Optional[dict],
        confidence: str,
        extracted_content_hash: str,
        extraction_version: int = 1,
    ) -> None:
        """Layer 5 writes its result through this single, explicit method,
        same pattern as set_entity_resolution — keeps the audit trail and
        the record consistent, and is the only place these columns are set.
        Writes to `extraction_confidence`, NOT `confidence` (that column
        belongs to Layer 4's entity-resolution confidence and must not be
        clobbered by Layer 5)."""
        if extraction_status not in {"COMPLETE", "NEEDS_REVIEW", "FAILED"}:
            raise ValueError(f"invalid extraction_status: {extraction_status}")
        rec = self.get(path)
        if rec is None:
            raise KeyError(path)
        rec.extraction_status = extraction_status
        rec.extracted_knowledge = extracted_knowledge
        rec.relationships = relationships
        rec.conflicts = conflicts
        rec.escalation_packet = escalation_packet
        rec.extraction_confidence = confidence
        rec.extracted_content_hash = extracted_content_hash
        rec.extraction_version = extraction_version
        rec.escalated = bool(escalation_packet)
        if escalation_packet:
            rec.escalation_reason = escalation_packet.get("reason")
        self._upsert(rec)
        self.audit(
            path, rec.entity, "knowledge_extracted",
            {
                "status": extraction_status,
                "fact_count": len(extracted_knowledge.get("facts", [])) if extracted_knowledge else 0,
                "conflict_count": len(conflicts or []),
                "escalated": bool(escalation_packet),
            },
            confidence=confidence,
        )

    def known_fingerprints_for_entity(self, entity: str) -> set[str]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT fingerprint FROM fact_fingerprints WHERE entity = ?", (entity,)
            ).fetchall()
        return {r["fingerprint"] for r in rows}

    def record_fingerprints(self, entity: str, fingerprints: list[str], source_path: str) -> None:
        now = time.time()
        with self._cursor() as cur:
            for fp in fingerprints:
                cur.execute(
                    "INSERT OR IGNORE INTO fact_fingerprints (entity, fingerprint, source_path, first_seen_ts) "
                    "VALUES (?,?,?,?)",
                    (entity, fp, source_path, now),
                )

    def all_by_status(self, status: str) -> list[FileRecord]:
        with self._cursor() as cur:
            rows = cur.execute("SELECT path FROM files WHERE status = ?", (status,)).fetchall()
        return [self.get(r["path"]) for r in rows]

    def known_paths(self) -> set[str]:
        with self._cursor() as cur:
            rows = cur.execute("SELECT path FROM files WHERE status != 'UNAVAILABLE'").fetchall()
        return {r["path"] for r in rows}

    # ---- audit trail ---------------------------------------------------

    def audit(self, path: Optional[str], entity: Optional[str], event: str,
              detail: Optional[dict] = None, confidence: Optional[str] = None) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (ts, path, entity, event, detail, confidence) VALUES (?,?,?,?,?,?)",
                (time.time(), path, entity, event, json.dumps(detail or {}), confidence),
            )

    def why(self, path: str) -> list[dict]:
        """Answers: 'Why does FREYA believe this?' for a given file."""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM audit_log WHERE path = ? ORDER BY ts ASC", (path,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            out.append(d)
        return out

    # ---- internal --------------------------------------------------------

    def _upsert(self, rec: FileRecord, fields: Optional[tuple] = None) -> None:
        payload = asdict(rec)
        payload["extracted_knowledge"] = json.dumps(payload["extracted_knowledge"]) if payload["extracted_knowledge"] is not None else None
        payload["relationships"] = json.dumps(payload["relationships"] or [])
        payload["canonical_notes"] = json.dumps(payload["canonical_notes"] or [])
        payload["entity_candidates"] = json.dumps(payload["entity_candidates"]) if payload["entity_candidates"] is not None else None
        payload["entity_evidence"] = json.dumps(payload["entity_evidence"] or [])
        payload["multi_entities"] = json.dumps(payload["multi_entities"] or [])
        payload["conflicts"] = json.dumps(payload["conflicts"] or [])
        payload["escalation_packet"] = json.dumps(payload["escalation_packet"]) if payload["escalation_packet"] is not None else None
        payload["escalated"] = int(payload["escalated"])
        cols = list(payload.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "path")
        with self._cursor() as cur:
            cur.execute(
                f"INSERT INTO files ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(path) DO UPDATE SET {updates}",
                [payload[c] for c in cols],
            )

    # ---- Layer 6: relationship ledger -----------------------------------

    @staticmethod
    def _relationship_row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["evidence"] = json.loads(d["evidence"]) if d.get("evidence") else []
        d["source_files"] = json.loads(d["source_files"]) if d.get("source_files") else []
        return d

    def get_relationship_by_fingerprint(self, fingerprint: str) -> Optional[dict]:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM relationships WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return self._relationship_row_to_dict(row) if row else None

    def get_active_relationships_for_source(self, source_entity: str) -> list[dict]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM relationships WHERE source_entity = ? AND status = 'ACTIVE'",
                (source_entity,),
            ).fetchall()
        return [self._relationship_row_to_dict(r) for r in rows]

    def get_relationships_for_entities(self, entities: list[str]) -> list[dict]:
        """ACTIVE + SUPERSEDED rows for the given entities -- feeds Layer 7's
        plan_relationship, which handles both statuses itself."""
        if not entities:
            return []
        placeholders = ",".join("?" for _ in entities)
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT * FROM relationships WHERE source_entity IN ({placeholders})",
                tuple(entities),
            ).fetchall()
        return [self._relationship_row_to_dict(r) for r in rows]

    def upsert_relationship(self, candidate, status: str, source_path: Optional[str] = None) -> dict:
        """ADD or SUPERSEDE_AND_ADD path: insert a new relationship row (or
        replace one at the same fingerprint, defensively). `candidate` is a
        relationship_engine.RelationshipCandidate."""
        from .relationship_engine import relationship_fingerprint
        fp = relationship_fingerprint(candidate.source, candidate.rel_type, candidate.target)
        now = time.time()
        source_files = list(candidate.source_files or [])
        if source_path and source_path not in source_files:
            source_files.append(source_path)
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO relationships (fingerprint, source_entity, rel_type, target, "
                "temporal_status, confidence, status, evidence, source_files, confirm_count, "
                "discovered_at, last_confirmed_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET "
                "temporal_status=excluded.temporal_status, confidence=excluded.confidence, "
                "status=excluded.status, evidence=excluded.evidence, "
                "source_files=excluded.source_files, last_confirmed_at=excluded.last_confirmed_at",
                (fp, candidate.source, candidate.rel_type, candidate.target,
                 candidate.temporal_status, candidate.confidence, status,
                 json.dumps(candidate.evidence or []), json.dumps(source_files), now, now),
            )
        self.audit(source_path, candidate.source, "relationship_recorded",
                   {"fingerprint": fp, "rel_type": candidate.rel_type, "target": candidate.target,
                    "status": status})
        return self.get_relationship_by_fingerprint(fp)

    def confirm_relationship(self, fingerprint: str, candidate, source_path: Optional[str] = None) -> dict:
        """CONFIRM path: exact-fingerprint match already exists -- merge
        evidence/source_files onto it, take the stronger confidence,
        increment confirm_count. Never a new row."""
        existing = self.get_relationship_by_fingerprint(fingerprint)
        if existing is None:
            raise KeyError(fingerprint)
        merged_evidence = existing["evidence"] + list(candidate.evidence or [])
        merged_sources = list(existing["source_files"])
        candidate_sources = list(candidate.source_files or [])
        if source_path:
            candidate_sources.append(source_path)
        for s in candidate_sources:
            if s and s not in merged_sources:
                merged_sources.append(s)
        stronger_confidence = existing["confidence"]
        if _CONFIDENCE_RANK.get(candidate.confidence, 0) > _CONFIDENCE_RANK.get(stronger_confidence, 0):
            stronger_confidence = candidate.confidence
        now = time.time()
        with self._cursor() as cur:
            cur.execute(
                "UPDATE relationships SET evidence=?, source_files=?, confidence=?, "
                "confirm_count=confirm_count+1, last_confirmed_at=? WHERE fingerprint=?",
                (json.dumps(merged_evidence), json.dumps(merged_sources), stronger_confidence,
                 now, fingerprint),
            )
        self.audit(source_path, existing["source_entity"], "relationship_confirmed",
                   {"fingerprint": fingerprint})
        return self.get_relationship_by_fingerprint(fingerprint)

    def mark_relationship_superseded(self, fingerprint: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE relationships SET status='SUPERSEDED' WHERE fingerprint=?", (fingerprint,))
        self.audit(None, None, "relationship_superseded", {"fingerprint": fingerprint})

    def record_relationship_conflict(self, packet: dict) -> None:
        """Persists a relationship_engine.build_relationship_conflict_packet()
        dict. Never auto-resolved by this method -- opening a conflict is
        the terminal action for that candidate this pass."""
        now = time.time()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO relationship_conflicts (source_entity, rel_type, packet_json, status, created_at) "
                "VALUES (?,?,?,?,?)",
                (packet["source_entity"], packet["relationship_type"], json.dumps(packet), "OPEN", now),
            )
        self.audit(None, packet["source_entity"], "relationship_conflict_opened",
                   {"reason": packet.get("reason")})

    def open_relationship_conflicts(self) -> list[dict]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT packet_json FROM relationship_conflicts WHERE status = 'OPEN'"
            ).fetchall()
        return [json.loads(r["packet_json"]) for r in rows]

    # ---- Layer 7: plans / proposals --------------------------------------

    def record_plan(self, plan: dict, source_file: Optional[str] = None) -> None:
        now = time.time()
        origin = plan.get("origin")
        if origin not in ("PRODUCTION", "VALIDATION"):
            origin = "UNKNOWN"  # never inferred as PRODUCTION -- see canonical_executor origin gate
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO plans (plan_id, source_file, plan_json, created_at, origin) VALUES (?,?,?,?,?) "
                "ON CONFLICT(plan_id) DO UPDATE SET plan_json=excluded.plan_json, origin=excluded.origin",
                (plan["plan_id"], source_file, json.dumps(plan), now, origin),
            )
        self.audit(source_file, None, "plan_recorded",
                   {"plan_id": plan["plan_id"], "proposal_count": len(plan.get("proposals", [])), "origin": origin})

    def get_plan(self, plan_id: str) -> Optional[dict]:
        with self._cursor() as cur:
            row = cur.execute("SELECT plan_json, origin FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if not row:
            return None
        d = json.loads(row["plan_json"])
        d["origin"] = row["origin"]  # DB column is authoritative, overrides whatever plan_json may say
        return d

    def get_plan_origin(self, plan_id: str) -> Optional[str]:
        """Authoritative origin lookup used by the canonical_executor
        origin gate. Returns None (never 'PRODUCTION') if the plan
        doesn't exist."""
        with self._cursor() as cur:
            row = cur.execute("SELECT origin FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        return row["origin"] if row else None

    def get_proposal_plan_id(self, fingerprint: str) -> Optional[str]:
        """Authoritative FK lookup: which persisted plan does this
        proposal fingerprint belong to, from the `proposals` table column
        (not the JSON blob). Returns None if the fingerprint was never
        persisted."""
        with self._cursor() as cur:
            row = cur.execute("SELECT plan_id FROM proposals WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return row["plan_id"] if row else None

    def record_proposals(self, proposals: list[dict], plan_id: Optional[str] = None) -> None:
        """Upsert-by-fingerprint. Re-planning identical inputs increments
        seen_count on the same row instead of duplicating -- idempotency at
        the storage layer, not just at fingerprint-computation time."""
        now = time.time()
        with self._cursor() as cur:
            for p in proposals:
                fp = p["fingerprint"]
                existing = cur.execute(
                    "SELECT seen_count FROM proposals WHERE fingerprint = ?", (fp,)
                ).fetchone()
                if existing:
                    cur.execute(
                        "UPDATE proposals SET seen_count = seen_count + 1, last_seen_at = ?, "
                        "proposal_json = ? WHERE fingerprint = ?",
                        (now, json.dumps(p), fp),
                    )
                else:
                    cur.execute(
                        "INSERT INTO proposals (fingerprint, plan_id, action, entity, canonical_path, "
                        "proposal_json, seen_count, first_seen_at, last_seen_at) "
                        "VALUES (?,?,?,?,?,?,1,?,?)",
                        (fp, plan_id or p.get("proposal_id"), p.get("action"), p.get("entity"),
                         p.get("canonical_path"), json.dumps(p), now, now),
                    )

    def get_proposal(self, fingerprint: str) -> Optional[dict]:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT proposal_json, seen_count FROM proposals WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        if not row:
            return None
        d = json.loads(row["proposal_json"])
        d["_seen_count"] = row["seen_count"]
        return d

    def list_proposals_by_action(self, action: str) -> list[dict]:
        with self._cursor() as cur:
            rows = cur.execute("SELECT proposal_json FROM proposals WHERE action = ?", (action,)).fetchall()
        return [json.loads(r["proposal_json"]) for r in rows]

    # ---- Phase 8: transaction ledger, events, per-path locks --------------

    def record_transaction(self, transaction_id: str, plan_id: Optional[str],
                            proposal_fingerprint: str, entity: Optional[str],
                            canonical_path: Optional[str], action: Optional[str],
                            mode: str, state: str, enforcer_state: Optional[str] = None,
                            pre_hash: Optional[str] = None) -> None:
        now = time.time()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO transactions (transaction_id, plan_id, proposal_fingerprint, entity, "
                "canonical_path, action, mode, state, enforcer_state, pre_hash, post_hash, error, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?) "
                "ON CONFLICT(transaction_id) DO UPDATE SET state=excluded.state, "
                "enforcer_state=excluded.enforcer_state, updated_at=excluded.updated_at",
                (transaction_id, plan_id, proposal_fingerprint, entity, canonical_path, action,
                 mode, state, enforcer_state, pre_hash, now, now),
            )

    def update_transaction(self, transaction_id: str, *, state: Optional[str] = None,
                            enforcer_state: Optional[str] = None, pre_hash: Optional[str] = None,
                            post_hash: Optional[str] = None, error: Optional[str] = None) -> None:
        now = time.time()
        existing = self.get_transaction(transaction_id)
        if existing is None:
            raise KeyError(transaction_id)
        with self._cursor() as cur:
            cur.execute(
                "UPDATE transactions SET "
                "state = COALESCE(?, state), "
                "enforcer_state = COALESCE(?, enforcer_state), "
                "pre_hash = COALESCE(?, pre_hash), "
                "post_hash = COALESCE(?, post_hash), "
                "error = COALESCE(?, error), "
                "updated_at = ? WHERE transaction_id = ?",
                (state, enforcer_state, pre_hash, post_hash, error, now, transaction_id),
            )

    def get_transaction(self, transaction_id: str) -> Optional[dict]:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
            ).fetchone()
        return dict(row) if row else None

    _TERMINAL_TRANSACTION_STATES = {
        "COMMITTED", "ALREADY_APPLIED", "NO_CHANGE_NOOP", "NOT_APPLICABLE",
        "BLOCKED", "VALIDATION_FAILED", "STALE", "ROLLED_BACK", "ROLLBACK_FAILED",
        "DRY_RUN_VALIDATED", "RECOVERY_REQUIRED",
    }

    def list_incomplete_transactions(self) -> list[dict]:
        """Transactions that never reached a terminal FREYA-level state --
        the restart-safety inspection point. Never blindly replayed; the
        caller (executor) must inspect current canonical hash before
        deciding anything."""
        placeholders = ",".join("?" for _ in self._TERMINAL_TRANSACTION_STATES)
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT * FROM transactions WHERE state NOT IN ({placeholders})",
                tuple(self._TERMINAL_TRANSACTION_STATES),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_committed_transaction_for_fingerprint(self, fingerprint: str) -> Optional[dict]:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM transactions WHERE proposal_fingerprint = ? AND state = 'COMMITTED' "
                "ORDER BY created_at DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
        return dict(row) if row else None

    def record_transaction_event(self, transaction_id: str, event: str,
                                  result: Optional[str] = None, detail: Optional[dict] = None) -> None:
        now = time.time()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO transaction_events (transaction_id, event, result, detail_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (transaction_id, event, result, json.dumps(detail) if detail is not None else None, now),
            )

    def get_transaction_events(self, transaction_id: str) -> list[dict]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM transaction_events WHERE transaction_id = ? ORDER BY id ASC",
                (transaction_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail_json"]) if d.get("detail_json") else None
            out.append(d)
        return out

    def acquire_path_lock(self, canonical_path: str, transaction_id: str) -> bool:
        """Per-path concurrency guard. INSERT with a PRIMARY KEY collision
        is the lock-contention signal -- a second transaction targeting the
        same canonical_path fails to acquire and must not proceed."""
        now = time.time()
        try:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO path_locks (canonical_path, transaction_id, locked_at) VALUES (?,?,?)",
                    (canonical_path, transaction_id, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_path_lock(self, canonical_path: str, transaction_id: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM path_locks WHERE canonical_path = ? AND transaction_id = ?",
                (canonical_path, transaction_id),
            )

    def get_path_lock(self, canonical_path: str) -> Optional[dict]:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM path_locks WHERE canonical_path = ?", (canonical_path,)
            ).fetchone()
        return dict(row) if row else None

    def health(self) -> dict:
        with self._cursor() as cur:
            total = cur.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
            by_status = {r["status"]: r["c"] for r in cur.execute(
                "SELECT status, COUNT(*) c FROM files GROUP BY status")}
            failed = cur.execute("SELECT COUNT(*) c FROM files WHERE status='FAILED'").fetchone()["c"]
            pending_escalation = cur.execute(
                "SELECT COUNT(*) c FROM files WHERE escalated=1 AND status='NEEDS_REVIEW'").fetchone()["c"]
            last_write = cur.execute(
                "SELECT MAX(ts) t FROM audit_log WHERE event='canonical_write'").fetchone()["t"]
        return {
            "total_files_tracked": total,
            "by_status": by_status,
            "failed_count": failed,
            "pending_escalations": pending_escalation,
            "last_canonical_write_ts": last_write,
        }
