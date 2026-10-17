"""
freya/transaction_reconciliation.py -- Phase 9A.2 Transaction reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class TxEvent:
    event: str
    result: Optional[str] = None
    detail_json: Optional[str] = None
    created_at: float = 0.0


@dataclass
class TransactionInfo:
    transaction_id: str
    plan_id: Optional[str]
    proposal_fingerprint: str
    entity: Optional[str]
    canonical_path: Optional[str]
    action: Optional[str]
    mode: str
    state: str
    enforcer_state: Optional[str]
    pre_hash: Optional[str]
    post_hash: Optional[str]
    error: Optional[str]
    created_at: float
    updated_at: float
    events: List[TxEvent] = field(default_factory=list)


@dataclass
class TxFinding:
    finding_id: str
    severity: str  # INFO|WARN|ERROR|BLOCKED
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)
