"""Append-only, evidence-grounded Knowledge Vaults for Build-up."""

from .models import (
    KnowledgeIngestResult,
    KnowledgeLintReport,
    KnowledgeQueryHit,
    KnowledgeStatus,
    KnowledgeVaultSummary,
)
from .service import (
    add_research_to_vault,
    auto_ingest_research_run,
    bind_job_to_vault,
    compile_vault,
    format_ingest_result,
    format_lint_report,
    format_query_results,
    format_vault_summary,
    lint_vault,
    query_vault,
    reject_claim,
    review_vault,
    rollback_proposal,
    verify_claim,
    vault_summary,
)

__all__ = [
    "KnowledgeIngestResult",
    "KnowledgeLintReport",
    "KnowledgeQueryHit",
    "KnowledgeStatus",
    "KnowledgeVaultSummary",
    "add_research_to_vault",
    "auto_ingest_research_run",
    "bind_job_to_vault",
    "compile_vault",
    "format_ingest_result",
    "format_lint_report",
    "format_query_results",
    "format_vault_summary",
    "lint_vault",
    "query_vault",
    "reject_claim",
    "review_vault",
    "rollback_proposal",
    "verify_claim",
    "vault_summary",
]
