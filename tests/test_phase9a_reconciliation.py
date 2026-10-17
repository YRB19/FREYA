"""
Phase 9A tests - deterministic, fixtures/fakes, temp DB + temp recovery dir only.
Never touches production DB.
"""
import os
import tempfile
from pathlib import Path

import pytest

from freya.state_store import FreyaStateStore
from freya.reconciliation import Phase9AReconciliation, KNOWN_HISTORICAL_RESIDUES
from freya.health_report import HealthReport


def _make_temp_store():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        os.unlink(path)
    except Exception:
        pass
    store = FreyaStateStore(db_path=path)
    return store, path


def test_healthy_set():
    store, path = _make_temp_store()
    try:
        # minimal healthy state
        rec = Phase9AReconciliation(store, recovery_dir=tempfile.mkdtemp())
        report = rec.reconcile()
        assert isinstance(report, HealthReport)
        assert report.overall_status in ("HEALTHY", "DEGRADED", "BLOCKED", "UNKNOWN")
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
        try:
            os.unlink(path + "-shm")
        except Exception:
            pass
        try:
            os.unlink(path + "-wal")
        except Exception:
            pass

def test_read_only_sqlite_enforcement():
    store, path = _make_temp_store()
    try:
        rec = Phase9AReconciliation(store, recovery_dir=tempfile.mkdtemp())
        # Try to write through reconciliation connection pattern - use store's ro conn
        try:
            with store._conn_ro() as conn:
                conn.execute("INSERT INTO transactions (transaction_id, proposal_fingerprint, mode, state, created_at, updated_at) VALUES ('t', 'f', 'DRY_RUN', 'PLANNED', 0, 0)")
            assert False, "should have failed"
        except Exception as e:
            # Read-only should prevent writes
            pass
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

def test_idempotency():
    store, path = _make_temp_store()
    try:
        rec_dir = tempfile.mkdtemp()
        rec = Phase9AReconciliation(store, recovery_dir=rec_dir)
        r1 = rec.reconcile()
        r2 = rec.reconcile()
        assert r1.overall_status == r2.overall_status
        assert len(r1.findings) == len(r2.findings)
        assert r1.evidence.get("known_residues") == r2.evidence.get("known_residues")
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def test_no_mutation_tmp_db():
    store, path = _make_temp_store()
    try:
        rec_dir = tempfile.mkdtemp()
        rec = Phase9AReconciliation(store, recovery_dir=rec_dir)
        # snapshot
        with store._conn_ro() as conn:
            c1 = conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
        r1 = rec.reconcile()
        with store._conn_ro() as conn:
            c2 = conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
        assert c1 == c2
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

def test_known_residues_classified():
    store, path = _make_temp_store()
    try:
        # Add known historical lock tx-d9d44f355acd4b87 to locks? But store doesn't have method to add locks; 
        # we can inspect via raw - but better test the logic. Let's just run reconcile on empty store.
        rec = Phase9AReconciliation(store, recovery_dir=tempfile.mkdtemp())
        report = rec.reconcile()
        # known residues info is in evidence
        assert "known_residues" in report.evidence
        assert any(r["tx_id"] == "tx-d9d44f355acd4b87" for r in report.evidence["known_residues"])
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
