"""
freya/recovery_reconciliation.py -- Phase 9A.4 Recovery artifact reconciliation.
STEP 0: Document what an artifact means, authoritative fields, tx-id mapping, pre-state hashes, terminal states.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pathlib import Path


@dataclass
class RecoveryArtifact:
    path: Path
    name: str
    tx_id: Optional[str] = None
    info: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RecoveryFinding:
    finding_id: str
    severity: str
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)
