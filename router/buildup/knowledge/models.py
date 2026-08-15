"""Public domain models for the Build-up Knowledge Vault."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List


class KnowledgeStatus(str, Enum):
    DRAFT = "draft"
    SUPPORTED = "supported"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    STALE = "stale"
    REJECTED = "rejected"


@dataclass(frozen=True)
class KnowledgeIngestResult:
    vault_id: str
    vault_dir: Path
    proposal_id: str
    run_id: str
    claims_added: int
    claims_updated: int
    conflicts: int
    stale_claims: int
    invalid: int = 0
    duplicate: bool = False


@dataclass(frozen=True)
class KnowledgeQueryHit:
    claim_id: str
    text: str
    status: str
    score: float
    citations: List[Dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class KnowledgeLintReport:
    vault_id: str
    passed: bool
    errors: List[str]
    warnings: List[str]
    stats: Dict[str, int]
    repaired_stale: int = 0


@dataclass(frozen=True)
class KnowledgeVaultSummary:
    vault_id: str
    vault_dir: Path
    jobs: List[str]
    claims_by_status: Dict[str, int]
    source_versions: int
    proposals: int
    contradictions: int
    open_questions: int
    last_event_at: str
    settings: Dict[str, Any]
