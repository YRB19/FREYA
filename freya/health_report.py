"""
freya/health_report.py -- Phase 9A: Health report model.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class Finding:
    id: str
    component: str
    severity: str  # INFO|WARN|ERROR|BLOCKED
    message: str
    evidence: Optional[Dict[str, Any]] = None
    provenance: Optional[Dict[str, Any]] = None


@dataclass
class ComponentStatus:
    name: str
    status: str  # OK|DEGRADED|BLOCKED|UNKNOWN
    severity: str = "INFO"
    findings: List[Finding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    blocking: List[str] = field(default_factory=list)


@dataclass
class HealthReport:
    overall_status: str  # HEALTHY|DEGRADED|BLOCKED|UNKNOWN
    timestamp: float
    components: Dict[str, ComponentStatus] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    blocking_issues: List[str] = field(default_factory=list)
    recommended_action: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "timestamp": self.timestamp,
            "components": {k: {
                "name": v.name,
                "status": v.status,
                "severity": v.severity,
                "findings": [asdict(f) for f in v.findings],
                "warnings": v.warnings,
                "blocking": v.blocking,
            } for k, v in self.components.items()},
            "findings": [asdict(f) for f in self.findings],
            "warnings": self.warnings,
            "blocking_issues": self.blocking_issues,
            "recommended_action": self.recommended_action,
            "evidence": self.evidence,
        }
