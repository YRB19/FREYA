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
"""


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
