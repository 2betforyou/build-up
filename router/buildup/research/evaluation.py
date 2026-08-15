"""Offline quality gates for completed or failed Build-up research runs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from .citations import audit_report, passage_sha256, verify_exact_passage
from .models import ResearchState, RunStatus
from .store import checkpoint_invariant_failures, verify_manifest


REQUIRED_ARTIFACTS = (
    "state.json",
    "metadata.json",
    "00-sources.json",
    "01-contract.json",
    "02-plan.json",
    "03-sources.jsonl",
    "04-evidence.jsonl",
    "05-claims.jsonl",
    "05-gaps.json",
    "06-report.md",
    "07-study-guide.md",
    "08-citation-audit.json",
    "09-claim-audit.json",
    "events.jsonl",
    "manifest.json",
)


def evaluate_run(run_dir: Path) -> Dict[str, Any]:
    """Recompute deterministic gates without a model or network connection."""
    run_dir = run_dir.expanduser().resolve()
    state = _load_state(run_dir)
    issues: List[str] = []
    checkpoint_failures = checkpoint_invariant_failures(state)
    if checkpoint_failures:
        issues.append("checkpoint invariant validation failed")
    missing = [name for name in REQUIRED_ARTIFACTS if not (run_dir / name).is_file()]
    if missing:
        issues.append(f"missing artifacts: {', '.join(missing)}")
    manifest_failures = verify_manifest(run_dir, expected_run_id=state.run_id)
    if manifest_failures:
        issues.append("artifact manifest verification failed")

    source_hash_failures = [
        source.id
        for source in state.sources
        if source.content_sha256 != hashlib.sha256(source.content.encode("utf-8")).hexdigest()
    ]
    if source_hash_failures:
        issues.append("source content hashes do not match")

    passage_failures: List[str] = []
    source_by_id = {item.id: item for item in state.sources}
    for evidence in state.evidence:
        source = source_by_id.get(evidence.source_id)
        if not source:
            passage_failures.append(evidence.id)
            continue
        exact = verify_exact_passage(source.content, evidence.passage)
        if (
            not exact.valid
            or not evidence.verified_exact
            or evidence.passage_sha256 != passage_sha256(evidence.passage)
        ):
            passage_failures.append(evidence.id)
    if passage_failures:
        issues.append("evidence exact-passage validation failed")

    report = (run_dir / "06-report.md").read_text(encoding="utf-8") if (run_dir / "06-report.md").is_file() else ""
    citation = audit_report(
        report,
        state.evidence,
        state.sources,
        state.claims,
        used_claim_ids=state.used_claim_ids,
    )
    if not citation.get("passed"):
        issues.append("recomputed citation audit failed")
    if not state.claim_audit.get("passed"):
        issues.append("independent claim audit did not pass")
    if state.status != RunStatus.COMPLETED.value:
        issues.append(f"run status is {state.status}, not completed")

    evidence_exactness = (
        round((len(state.evidence) - len(passage_failures)) / len(state.evidence), 3)
        if state.evidence else 0.0
    )
    critical = [item for item in state.claims if item.critical]
    unsupported_critical_ids = set(citation.get("unsupported_critical_claim_ids", []))
    supported_critical = [
        item
        for item in critical
        if item.evidence_ids and item.id not in unsupported_critical_ids
    ]
    critical_support = round(len(supported_critical) / len(critical), 3) if critical else 1.0
    passed = not issues
    return {
        "schema_version": 1,
        "run_id": state.run_id,
        "evaluated_status": state.status,
        "passed": passed,
        "issues": issues,
        "metrics": {
            "contract_coverage": state.coverage,
            "target_coverage": state.target_coverage,
            "target_coverage_met": state.coverage >= state.target_coverage,
            "max_results_per_search": state.max_results_per_search,
            "citation_placement_coverage": citation.get("citation_placement_coverage", 0.0),
            "citation_count": citation.get("citation_count", 0),
            "source_utilization": citation.get("source_utilization", 0.0),
            "evidence_exactness": evidence_exactness,
            "critical_claim_support": critical_support,
            "sources": len(state.sources),
            "evidence": len(state.evidence),
            "claims": len(state.claims),
            "searches": state.budget.searches_used,
            "model_calls": state.budget.model_calls_used,
            "runtime_seconds": round(state.budget.runtime_seconds_used, 3),
        },
        "failures": {
            "missing_artifacts": missing,
            "source_hash_ids": source_hash_failures,
            "evidence_ids": passage_failures,
            "manifest_files": manifest_failures,
            "checkpoint_invariants": checkpoint_failures,
            "citation": citation,
        },
    }


def format_evaluation(result: Dict[str, Any]) -> str:
    marker = "PASS" if result.get("passed") else "FAIL"
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    lines = [
        f"{marker} · {result.get('run_id', '')}",
        f"coverage {metrics.get('contract_coverage', 0):.3f}/"
        f"{metrics.get('target_coverage', 0):.3f} · "
        f"citation placement {metrics.get('citation_placement_coverage', 0):.3f} · "
        f"evidence exactness {metrics.get('evidence_exactness', 0):.3f}",
        f"sources {metrics.get('sources', 0)} · evidence {metrics.get('evidence', 0)} · "
        f"claims {metrics.get('claims', 0)} · model calls {metrics.get('model_calls', 0)}",
    ]
    issues = result.get("issues") if isinstance(result.get("issues"), list) else []
    lines.extend(f"- {item}" for item in issues)
    return "\n".join(lines)


def _load_state(run_dir: Path) -> ResearchState:
    path = run_dir / "state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"research state가 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"research state JSON이 손상되었습니다: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"research state 형식이 잘못되었습니다: {path}")
    return ResearchState.from_dict(value)
