"""
freya/lock_reconciliation.py -- Phase 9A.3 Path lock reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class LockInfo:
    canonical_path: str
    transaction_id: str
    locked_at: float


@dataclass
class LockFinding:
    finding_id: str
    severity: str
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)
