"""Evidence-only ingestion, materialization, query, lint, and review services."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from rich.markup import escape as rich_escape

from buildup.config import BuildupConfig
from buildup.paths import ensure_within, slugify, validate_job_id
from buildup.research.citations import passage_sha256, verify_exact_passage
from buildup.research.models import ResearchState, now_iso, stable_id
from buildup.research.reader import is_public_web_url, normalize_url
from buildup.research.store import ResearchStore, verify_manifest
from buildup.sandbox import require_knowledge_write

from .models import (
    KnowledgeIngestResult,
    KnowledgeLintReport,
    KnowledgeQueryHit,
    KnowledgeStatus,
    KnowledgeVaultSummary,
)
from .store import (
    KNOWLEDGE_SCHEMA_VERSION,
    MAX_LEDGER_LINE_BYTES,
    MAX_LEDGER_ROWS,
    PAGE_CATEGORIES,
    SCHEMA_YAML,
    KnowledgeLease,
    KnowledgeStoreError,
    KnowledgeVaultStore,
    validate_vault_id,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CLAIM_ID_RE = re.compile(r"^C[0-9A-F]{14}$")
_SOURCE_VERSION_RE = re.compile(r"^KS[0-9A-F]{18}$")
_PROPOSAL_ID_RE = re.compile(r"^KP[0-9A-F]{20}$")
_STATUS_VALUES = {item.value for item in KnowledgeStatus}


def _default_vault_id(job_id: str) -> str:
    clean = slugify(validate_job_id(job_id))[:58]
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]
    return f"job-{clean}-{digest}"


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if line_number > MAX_LEDGER_ROWS:
                raise KnowledgeStoreError(f"ledger row limit exceeded: {path}")
            if len(raw) > MAX_LEDGER_LINE_BYTES:
                raise KnowledgeStoreError(f"ledger line too large: {path}:{line_number}")
            if not raw.strip():
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise KnowledgeStoreError(f"invalid JSONL: {path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise KnowledgeStoreError(f"JSONL row must be an object: {path}:{line_number}")
            rows.append(row)
    return rows


def _append_global_jsonl(path: Path, row: Mapping[str, Any], cfg: BuildupConfig) -> None:
    target = require_knowledge_write(path, cfg, context="knowledge binding append")
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n"
    if len(line.encode("utf-8")) > MAX_LEDGER_LINE_BYTES:
        raise KnowledgeStoreError("knowledge binding row exceeds size limit")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _bindings(cfg: BuildupConfig) -> Dict[str, str]:
    if cfg.knowledge_bindings_file.is_symlink():
        raise KnowledgeStoreError("knowledge bindings ledger cannot be a symlink")
    result: Dict[str, str] = {}
    for row in _read_jsonl(cfg.knowledge_bindings_file):
        if row.get("event") != "job_bound":
            continue
        try:
            job_id = validate_job_id(str(row.get("job_id") or ""))
            vault_id = validate_vault_id(str(row.get("vault_id") or ""))
        except ValueError:
            continue
        result[job_id] = vault_id
    return result


def bind_job_to_vault(
    job_id: str,
    vault_id: str,
    cfg: BuildupConfig,
    *,
    confirmed: bool = False,
) -> KnowledgeVaultStore:
    """Explicitly bind a job to a vault; cross-vault sharing needs confirmation."""
    job_id = validate_job_id(job_id)
    owning_job_candidate = cfg.workspace_dir / job_id
    if owning_job_candidate.is_symlink() or not owning_job_candidate.is_dir():
        raise ValueError(f"vault에 bind할 job 폴더가 없습니다: {job_id}")
    ensure_within(owning_job_candidate.resolve(), cfg.workspace_dir)
    vault_id = validate_vault_id(vault_id)
    default = _default_vault_id(job_id)
    if vault_id != default and not confirmed:
        raise PermissionError(
            "다른 job과 공유될 수 있는 vault bind에는 명시적 확인이 필요합니다. "
            "`--confirm` 또는 대화형 확인 후 다시 실행하세요."
        )
    store = KnowledgeVaultStore(vault_id, cfg)
    store.initialize(title=f"Knowledge for {job_id}")
    with store.lease():
        _compile_locked(store)
    lock = KnowledgeLease(cfg.knowledge_dir / ".bindings.lock", cfg)
    with lock:
        current = _bindings(cfg).get(job_id)
        if current != vault_id:
            _append_global_jsonl(cfg.knowledge_bindings_file, {
                "at": now_iso(),
                "event": "job_bound",
                "job_id": job_id,
                "vault_id": vault_id,
                "actor": "user" if confirmed else "system",
                "reason": "explicit-cross-vault-bind" if vault_id != default else "default-job-vault",
            }, cfg)
    return store


def _store_for_job(
    job_id: str,
    cfg: BuildupConfig,
    *,
    vault_id: str = "",
) -> KnowledgeVaultStore:
    job_id = validate_job_id(job_id)
    if vault_id:
        selected = validate_vault_id(vault_id)
        bound = _bindings(cfg).get(job_id)
        if bound != selected:
            raise PermissionError(
                f"job {job_id!r}은 vault {selected!r}에 bind되어 있지 않습니다."
            )
    else:
        selected = _bindings(cfg).get(job_id, "")
        if not selected:
            return bind_job_to_vault(job_id, _default_vault_id(job_id), cfg)
    store = KnowledgeVaultStore(selected, cfg)
    store.initialize(title=f"Knowledge for {job_id}")
    return store


def _manifest_digest(run_dir: Path) -> str:
    return hashlib.sha256((run_dir / "manifest.json").read_bytes()).hexdigest()


def _proposal_digest(proposal: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(proposal), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_completed_state(
    run_dir: Path,
    job_id: str,
    cfg: BuildupConfig,
) -> ResearchState:
    candidate = run_dir.expanduser()
    if candidate.is_symlink():
        raise ValueError("research run 심볼릭 링크는 지식에 반영할 수 없습니다.")
    validated_job = validate_job_id(job_id)
    owning_job_candidate = cfg.workspace_dir / validated_job
    if owning_job_candidate.is_symlink():
        raise ValueError("research run의 owning job은 심볼릭 링크일 수 없습니다.")
    owning_job = owning_job_candidate.resolve()
    if not owning_job.is_dir():
        raise ValueError(f"research run의 owning job 폴더가 없습니다: {job_id}")
    resolved = ensure_within(candidate.resolve(), owning_job)
    store = ResearchStore(resolved, job_id, cfg)
    state = store.load()
    failures = store.manifest_failures(state)
    if (
        state.status != "completed"
        or not state.citation_audit.get("passed")
        or not state.claim_audit.get("passed")
        or failures
        or not (resolved / "06-report.md").is_file()
        or not (resolved / "07-study-guide.md").is_file()
    ):
        details = ", ".join(failures) if failures else "quality gates not passed"
        raise ValueError(
            "봉인·인용·claim audit를 모두 통과한 완료 research run만 "
            f"지식에 반영할 수 있습니다: {details}"
        )
    return state


def _source_record(
    source: Any,
    state: ResearchState,
    run_dir: Path,
    proposal_id: str,
) -> Dict[str, Any]:
    source_version_id = stable_id(
        "KS",
        state.job_id,
        state.run_id,
        source.id,
        source.normalized_url,
        source.content_sha256,
        length=18,
    )
    metadata = source.metadata if isinstance(source.metadata, dict) else {}
    safe_metadata = {
        key: value
        for key, value in metadata.items()
        if key in {
            "openalex_id", "doi", "publication_year", "work_type",
            "cited_by_count", "is_retracted", "is_oa", "locator_kind",
            "raw_artifact", "raw_sha256", "raw_bytes",
        }
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    raw_relative = str(safe_metadata.get("raw_artifact") or "")
    raw_artifact_path = (
        str(ensure_within((run_dir / raw_relative).resolve(), run_dir))
        if raw_relative else ""
    )
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "source_version_id": source_version_id,
        "research_source_id": source.id,
        "proposal_id": proposal_id,
        "run_id": state.run_id,
        "job_id": state.job_id,
        "title": source.title,
        "url": source.url,
        "normalized_url": source.normalized_url,
        "artifact_path": str((run_dir / "03-sources.jsonl").resolve()),
        "manifest_path": str((run_dir / "manifest.json").resolve()),
        "source_type": source.source_type,
        "provider": source.provider,
        "author": source.author,
        "published_at": source.published_at,
        "retrieved_at": source.retrieved_at,
        "content_sha256": source.content_sha256,
        "raw_artifact_path": raw_artifact_path,
        "raw_sha256": str(safe_metadata.get("raw_sha256") or ""),
        "raw_bytes": int(safe_metadata.get("raw_bytes") or 0),
        "metadata": safe_metadata,
        "recorded_at": state.updated_at,
    }


def _evidence_record(
    evidence: Any,
    source_records: Mapping[str, Dict[str, Any]],
) -> Dict[str, Any]:
    source = source_records[evidence.source_id]
    return {
        "research_evidence_id": evidence.id,
        "research_source_id": evidence.source_id,
        "source_version_id": source["source_version_id"],
        "artifact_path": source["artifact_path"],
        "url": source["url"],
        "content_sha256": source["content_sha256"],
        "passage": evidence.passage,
        "passage_sha256": evidence.passage_sha256,
        "locator": evidence.locator,
        "stance": evidence.stance,
        "verified_exact": evidence.verified_exact,
    }


def _proposal_from_state(
    state: ResearchState,
    run_dir: Path,
    vault_id: str,
) -> Dict[str, Any]:
    manifest_digest = _manifest_digest(run_dir)
    proposal_id = stable_id(
        "KP", vault_id, state.run_id, manifest_digest, length=20
    )
    source_records = {
        source.id: _source_record(source, state, run_dir, proposal_id)
        for source in state.sources
    }
    evidence_by_id = {item.id: item for item in state.evidence}
    supporting_owner: Dict[str, List[str]] = {}
    for claim in state.claims:
        for evidence_id in claim.evidence_ids:
            supporting_owner.setdefault(evidence_id, []).append(claim.id)

    claims: List[Dict[str, Any]] = []
    for claim in state.claims:
        support = [
            _evidence_record(evidence_by_id[evidence_id], source_records)
            for evidence_id in claim.evidence_ids
            if evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].source_id in source_records
        ]
        contradiction = [
            _evidence_record(evidence_by_id[evidence_id], source_records)
            for evidence_id in claim.contradicting_evidence_ids
            if evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].source_id in source_records
        ]
        related = sorted({
            owner
            for evidence_id in claim.contradicting_evidence_ids
            for owner in supporting_owner.get(evidence_id, [])
            if owner != claim.id
        })
        claims.append({
            "claim_id": claim.id,
            "text": claim.text,
            "proposed_status": KnowledgeStatus.DRAFT.value,
            "validated_status": (
                KnowledgeStatus.DISPUTED.value
                if contradiction or claim.status == "contested"
                else KnowledgeStatus.SUPPORTED.value
            ),
            "critical": claim.critical,
            "confidence": claim.confidence,
            "evidence": support,
            "contradicting_evidence": contradiction,
            "related_claim_ids": related,
        })
    contract = state.contract.__dict__ if state.contract else {}
    report_path = run_dir / "06-report.md"
    guide_path = run_dir / "07-study-guide.md"
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "vault_id": vault_id,
        "status": "ready",
        "created_at": state.updated_at,
        "source_kind": "sealed-deep-research-run",
        "job_id": state.job_id,
        "run_id": state.run_id,
        "run_dir": str(run_dir.resolve()),
        "manifest_sha256": manifest_digest,
        "audit": {
            "citation_passed": True,
            "claim_passed": True,
            "manifest_passed": True,
        },
        "sources": list(source_records.values()),
        "claims": claims,
        "gaps": [
            {
                "gap_id": gap.id,
                "question": gap.question,
                "reason": gap.reason,
                "importance": gap.importance,
                "suggested_queries": gap.suggested_queries,
            }
            for gap in state.gaps
            if gap.status == "open"
        ],
        "synthesis": {
            "query": state.query,
            "objective": str(contract.get("objective") or state.query),
            "coverage": state.coverage,
            "report_path": str(report_path.resolve()),
            "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "study_guide_path": str(guide_path.resolve()),
            "study_guide_sha256": hashlib.sha256(guide_path.read_bytes()).hexdigest(),
        },
    }


def _rolled_back(events: Sequence[Mapping[str, Any]]) -> set[str]:
    rolled_back: set[str] = set()
    for event in events:
        proposal_id = str(event.get("proposal_id") or "")
        if (
            not proposal_id
            or event.get("schema_version") != KNOWLEDGE_SCHEMA_VERSION
            or event.get("actor") != "user"
        ):
            continue
        if event.get("event") == "proposal_rollback":
            rolled_back.add(proposal_id)
        elif event.get("event") == "proposal_restore":
            rolled_back.discard(proposal_id)
    return rolled_back


def _valid_applied_proposals(
    store: KnowledgeVaultStore,
) -> Dict[str, Dict[str, Any]]:
    proposals = {
        str(item.get("proposal_id") or ""): item
        for item in store.read_proposals()
        if str(item.get("proposal_id") or "")
    }
    commits: Dict[str, List[str]] = {}
    for item in store.read_log():
        if item.get("event") != "proposal_applied":
            continue
        proposal_id = str(item.get("proposal_id") or "")
        proposal_hash = str(item.get("proposal_sha256") or "")
        commits.setdefault(proposal_id, []).append(proposal_hash)
    return {
        proposal_id: proposal
        for proposal_id, proposal in proposals.items()
        if _PROPOSAL_ID_RE.fullmatch(proposal_id)
        and proposal.get("vault_id") == store.vault_id
        and proposal.get("schema_version") == KNOWLEDGE_SCHEMA_VERSION
        and proposal.get("status") == "ready"
        and proposal.get("source_kind") == "sealed-deep-research-run"
        and isinstance(proposal.get("sources"), list)
        and isinstance(proposal.get("claims"), list)
        and len(commits.get(proposal_id, [])) == 1
        and _SHA256_RE.fullmatch(commits[proposal_id][0])
        and commits[proposal_id][0] == _proposal_digest(proposal)
    }


def _applied_proposal_ids(store: KnowledgeVaultStore) -> set[str]:
    return set(_valid_applied_proposals(store))


def _active_sources(store: KnowledgeVaultStore) -> Dict[str, Dict[str, Any]]:
    events = store.read_claim_events()
    rolled_back = _rolled_back(events)
    applied = _valid_applied_proposals(store)
    expected_sources = {
        (proposal_id, str(source.get("source_version_id") or "")): source
        for proposal_id, proposal in applied.items()
        for source in proposal.get("sources", [])
        if isinstance(source, dict)
    }
    sources: Dict[str, Dict[str, Any]] = {}
    for source in store.read_sources():
        proposal_id = str(source.get("proposal_id") or "")
        if proposal_id not in applied or proposal_id in rolled_back:
            continue
        source_id = str(source.get("source_version_id") or "")
        if source_id and source == expected_sources.get((proposal_id, source_id)):
            sources[source_id] = dict(source)
    return sources


def _merge_evidence(
    existing: Iterable[Mapping[str, Any]],
    incoming: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in [*existing, *incoming]:
        key = (
            str(item.get("research_evidence_id") or ""),
            str(item.get("source_version_id") or ""),
        )
        if all(key):
            merged[key] = dict(item)
    return [merged[key] for key in sorted(merged)]


def _materialize_claims(store: KnowledgeVaultStore) -> Dict[str, Dict[str, Any]]:
    events = store.read_claim_events()
    rolled_back = _rolled_back(events)
    applied = _valid_applied_proposals(store)
    proposed_claims = {
        (proposal_id, str(claim.get("claim_id") or "")): claim
        for proposal_id, proposal in applied.items()
        for claim in proposal.get("claims", [])
        if isinstance(claim, dict)
    }
    claims: Dict[str, Dict[str, Any]] = {}
    for event in events:
        proposal_id = str(event.get("proposal_id") or "")
        if proposal_id and (
            proposal_id not in applied or proposal_id in rolled_back
        ):
            continue
        event_name = str(event.get("event") or "")
        claim_id = str(event.get("claim_id") or "")
        if event_name == "claim_upsert" and claim_id:
            proposal_claim = proposed_claims.get((proposal_id, claim_id))
            if not proposal_claim:
                continue
            expected_event = _claim_event_from_proposal(
                proposal_claim, applied[proposal_id]
            )
            expected_event["at"] = event.get("at")
            if event != expected_event:
                continue
            incoming = dict(event)
            previous = claims.get(claim_id)
            if previous is None:
                incoming["support_runs"] = [str(event.get("run_id") or "")]
                claims[claim_id] = incoming
                continue
            previous["evidence"] = _merge_evidence(
                previous.get("evidence", []), incoming.get("evidence", [])
            )
            previous["contradicting_evidence"] = _merge_evidence(
                previous.get("contradicting_evidence", []),
                incoming.get("contradicting_evidence", []),
            )
            previous["related_claim_ids"] = sorted({
                *[str(item) for item in previous.get("related_claim_ids", [])],
                *[str(item) for item in incoming.get("related_claim_ids", [])],
            })
            previous["support_runs"] = sorted({
                *[str(item) for item in previous.get("support_runs", []) if item],
                str(event.get("run_id") or ""),
            } - {""})
            previous["critical"] = bool(previous.get("critical") or incoming.get("critical"))
            previous["confidence"] = max(
                float(previous.get("confidence") or 0.0),
                float(incoming.get("confidence") or 0.0),
            )
            current_status = str(previous.get("status") or "")
            if current_status != KnowledgeStatus.REJECTED.value:
                if previous["contradicting_evidence"]:
                    previous["status"] = KnowledgeStatus.DISPUTED.value
                elif current_status == KnowledgeStatus.VERIFIED.value:
                    previous["status"] = KnowledgeStatus.VERIFIED.value
                else:
                    previous["status"] = KnowledgeStatus.SUPPORTED.value
            previous["updated_at"] = event.get("at")
            previous["latest_proposal_id"] = proposal_id
        elif event_name == "claim_status" and claim_id in claims:
            new_status = str(event.get("status") or "")
            current_status = str(claims[claim_id].get("status") or "")
            actor = str(event.get("actor") or "")
            valid_transition = (
                new_status == KnowledgeStatus.VERIFIED.value
                and current_status == KnowledgeStatus.SUPPORTED.value
                and actor == "user"
            ) or (
                new_status == KnowledgeStatus.REJECTED.value
                and actor == "user"
            ) or (
                new_status == KnowledgeStatus.STALE.value
                and actor == "system"
                and current_status != KnowledgeStatus.REJECTED.value
            )
            if not valid_transition:
                continue
            claims[claim_id]["status"] = new_status
            claims[claim_id]["status_reason"] = str(event.get("reason") or "")
            claims[claim_id]["status_actor"] = actor
            claims[claim_id]["updated_at"] = event.get("at")
            if new_status == KnowledgeStatus.VERIFIED.value:
                claims[claim_id]["verified_at"] = event.get("at")
                claims[claim_id]["verified_via"] = str(event.get("via") or "user")
    return claims


def _grounded_claims(store: KnowledgeVaultStore) -> Dict[str, Dict[str, Any]]:
    """Expose only claims whose immutable proposal and source rows still agree."""
    sources = _active_sources(store)
    grounded: Dict[str, Dict[str, Any]] = {}
    for claim_id, claim in _materialize_claims(store).items():
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            continue
        valid = True
        for item in evidence:
            if not isinstance(item, dict):
                valid = False
                break
            source = sources.get(str(item.get("source_version_id") or ""))
            if not source or (
                item.get("research_source_id") != source.get("research_source_id")
                or item.get("artifact_path") != source.get("artifact_path")
                or item.get("url") != source.get("url")
                or item.get("content_sha256") != source.get("content_sha256")
            ):
                valid = False
                break
        if valid:
            grounded[claim_id] = claim
    return grounded


def _active_proposals(store: KnowledgeVaultStore) -> List[Dict[str, Any]]:
    rolled_back = _rolled_back(store.read_claim_events())
    applied = _valid_applied_proposals(store)
    proposals = [
        proposal
        for proposal in applied.values()
        if str(proposal.get("proposal_id") or "") in applied
        and str(proposal.get("proposal_id") or "") not in rolled_back
    ]
    return sorted(
        proposals,
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("proposal_id") or ""),
        ),
    )


def _claim_event_from_proposal(
    claim: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "at": now_iso(),
        "event": "claim_upsert",
        "proposal_id": proposal["proposal_id"],
        "run_id": proposal["run_id"],
        "job_id": proposal["job_id"],
        "claim_id": claim["claim_id"],
        "text": claim["text"],
        "status": claim["validated_status"],
        "critical": bool(claim.get("critical")),
        "confidence": float(claim.get("confidence") or 0.0),
        "evidence": list(claim.get("evidence") or []),
        "contradicting_evidence": list(claim.get("contradicting_evidence") or []),
        "related_claim_ids": list(claim.get("related_claim_ids") or []),
        "source_of_truth": str(Path(str(proposal["run_dir"])) / "manifest.json"),
    }


def _proposal_applied(store: KnowledgeVaultStore, proposal_id: str) -> bool:
    return proposal_id in _applied_proposal_ids(store)


def _apply_proposal(
    store: KnowledgeVaultStore,
    proposal: Dict[str, Any],
    cfg: BuildupConfig,
) -> KnowledgeIngestResult:
    proposal_id = str(proposal["proposal_id"])
    with store.lease():
        store.initialize()
        if _proposal_applied(store, proposal_id):
            if proposal_id in _rolled_back(store.read_claim_events()):
                existing_claims = _grounded_claims(store)
                proposal_claims = [
                    item for item in proposal.get("claims", [])
                    if isinstance(item, dict)
                ]
                store.append_claim_events(({
                    "schema_version": KNOWLEDGE_SCHEMA_VERSION,
                    "at": now_iso(),
                    "event": "proposal_restore",
                    "proposal_id": proposal_id,
                    "actor": "user",
                    "reason": "explicit re-ingest of a rolled-back proposal",
                },))
                store.append_log("proposal_restored", {
                    "proposal_id": proposal_id,
                    "actor": "user",
                })
                if cfg.knowledge_auto_compile:
                    _compile_locked(store)
                invalid = 0
                if cfg.knowledge_auto_lint:
                    invalid = len(_lint_locked(
                        store,
                        cfg,
                        repair_stale=False,
                        check_compiled=cfg.knowledge_auto_compile,
                    ).errors)
                conflicts = sum(
                    item.get("validated_status") == KnowledgeStatus.DISPUTED.value
                    for item in proposal_claims
                )
                return KnowledgeIngestResult(
                    store.vault_id,
                    store.root,
                    proposal_id,
                    str(proposal["run_id"]),
                    sum(
                        str(item.get("claim_id") or "") not in existing_claims
                        for item in proposal_claims
                    ),
                    sum(
                        str(item.get("claim_id") or "") in existing_claims
                        for item in proposal_claims
                    ),
                    conflicts,
                    0,
                    invalid=invalid,
                )
            return KnowledgeIngestResult(
                store.vault_id, store.root, proposal_id, str(proposal["run_id"]),
                0, 0, 0, 0, duplicate=True,
            )
        store.write_proposal(proposal)
        existing_claims = _grounded_claims(store)
        existing_sources = _active_sources(store)
        proposed_claim_ids = {
            str(claim.get("claim_id") or "") for claim in proposal.get("claims", [])
        }
        changed_old_versions: set[str] = set()
        for new_source in proposal.get("sources", []):
            new_url = str(new_source.get("normalized_url") or "")
            new_hash = str(new_source.get("content_sha256") or "")
            for old_id, old_source in existing_sources.items():
                if (
                    old_source.get("normalized_url") == new_url
                    and old_source.get("content_sha256") != new_hash
                ):
                    changed_old_versions.add(old_id)
        stale_events: List[Dict[str, Any]] = []
        for claim_id, claim in existing_claims.items():
            referenced = {
                str(item.get("source_version_id") or "")
                for item in [
                    *claim.get("evidence", []),
                    *claim.get("contradicting_evidence", []),
                ]
            }
            if (
                claim_id not in proposed_claim_ids
                and referenced & changed_old_versions
                and claim.get("status") != KnowledgeStatus.REJECTED.value
            ):
                stale_events.append({
                    "schema_version": KNOWLEDGE_SCHEMA_VERSION,
                    "at": now_iso(),
                    "event": "claim_status",
                    "claim_id": claim_id,
                    "status": KnowledgeStatus.STALE.value,
                    "actor": "system",
                    "reason": "source content hash changed in a later sealed research run",
                    "proposal_id": proposal_id,
                })
        claim_events = [
            _claim_event_from_proposal(claim, proposal)
            for claim in proposal.get("claims", [])
        ]
        added = sum(
            1 for event in claim_events if event["claim_id"] not in existing_claims
        )
        updated = len(claim_events) - added
        conflicts = sum(
            1 for event in claim_events
            if event["status"] == KnowledgeStatus.DISPUTED.value
        )
        store.append_sources(proposal.get("sources", []))
        store.append_claim_events([*stale_events, *claim_events])
        store.append_log("proposal_applied", {
            "proposal_id": proposal_id,
            "proposal_sha256": _proposal_digest(proposal),
            "run_id": proposal["run_id"],
            "job_id": proposal["job_id"],
            "claims_added": added,
            "claims_updated": updated,
            "conflicts": conflicts,
            "stale_claims": len(stale_events),
        })
        if cfg.knowledge_auto_compile:
            _compile_locked(store)
        invalid = 0
        if cfg.knowledge_auto_lint:
            report = _lint_locked(
                store,
                cfg,
                repair_stale=False,
                check_compiled=cfg.knowledge_auto_compile,
            )
            invalid = len(report.errors)
        return KnowledgeIngestResult(
            store.vault_id,
            store.root,
            proposal_id,
            str(proposal["run_id"]),
            added,
            updated,
            conflicts,
            len(stale_events),
            invalid=invalid,
        )


def auto_ingest_research_run(
    run_dir: Path,
    job_id: str,
    cfg: BuildupConfig,
) -> KnowledgeIngestResult:
    """Import one just-completed run without another model call."""
    state = _load_completed_state(run_dir, job_id, cfg)
    store = _store_for_job(job_id, cfg)
    proposal = _proposal_from_state(state, run_dir.resolve(), store.vault_id)
    return _apply_proposal(store, proposal, cfg)


def add_research_to_vault(
    selector: str,
    job_id: str,
    cfg: BuildupConfig,
) -> KnowledgeIngestResult:
    """Resolve a saved research run and deterministically ingest it."""
    from buildup.deep_research import resolve_research_run

    selected = resolve_research_run(selector or "latest", job_id, cfg)
    if not selected:
        raise ValueError(f"research run을 찾지 못했습니다: {selector or 'latest'}")
    return auto_ingest_research_run(
        Path(str(selected["run_dir"])), job_id, cfg
    )


def _ledger_digest(store: KnowledgeVaultStore) -> str:
    digest = hashlib.sha256()
    for path in (store.sources_path, store.claims_path):
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()


def _md(value: Any) -> str:
    text = html.escape(
        str(value or "").replace("\x00", ""), quote=False
    ).strip()
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _quote(value: Any, *, limit: int = 1200) -> str:
    text = _md(value)[:limit]
    return "\n".join("> " + line for line in text.splitlines()) or "> (empty)"


def _citation_lines(
    evidence: Sequence[Mapping[str, Any]],
    sources: Mapping[str, Mapping[str, Any]],
) -> List[str]:
    lines: List[str] = []
    for index, item in enumerate(evidence, start=1):
        source = sources.get(str(item.get("source_version_id") or ""), {})
        block = [
            f"### Evidence {index}",
            "",
            _quote(item.get("passage")),
            "",
            f"- source: {_md(source.get('title')) or 'Untitled'}",
            f"- URL: `{_md(source.get('url'))}`",
            f"- locator: {_md(item.get('locator')) or '-'}",
            f"- source hash: `{_md(source.get('content_sha256'))}`",
            f"- evidence ID: `{_md(item.get('research_evidence_id'))}`",
            f"- artifact: `{_md(source.get('artifact_path'))}`",
        ]
        if source.get("raw_artifact_path"):
            block.extend([
                f"- original PDF: `{_md(source.get('raw_artifact_path'))}`",
                f"- original PDF hash: `{_md(source.get('raw_sha256'))}`",
            ])
        block.append("")
        lines.extend(block)
    return lines


def _compiled_files(store: KnowledgeVaultStore) -> Dict[str, str]:
    claims = _grounded_claims(store)
    sources = _active_sources(store)
    proposals = _active_proposals(store)
    status_counts = Counter(str(claim.get("status") or "") for claim in claims.values())
    version = _ledger_digest(store)[:16]
    files: Dict[str, str] = {}

    index_lines = [
        f"# Knowledge Vault — {store.vault_id}",
        "",
        "> 이 Wiki는 봉인된 Deep Research 산출물에서 재생성된 파생 뷰입니다. "
        "Wiki 페이지 자체는 근거가 아니며, 모든 주장은 아래 원본 URL·artifact·hash로 추적됩니다.",
        "",
        f"- knowledge version: `{version}`",
        f"- active claims: {len(claims)}",
        f"- source versions: {len(sources)}",
        f"- active proposals: {len(proposals)}",
        "- auto verify: never",
        "- destructive edit: disabled",
        "",
        "## Claim status",
        "",
    ]
    for status in KnowledgeStatus:
        index_lines.append(f"- {status.value}: {status_counts.get(status.value, 0)}")
    index_lines.extend(["", "## Claims", ""])
    for claim_id, claim in sorted(claims.items()):
        status = _md(claim.get("status"))
        index_lines.append(
            f"- [{_md(claim.get('text'))}](pages/claims/{claim_id}.md) "
            f"— `{status}` · `{claim_id}`"
        )
    if not claims:
        index_lines.append("- (none)")
    index_lines.extend(["", "## Original sources", ""])
    for source_id, source in sorted(sources.items()):
        index_lines.append(
            f"- [{_md(source.get('title')) or 'Untitled'}]"
            f"(pages/papers/{source_id}.md) · `{source_id}`"
        )
    if not sources:
        index_lines.append("- (none)")
    files["index.md"] = "\n".join(index_lines)

    for claim_id, claim in sorted(claims.items()):
        lines = [
            f"# Claim {claim_id}",
            "",
            f"**Status:** `{_md(claim.get('status'))}`",
            "",
            _md(claim.get("text")),
            "",
            "## Provenance",
            "",
            f"- research runs: {', '.join('`' + item + '`' for item in claim.get('support_runs', [])) or '-'}",
            f"- critical: {bool(claim.get('critical'))}",
            f"- confidence: {float(claim.get('confidence') or 0.0):.2f}",
            "",
            "## Supporting evidence",
            "",
            *_citation_lines(claim.get("evidence", []), sources),
        ]
        contradiction = claim.get("contradicting_evidence", [])
        if contradiction:
            lines.extend([
                "## Contradicting evidence",
                "",
                "충돌은 자동으로 해결하지 않습니다. 양쪽 근거를 함께 보존합니다.",
                "",
                *_citation_lines(contradiction, sources),
            ])
        files[f"pages/claims/{claim_id}.md"] = "\n".join(lines)
        if claim.get("status") == KnowledgeStatus.DISPUTED.value or contradiction:
            conflict_lines = [
                f"# Contradiction — {claim_id}",
                "",
                f"- claim: [{_md(claim.get('text'))}](../claims/{claim_id}.md)",
                f"- status: `{_md(claim.get('status'))}`",
                "- resolution: unresolved (no automatic winner)",
                "",
                "## Counter-evidence",
                "",
                *_citation_lines(contradiction, sources),
            ]
            related = claim.get("related_claim_ids", [])
            if related:
                conflict_lines.extend([
                    "## Related claims",
                    "",
                    *[f"- [{item}](../claims/{item}.md)" for item in related],
                ])
            files[f"pages/contradictions/{claim_id}.md"] = "\n".join(conflict_lines)

    for source_id, source in sorted(sources.items()):
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        lines = [
            f"# {_md(source.get('title')) or 'Untitled'}",
            "",
            "> Source registry view. This page is not itself evidence.",
            "",
            f"- source version: `{source_id}`",
            f"- research source: `{_md(source.get('research_source_id'))}`",
            f"- URL: `{_md(source.get('url'))}`",
            f"- content hash: `{_md(source.get('content_sha256'))}`",
            f"- provider: {_md(source.get('provider')) or '-'}",
            f"- type: {_md(source.get('source_type')) or '-'}",
            f"- author: {_md(source.get('author')) or '-'}",
            f"- published: {_md(source.get('published_at')) or '-'}",
            f"- retrieved: {_md(source.get('retrieved_at')) or '-'}",
            f"- artifact: `{_md(source.get('artifact_path'))}`",
        ]
        if source.get("raw_artifact_path"):
            lines.extend([
                f"- original PDF: `{_md(source.get('raw_artifact_path'))}`",
                f"- original PDF hash: `{_md(source.get('raw_sha256'))}`",
                f"- original PDF bytes: {int(source.get('raw_bytes') or 0)}",
            ])
        if metadata:
            lines.extend(["", "## Discovery metadata", ""])
            for key, value in sorted(metadata.items()):
                lines.append(f"- {key}: {_md(value)}")
        files[f"pages/papers/{source_id}.md"] = "\n".join(lines)

    for proposal in proposals:
        run_id = _md(proposal.get("run_id"))
        synthesis = proposal.get("synthesis") if isinstance(proposal.get("synthesis"), dict) else {}
        proposal_claims = [
            str(item.get("claim_id") or "")
            for item in proposal.get("claims", [])
            if str(item.get("claim_id") or "") in claims
        ]
        synthesis_lines = [
            f"# Synthesis — {run_id}",
            "",
            "> Deep Research 보고서의 파생 인덱스입니다. 원문 보고서와 원본 source가 근거 경계입니다.",
            "",
            f"- query: {_md(synthesis.get('query'))}",
            f"- objective: {_md(synthesis.get('objective'))}",
            f"- coverage: {float(synthesis.get('coverage') or 0.0):.2f}",
            f"- report: `{_md(synthesis.get('report_path'))}`",
            f"- report hash: `{_md(synthesis.get('report_sha256'))}`",
            f"- proposal: `{_md(proposal.get('proposal_id'))}`",
            "",
            "## Grounded claims",
            "",
            *[
                f"- [{_md(claims[item].get('text'))}](../claims/{item}.md)"
                for item in proposal_claims
            ],
        ]
        files[f"pages/synthesis/{run_id}.md"] = "\n".join(synthesis_lines)
        gaps = proposal.get("gaps") if isinstance(proposal.get("gaps"), list) else []
        if gaps:
            gap_lines = [f"# Open Questions — {run_id}", ""]
            for gap in gaps:
                if not isinstance(gap, dict):
                    continue
                gap_lines.extend([
                    f"## {_md(gap.get('question'))}",
                    "",
                    f"- gap ID: `{_md(gap.get('gap_id'))}`",
                    f"- importance: {int(gap.get('importance') or 0)}",
                    f"- reason: {_md(gap.get('reason'))}",
                    "- suggested queries:",
                    *[f"  - {_md(item)}" for item in gap.get("suggested_queries", [])],
                    "",
                ])
            files[f"pages/open-questions/{run_id}.md"] = "\n".join(gap_lines)
    return files


def _compile_locked(store: KnowledgeVaultStore) -> Path:
    store.write_compiled(_compiled_files(store))
    return store.root / "index.md"


def compile_vault(job_id: str, cfg: BuildupConfig) -> Path:
    store = _store_for_job(job_id, cfg)
    with store.lease():
        return _compile_locked(store)


def _artifact_source_rows(path: Path) -> Dict[str, Dict[str, Any]]:
    rows = _read_jsonl(path)
    return {
        str(row.get("id") or ""): row
        for row in rows
        if str(row.get("id") or "")
    }


def _lint_locked(
    store: KnowledgeVaultStore,
    cfg: BuildupConfig,
    *,
    repair_stale: bool,
    check_compiled: bool = True,
) -> KnowledgeLintReport:
    errors: List[str] = []
    warnings: List[str] = []
    repaired = 0
    if (
        store.schema_path.is_symlink()
        or not store.schema_path.is_file()
        or store.schema_path.read_text(encoding="utf-8") != SCHEMA_YAML
    ):
        errors.append("schema.yaml missing or modified")
    try:
        if store.metadata_path.is_symlink():
            raise ValueError("vault.json is a symlink")
        metadata = json.loads(store.metadata_path.read_text(encoding="utf-8"))
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema_version") != KNOWLEDGE_SCHEMA_VERSION
            or metadata.get("vault_id") != store.vault_id
        ):
            errors.append("vault.json identity/schema mismatch")
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("vault.json unreadable")

    try:
        source_rows = store.read_sources()
        events = store.read_claim_events()
        log_rows = store.read_log()
        proposal_rows = store.read_proposals()
    except KnowledgeStoreError as exc:
        return KnowledgeLintReport(
            store.vault_id, False, [str(exc)], [], {}, repaired_stale=0
        )
    proposal_by_id = {
        str(item.get("proposal_id") or ""): item for item in proposal_rows
    }
    applied_rows = [
        item for item in log_rows if item.get("event") == "proposal_applied"
    ]
    applied_counts = Counter(
        str(item.get("proposal_id") or "") for item in applied_rows
    )
    applied_hashes: Dict[str, str] = {}
    for item in applied_rows:
        proposal_id = str(item.get("proposal_id") or "")
        proposal_hash = str(item.get("proposal_sha256") or "")
        if not _PROPOSAL_ID_RE.fullmatch(proposal_id) or not _SHA256_RE.fullmatch(
            proposal_hash
        ):
            errors.append(f"proposal commit record invalid: {proposal_id or '<empty>'}")
            continue
        applied_hashes[proposal_id] = proposal_hash
    for proposal_id, count in applied_counts.items():
        if proposal_id and count > 1:
            errors.append(f"duplicate proposal commit record: {proposal_id}")

    proposal_sources: Dict[Tuple[str, str], Dict[str, Any]] = {}
    proposal_claims: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for proposal in proposal_rows:
        proposal_id = str(proposal.get("proposal_id") or "")
        run_id = str(proposal.get("run_id") or "")
        manifest_hash = str(proposal.get("manifest_sha256") or "")
        expected_id = stable_id(
            "KP", store.vault_id, run_id, manifest_hash, length=20
        )
        if (
            not _PROPOSAL_ID_RE.fullmatch(proposal_id)
            or proposal_id != expected_id
            or proposal.get("vault_id") != store.vault_id
            or proposal.get("schema_version") != KNOWLEDGE_SCHEMA_VERSION
            or proposal.get("status") != "ready"
            or proposal.get("source_kind") != "sealed-deep-research-run"
        ):
            errors.append(f"proposal identity/schema invalid: {proposal_id or '<empty>'}")
        try:
            proposal_job = validate_job_id(str(proposal.get("job_id") or ""))
            proposal_job_candidate = cfg.workspace_dir / proposal_job
            if proposal_job_candidate.is_symlink():
                raise ValueError("owning job directory is a symlink")
            proposal_job_root = proposal_job_candidate.resolve()
            if not proposal_job_root.is_dir():
                raise ValueError("owning job directory is missing")
            run_candidate = Path(str(proposal.get("run_dir") or "")).expanduser()
            if run_candidate.is_symlink():
                raise ValueError("run directory is a symlink")
            proposal_run_dir = ensure_within(
                run_candidate.resolve(), proposal_job_root
            )
            if proposal_run_dir.name != run_id:
                raise ValueError("run ID/path mismatch")
            manifest = proposal_run_dir / "manifest.json"
            if (
                hashlib.sha256(manifest.read_bytes()).hexdigest() != manifest_hash
                or verify_manifest(proposal_run_dir, expected_run_id=run_id)
            ):
                raise ValueError("sealed manifest failed")
            synthesis = proposal.get("synthesis")
            if not isinstance(synthesis, dict):
                raise ValueError("synthesis is missing")
            for path_key, hash_key, expected_name in (
                ("report_path", "report_sha256", "06-report.md"),
                ("study_guide_path", "study_guide_sha256", "07-study-guide.md"),
            ):
                artifact_candidate = Path(
                    str(synthesis.get(path_key) or "")
                ).expanduser()
                if artifact_candidate.is_symlink():
                    raise ValueError(f"invalid {path_key} symlink")
                artifact = ensure_within(
                    artifact_candidate.resolve(),
                    proposal_run_dir,
                )
                if artifact.name != expected_name:
                    raise ValueError(f"invalid {path_key}")
                if hashlib.sha256(artifact.read_bytes()).hexdigest() != synthesis.get(hash_key):
                    raise ValueError(f"{path_key} hash mismatch")
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"proposal source-of-truth invalid {proposal_id}: {exc}")
        applied_hash = applied_hashes.get(proposal_id)
        if not applied_hash:
            warnings.append(f"proposal is not applied: {proposal_id}")
        elif applied_hash != _proposal_digest(proposal):
            errors.append(f"immutable proposal hash mismatch: {proposal_id}")
        audit = proposal.get("audit")
        if not isinstance(audit, dict) or not all(
            audit.get(key) is True
            for key in ("citation_passed", "claim_passed", "manifest_passed")
        ):
            errors.append(f"proposal audit boundary invalid: {proposal_id}")
        if not isinstance(proposal.get("sources"), list) or not isinstance(
            proposal.get("claims"), list
        ):
            errors.append(f"proposal collections invalid: {proposal_id}")
            continue
        for source in proposal["sources"]:
            if not isinstance(source, dict):
                errors.append(f"proposal source is not an object: {proposal_id}")
                continue
            source_id = str(source.get("source_version_id") or "")
            key = (proposal_id, source_id)
            if not source_id or key in proposal_sources:
                errors.append(f"proposal source identity is invalid/duplicate: {proposal_id}")
                continue
            proposal_sources[key] = source
        for claim in proposal["claims"]:
            if not isinstance(claim, dict):
                errors.append(f"proposal claim is not an object: {proposal_id}")
                continue
            claim_id = str(claim.get("claim_id") or "")
            text = str(claim.get("text") or "")
            key = (proposal_id, claim_id)
            if (
                not _CLAIM_ID_RE.fullmatch(claim_id)
                or claim_id != stable_id("C", text, length=14)
                or key in proposal_claims
                or claim.get("proposed_status") != KnowledgeStatus.DRAFT.value
                or claim.get("validated_status") not in {
                    KnowledgeStatus.SUPPORTED.value,
                    KnowledgeStatus.DISPUTED.value,
                }
                or not isinstance(claim.get("evidence"), list)
                or not claim.get("evidence")
                or not isinstance(claim.get("contradicting_evidence"), list)
            ):
                errors.append(
                    f"proposal claim schema invalid: {proposal_id}/{claim_id or '<empty>'}"
                )
                continue
            proposal_claims[key] = claim
    for proposal_id in applied_hashes:
        if proposal_id not in proposal_by_id:
            errors.append(f"committed proposal file is missing: {proposal_id}")
    allowed_claim_events = {
        "claim_upsert", "claim_status", "proposal_rollback", "proposal_restore",
    }
    for event in events:
        event_name = str(event.get("event") or "")
        proposal_id = str(event.get("proposal_id") or "")
        if event.get("schema_version") != KNOWLEDGE_SCHEMA_VERSION:
            errors.append(f"claim event schema invalid: {event_name or '<empty>'}")
        if event_name not in allowed_claim_events:
            errors.append(f"unknown claim event: {event_name or '<empty>'}")
        if proposal_id and proposal_id not in applied_hashes:
            errors.append(f"claim event references unapplied proposal: {proposal_id}")
        if event_name == "claim_upsert":
            claim_id = str(event.get("claim_id") or "")
            proposal_claim = proposal_claims.get((proposal_id, claim_id))
            if not proposal_claim:
                errors.append(
                    f"claim upsert is absent from its proposal: {proposal_id}/{claim_id}"
                )
            else:
                expected = _claim_event_from_proposal(
                    proposal_claim, proposal_by_id[proposal_id]
                )
                expected["at"] = event.get("at")
                if event != expected:
                    errors.append(
                        f"claim upsert/proposal mismatch: {proposal_id}/{claim_id}"
                    )
        elif event_name == "claim_status":
            status = str(event.get("status") or "")
            actor = str(event.get("actor") or "")
            if not (
                status in {
                    KnowledgeStatus.VERIFIED.value,
                    KnowledgeStatus.REJECTED.value,
                }
                and actor == "user"
            ) and not (
                status == KnowledgeStatus.STALE.value and actor == "system"
            ):
                errors.append(
                    f"claim status event is invalid: {event.get('claim_id')}/"
                    f"{event.get('status')}"
                )
        elif event_name in {"proposal_rollback", "proposal_restore"} and event.get(
            "actor"
        ) != "user":
            errors.append(f"proposal lifecycle actor is invalid: {proposal_id}")
    sources = _active_sources(store)
    claims = _materialize_claims(store)
    artifact_cache: Dict[Path, Dict[str, Dict[str, Any]]] = {}
    manifest_cache: Dict[Tuple[Path, str], List[str]] = {}
    corrupt_source_versions: set[str] = set()
    original_content_by_source: Dict[str, str] = {}
    seen_source_rows: set[Tuple[str, str]] = set()
    for row in source_rows:
        source_id = str(row.get("source_version_id") or "")
        proposal_id = str(row.get("proposal_id") or "")
        row_key = (source_id, proposal_id)
        if row_key in seen_source_rows:
            warnings.append(f"duplicate source ledger row: {source_id}/{proposal_id}")
        seen_source_rows.add(row_key)
        normalized_url = str(row.get("normalized_url") or "")
        source_url = str(row.get("url") or "")
        content_hash = str(row.get("content_sha256") or "")
        expected_id = stable_id(
            "KS",
            str(row.get("job_id") or ""),
            str(row.get("run_id") or ""),
            str(row.get("research_source_id") or ""),
            normalized_url,
            content_hash,
            length=18,
        )
        if not _SOURCE_VERSION_RE.fullmatch(source_id) or source_id != expected_id:
            errors.append(f"invalid source version ID: {source_id or '<empty>'}")
        if not _SHA256_RE.fullmatch(content_hash):
            errors.append(f"invalid source hash: {source_id}")
        if (
            not is_public_web_url(source_url)
            or not is_public_web_url(normalized_url)
            or normalize_url(source_url) != normalized_url
        ):
            errors.append(f"invalid source URL: {source_id}")
        if proposal_id not in proposal_by_id:
            errors.append(f"source references unknown proposal: {source_id}/{proposal_id}")
        elif proposal_id not in applied_hashes:
            errors.append(f"source references unapplied proposal: {source_id}/{proposal_id}")
        expected_source = proposal_sources.get((proposal_id, source_id))
        if expected_source is None:
            errors.append(f"source is absent from its proposal: {source_id}/{proposal_id}")
        elif row != expected_source:
            errors.append(f"source/proposal mismatch: {source_id}/{proposal_id}")
        if str(row.get("source_type") or "").lower() == "wiki":
            errors.append(f"wiki source type is forbidden: {source_id}")
        job_id = str(row.get("job_id") or "")
        try:
            source_job = validate_job_id(job_id)
            source_job_candidate = cfg.workspace_dir / source_job
            if source_job_candidate.is_symlink():
                raise ValueError("owning job directory is a symlink")
            source_job_root = source_job_candidate.resolve()
            if not source_job_root.is_dir():
                raise ValueError("owning job directory is missing")
            artifact_candidate = Path(str(row.get("artifact_path") or "")).expanduser()
            if artifact_candidate.is_symlink():
                raise ValueError("source artifact is a symlink")
            artifact_resolved = artifact_candidate.resolve()
            if artifact_resolved.is_relative_to(cfg.knowledge_dir.resolve()):
                raise ValueError("wiki page cannot be an evidence artifact")
            artifact = ensure_within(
                artifact_resolved,
                source_job_root,
            )
            if artifact.name != "03-sources.jsonl":
                raise ValueError("unsafe source artifact")
            expected_manifest = artifact.parent / "manifest.json"
            if Path(str(row.get("manifest_path") or "")).resolve() != expected_manifest:
                raise ValueError("manifest path does not match source artifact")
            rows = artifact_cache.setdefault(artifact, _artifact_source_rows(artifact))
            original = rows.get(str(row.get("research_source_id") or ""))
            if not original:
                raise ValueError("research source record missing")
            original_content = str(original.get("content") or "")
            original_hash = hashlib.sha256(original_content.encode("utf-8")).hexdigest()
            if (
                original_hash != content_hash
                or original.get("normalized_url") != normalized_url
                or original.get("url") != source_url
            ):
                corrupt_source_versions.add(source_id)
                raise ValueError("source content/hash changed")
            original_content_by_source[source_id] = original_content
            original_metadata = (
                original.get("metadata")
                if isinstance(original.get("metadata"), dict)
                else {}
            )
            raw_relative = str(original_metadata.get("raw_artifact") or "")
            raw_path_value = str(row.get("raw_artifact_path") or "")
            raw_hash = str(row.get("raw_sha256") or "")
            raw_bytes = int(row.get("raw_bytes") or 0)
            if raw_relative or raw_path_value or raw_hash or raw_bytes:
                expected_relative = (
                    f"raw-sources/{row.get('research_source_id')}.pdf"
                )
                if raw_relative != expected_relative:
                    raise ValueError("raw PDF relative path mismatch")
                raw_candidate = Path(raw_path_value).expanduser()
                if raw_candidate.is_symlink():
                    raise ValueError("raw PDF artifact is a symlink")
                raw_artifact = ensure_within(
                    raw_candidate.resolve(), artifact.parent
                )
                if raw_artifact != artifact.parent / expected_relative:
                    raise ValueError("raw PDF path escaped its sealed run")
                raw = raw_artifact.read_bytes()
                if (
                    not _SHA256_RE.fullmatch(raw_hash)
                    or raw_bytes != len(raw)
                    or hashlib.sha256(raw).hexdigest() != raw_hash
                    or not raw.startswith(b"%PDF-")
                    or original_metadata.get("raw_sha256") != raw_hash
                    or int(original_metadata.get("raw_bytes") or 0) != raw_bytes
                ):
                    corrupt_source_versions.add(source_id)
                    raise ValueError("raw PDF provenance/hash changed")
            run_dir = artifact.parent
            run_id = str(row.get("run_id") or "")
            key = (run_dir, run_id)
            failures = manifest_cache.setdefault(
                key, verify_manifest(run_dir, expected_run_id=run_id)
            )
            if failures:
                corrupt_source_versions.add(source_id)
                raise ValueError("sealed research manifest failed")
        except (OSError, TypeError, ValueError, KnowledgeStoreError) as exc:
            errors.append(f"source provenance invalid {source_id}: {exc}")

    stale_claims: List[str] = []
    for claim_id, claim in claims.items():
        text = str(claim.get("text") or "")
        if not _CLAIM_ID_RE.fullmatch(claim_id) or claim_id != stable_id("C", text, length=14):
            errors.append(f"invalid claim ID: {claim_id}")
        status = str(claim.get("status") or "")
        if status not in _STATUS_VALUES:
            errors.append(f"invalid claim status: {claim_id}/{status}")
        supporting = claim.get("evidence") if isinstance(claim.get("evidence"), list) else []
        contradicting = (
            claim.get("contradicting_evidence")
            if isinstance(claim.get("contradicting_evidence"), list)
            else []
        )
        if status not in {KnowledgeStatus.REJECTED.value, KnowledgeStatus.DRAFT.value} and not supporting:
            errors.append(f"grounded claim has no supporting evidence: {claim_id}")
        referenced: set[str] = set()
        for evidence_group, expected_stance in (
            (supporting, "supports"),
            (contradicting, "contradicts"),
        ):
            for item in evidence_group:
                if not isinstance(item, dict):
                    errors.append(f"invalid evidence record: {claim_id}")
                    continue
                source_id = str(item.get("source_version_id") or "")
                referenced.add(source_id)
                if source_id not in sources:
                    errors.append(f"unknown source version: {claim_id}/{source_id}")
                passage = str(item.get("passage") or "")
                if passage_sha256(passage) != item.get("passage_sha256"):
                    errors.append(f"evidence passage hash mismatch: {claim_id}")
                if item.get("verified_exact") is not True:
                    errors.append(f"unverified evidence: {claim_id}")
                if item.get("stance") != expected_stance:
                    errors.append(
                        f"evidence stance mismatch: {claim_id}/"
                        f"{item.get('research_evidence_id')}"
                    )
                source = sources.get(source_id, {})
                if source and (
                    item.get("research_source_id") != source.get("research_source_id")
                    or item.get("artifact_path") != source.get("artifact_path")
                    or item.get("url") != source.get("url")
                    or item.get("content_sha256") != source.get("content_sha256")
                ):
                    errors.append(
                        f"evidence/source provenance mismatch: {claim_id}/{source_id}"
                    )
                original_content = original_content_by_source.get(source_id, "")
                exact = verify_exact_passage(original_content, passage)
                expected_evidence_id = stable_id(
                    "E",
                    str(source.get("research_source_id") or ""),
                    exact.normalized_passage,
                    length=14,
                )
                if (
                    not exact.valid
                    or item.get("research_evidence_id") != expected_evidence_id
                ):
                    errors.append(
                        f"evidence is not an exact original-source passage: "
                        f"{claim_id}/{item.get('research_evidence_id')}"
                    )
        if referenced & corrupt_source_versions and status not in {
            KnowledgeStatus.STALE.value,
            KnowledgeStatus.REJECTED.value,
        }:
            stale_claims.append(claim_id)

    if stale_claims:
        if repair_stale:
            status_events = [{
                "schema_version": KNOWLEDGE_SCHEMA_VERSION,
                "at": now_iso(),
                "event": "claim_status",
                "claim_id": claim_id,
                "status": KnowledgeStatus.STALE.value,
                "actor": "system",
                "reason": "sealed source provenance failed lint",
                "proposal_id": "",
            } for claim_id in stale_claims]
            store.append_claim_events(status_events)
            repaired = len(status_events)
            _compile_locked(store)
            claims = _materialize_claims(store)
        else:
            warnings.extend(f"claim should be stale: {claim_id}" for claim_id in stale_claims)

    if check_compiled:
        try:
            expected_files = _compiled_files(store)
            actual_files: Dict[str, str] = {}
            index = store.root / "index.md"
            if index.is_file():
                actual_files["index.md"] = index.read_text(encoding="utf-8").rstrip()
            for category in PAGE_CATEGORIES:
                for page in (store.pages_dir / category).glob("*.md"):
                    if page.is_symlink():
                        errors.append(f"compiled page is a symlink: {page}")
                        continue
                    actual_files[str(page.relative_to(store.root))] = page.read_text(
                        encoding="utf-8"
                    ).rstrip()
            normalized_expected = {
                key: value.rstrip() for key, value in expected_files.items()
            }
            if set(actual_files) != set(normalized_expected):
                errors.append("compiled page set drift")
            else:
                for key, value in normalized_expected.items():
                    if actual_files.get(key) != value:
                        errors.append(f"compiled page content drift: {key}")
        except (OSError, ValueError, KnowledgeStoreError) as exc:
            errors.append(f"compiled page audit failed: {exc}")

    stats = {
        "source_versions": len(sources),
        "claims": len(claims),
        "supported": sum(
            claim.get("status") == KnowledgeStatus.SUPPORTED.value
            for claim in claims.values()
        ),
        "verified": sum(
            claim.get("status") == KnowledgeStatus.VERIFIED.value
            for claim in claims.values()
        ),
        "disputed": sum(
            claim.get("status") == KnowledgeStatus.DISPUTED.value
            for claim in claims.values()
        ),
        "stale": sum(
            claim.get("status") == KnowledgeStatus.STALE.value
            for claim in claims.values()
        ),
        "rejected": sum(
            claim.get("status") == KnowledgeStatus.REJECTED.value
            for claim in claims.values()
        ),
    }
    return KnowledgeLintReport(
        store.vault_id,
        not errors,
        sorted(set(errors)),
        sorted(set(warnings)),
        stats,
        repaired_stale=repaired,
    )


def lint_vault(
    job_id: str,
    cfg: BuildupConfig,
    *,
    repair_stale: bool = True,
) -> KnowledgeLintReport:
    store = _store_for_job(job_id, cfg)
    with store.lease():
        return _lint_locked(
            store, cfg, repair_stale=repair_stale, check_compiled=True
        )


def _query_tokens(text: str) -> set[str]:
    normalized = re.sub(r"[^0-9a-z가-힣]+", " ", text.lower()).strip()
    words = {word for word in normalized.split() if len(word) >= 2}
    compact = normalized.replace(" ", "")
    grams = {
        compact[index:index + 3]
        for index in range(max(0, len(compact) - 2))
    }
    return words | grams


def query_vault(
    query: str,
    job_id: str,
    cfg: BuildupConfig,
    *,
    limit: int = 8,
) -> List[KnowledgeQueryHit]:
    clean = re.sub(r"\s+", " ", query).strip()
    if not clean:
        raise ValueError("Wiki 질문이 비어 있습니다.")
    store = _store_for_job(job_id, cfg)
    claims = _grounded_claims(store)
    sources = _active_sources(store)
    query_tokens = _query_tokens(clean)
    hits: List[KnowledgeQueryHit] = []
    for claim_id, claim in claims.items():
        if claim.get("status") == KnowledgeStatus.REJECTED.value:
            continue
        text = str(claim.get("text") or "")
        claim_tokens = _query_tokens(text)
        overlap = len(query_tokens & claim_tokens)
        if not overlap and clean.lower() not in text.lower():
            continue
        denominator = max(1, len(query_tokens))
        score = overlap / denominator
        if clean.lower() in text.lower():
            score += 1.0
        citations: List[Dict[str, str]] = []
        for evidence in claim.get("evidence", []):
            source = sources.get(str(evidence.get("source_version_id") or ""), {})
            citations.append({
                "title": str(source.get("title") or "Untitled"),
                "url": str(source.get("url") or ""),
                "content_sha256": str(source.get("content_sha256") or ""),
                "artifact_path": str(source.get("artifact_path") or ""),
                "research_evidence_id": str(evidence.get("research_evidence_id") or ""),
                "locator": str(evidence.get("locator") or ""),
                "raw_artifact_path": str(source.get("raw_artifact_path") or ""),
                "raw_sha256": str(source.get("raw_sha256") or ""),
            })
        hits.append(KnowledgeQueryHit(
            claim_id=claim_id,
            text=text,
            status=str(claim.get("status") or ""),
            score=score,
            citations=citations,
        ))
    return sorted(hits, key=lambda hit: (-hit.score, hit.claim_id))[:max(1, min(50, limit))]


def _status_claim(
    claim_id: str,
    status: KnowledgeStatus,
    job_id: str,
    cfg: BuildupConfig,
    *,
    reason: str,
    via: str,
) -> Dict[str, Any]:
    store = _store_for_job(job_id, cfg)
    with store.lease():
        claims = _grounded_claims(store)
        claim = claims.get(claim_id)
        if not claim:
            raise ValueError(f"claim을 찾지 못했습니다: {claim_id}")
        current = str(claim.get("status") or "")
        if status is KnowledgeStatus.VERIFIED and current != KnowledgeStatus.SUPPORTED.value:
            raise ValueError(
                "supported 상태의 claim만 verified로 승격할 수 있습니다. "
                f"현재 상태: {current}"
            )
        if current == KnowledgeStatus.REJECTED.value and status is not KnowledgeStatus.REJECTED:
            raise ValueError("rejected claim은 자동으로 되살릴 수 없습니다.")
        event = {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "at": now_iso(),
            "event": "claim_status",
            "claim_id": claim_id,
            "status": status.value,
            "actor": "user",
            "reason": reason.strip(),
            "via": via,
            "proposal_id": "",
        }
        store.append_claim_events((event,))
        store.append_log("claim_reviewed", {
            "claim_id": claim_id,
            "from_status": current,
            "to_status": status.value,
            "via": via,
        })
        _compile_locked(store)
        return _grounded_claims(store)[claim_id]


def verify_claim(
    claim_id: str,
    job_id: str,
    cfg: BuildupConfig,
    *,
    reason: str = "user directly explained or checked this claim",
    via: str = "user",
) -> Dict[str, Any]:
    return _status_claim(
        claim_id, KnowledgeStatus.VERIFIED, job_id, cfg,
        reason=reason, via=via,
    )


def reject_claim(
    claim_id: str,
    job_id: str,
    cfg: BuildupConfig,
    *,
    reason: str = "user rejected this claim",
) -> Dict[str, Any]:
    return _status_claim(
        claim_id, KnowledgeStatus.REJECTED, job_id, cfg,
        reason=reason, via="user",
    )


def rollback_proposal(
    proposal_id: str,
    job_id: str,
    cfg: BuildupConfig,
) -> Path:
    store = _store_for_job(job_id, cfg)
    with store.lease():
        proposal_rows = _active_proposals(store)
        if proposal_id.strip().lower() in {"latest", "최근", "마지막"}:
            if not proposal_rows:
                raise ValueError("rollback할 active proposal이 없습니다.")
            proposal_id = str(proposal_rows[-1]["proposal_id"])
        proposals = {
            str(item.get("proposal_id") or ""): item
            for item in store.read_proposals()
        }
        if proposal_id not in proposals:
            raise ValueError(f"proposal을 찾지 못했습니다: {proposal_id}")
        if not _proposal_applied(store, proposal_id):
            raise ValueError(f"적용되지 않은 proposal은 rollback할 수 없습니다: {proposal_id}")
        if proposal_id in _rolled_back(store.read_claim_events()):
            return store.root / "index.md"
        store.append_claim_events(({
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "at": now_iso(),
            "event": "proposal_rollback",
            "proposal_id": proposal_id,
            "actor": "user",
            "reason": "explicit non-destructive rollback",
        },))
        store.append_log("proposal_rolled_back", {
            "proposal_id": proposal_id,
            "actor": "user",
        })
        return _compile_locked(store)


def vault_summary(job_id: str, cfg: BuildupConfig) -> KnowledgeVaultSummary:
    store = _store_for_job(job_id, cfg)
    claims = _grounded_claims(store)
    sources = _active_sources(store)
    proposals = _active_proposals(store)
    counts = Counter(str(item.get("status") or "") for item in claims.values())
    logs = store.read_log()
    jobs = sorted(job for job, vault in _bindings(cfg).items() if vault == store.vault_id)
    gaps = sum(len(item.get("gaps") or []) for item in proposals)
    return KnowledgeVaultSummary(
        store.vault_id,
        store.root,
        jobs,
        {status.value: counts.get(status.value, 0) for status in KnowledgeStatus},
        len(sources),
        len(proposals),
        counts.get(KnowledgeStatus.DISPUTED.value, 0),
        gaps,
        str(logs[-1].get("at") or "") if logs else "",
        {
            "auto_ingest": cfg.knowledge_auto_ingest,
            "auto_compile": cfg.knowledge_auto_compile,
            "auto_lint": cfg.knowledge_auto_lint,
            "auto_verify": "never",
            "auto_destructive_edit": False,
            "auto_cross_vault": False,
        },
    )


def review_vault(
    job_id: str,
    cfg: BuildupConfig,
    proposal_id: str = "",
) -> str:
    store = _store_for_job(job_id, cfg)
    claims = _grounded_claims(store)
    all_proposals = store.read_proposals()
    rolled_back = _rolled_back(store.read_claim_events())
    proposals = _active_proposals(store)
    selected_proposal: Optional[Dict[str, Any]] = None
    selector = proposal_id.strip()
    if selector:
        if selector.lower() in {"latest", "최근", "마지막"}:
            selected_proposal = proposals[-1] if proposals else None
        else:
            exact = [
                item for item in all_proposals
                if str(item.get("proposal_id") or "") == selector
            ]
            prefix = [
                item for item in all_proposals
                if str(item.get("proposal_id") or "").startswith(selector)
            ]
            selected_proposal = exact[0] if exact else (prefix[0] if len(prefix) == 1 else None)
        if not selected_proposal:
            raise ValueError(f"proposal을 찾지 못했습니다: {selector}")
    first_seen: set[str] = set()
    change_kind: Dict[Tuple[str, str], str] = {}
    for event in store.read_claim_events():
        if event.get("event") != "claim_upsert":
            continue
        claim_id = str(event.get("claim_id") or "")
        pid = str(event.get("proposal_id") or "")
        if claim_id and pid:
            change_kind[(pid, claim_id)] = "+" if claim_id not in first_seen else "~"
            first_seen.add(claim_id)
    attention = [
        claim for claim in claims.values()
        if claim.get("status") in {
            KnowledgeStatus.DISPUTED.value,
            KnowledgeStatus.STALE.value,
            KnowledgeStatus.DRAFT.value,
        }
    ]
    lines = [
        f"vault: {store.vault_id}",
        f"active proposals: {len(proposals)}",
        f"claims needing review: {len(attention)}",
        "",
    ]
    for claim in sorted(attention, key=lambda item: str(item.get("claim_id") or "")):
        lines.append(
            f"- {rich_escape('[' + str(claim.get('status') or '') + ']')} "
            f"{claim.get('claim_id')}: "
            f"{rich_escape(str(claim.get('text') or ''))}"
        )
    if not attention:
        lines.append("검토가 필요한 disputed/stale/draft claim이 없습니다.")
    if proposals:
        lines.extend(["", "recent proposals:"])
        for item in proposals[-10:][::-1]:
            lines.append(
                f"- {item.get('proposal_id')} · run {item.get('run_id')} · "
                f"{len(item.get('claims') or [])} claims"
            )
    if selected_proposal:
        pid = str(selected_proposal.get("proposal_id") or "")
        lines.extend([
            "",
            f"proposal diff: {pid}",
            f"state: {'rolled-back' if pid in rolled_back else 'active'}",
            f"run: {selected_proposal.get('run_id')}",
            f"sources: {len(selected_proposal.get('sources') or [])}",
            f"open questions: {len(selected_proposal.get('gaps') or [])}",
            "",
        ])
        for item in selected_proposal.get("claims", []):
            if not isinstance(item, dict):
                continue
            claim_id = str(item.get("claim_id") or "")
            marker = change_kind.get((pid, claim_id), "?")
            conflict = " !conflict" if item.get("validated_status") == "disputed" else ""
            lines.append(
                f"{marker}{conflict} {claim_id}: "
                f"{rich_escape(str(item.get('text') or ''))}"
            )
    return "\n".join(lines)


def format_ingest_result(result: KnowledgeIngestResult) -> str:
    if result.duplicate:
        return (
            f"이미 반영된 research run입니다.\n"
            f"vault: {result.vault_id}\nproposal: {result.proposal_id}\n{result.vault_dir}"
        )
    return (
        f"Knowledge: +{result.claims_added} claims · "
        f"{result.claims_updated} updated · {result.conflicts} conflicts · "
        f"{result.stale_claims} stale · {result.invalid} invalid\n"
        f"vault: {result.vault_id}\nproposal: {result.proposal_id}\n{result.vault_dir}"
    )


def format_query_results(query: str, hits: Sequence[KnowledgeQueryHit]) -> str:
    if not hits:
        return "근거가 연결된 관련 claim을 찾지 못했습니다."
    lines = [
        f"저장된 grounded claims — {query}",
        "Wiki 자체가 아니라 아래 원본 URL·hash가 근거입니다.",
        "",
    ]
    for index, hit in enumerate(hits, start=1):
        lines.append(
            f"{index}. {rich_escape('[' + hit.status + ']')} "
            f"{rich_escape(hit.text)} · {hit.claim_id}"
        )
        for citation in hit.citations[:4]:
            raw_suffix = (
                f" · pdf-sha256:{citation['raw_sha256'][:12]}"
                if citation.get("raw_sha256") else ""
            )
            lines.append(
                f"   - {rich_escape(citation['title'])} · "
                f"{rich_escape(citation['url'])} · "
                f"sha256:{citation['content_sha256'][:12]} · "
                f"{citation['research_evidence_id']}{raw_suffix}"
            )
    return "\n".join(lines)


def format_lint_report(report: KnowledgeLintReport) -> str:
    lines = [
        f"vault: {report.vault_id}",
        f"status: {'PASS' if report.passed else 'FAIL'}",
        " · ".join(f"{key} {value}" for key, value in report.stats.items()),
    ]
    if report.repaired_stale:
        lines.append(f"stale repaired: {report.repaired_stale}")
    if report.errors:
        lines.extend([
            "", "errors:", *[f"- {rich_escape(item)}" for item in report.errors]
        ])
    if report.warnings:
        lines.extend([
            "", "warnings:",
            *[f"- {rich_escape(item)}" for item in report.warnings],
        ])
    return "\n".join(lines)


def format_vault_summary(summary: KnowledgeVaultSummary) -> str:
    statuses = " · ".join(
        f"{key} {value}" for key, value in summary.claims_by_status.items()
    )
    settings = " · ".join(
        f"{key}={value}" for key, value in summary.settings.items()
    )
    return (
        f"vault: {summary.vault_id}\n"
        f"jobs: {', '.join(summary.jobs) or '-'}\n"
        f"sources: {summary.source_versions} · proposals: {summary.proposals} · "
        f"conflicts: {summary.contradictions} · open questions: {summary.open_questions}\n"
        f"{statuses}\n{settings}\nlast event: {summary.last_event_at or '-'}\n"
        f"{summary.vault_dir}"
    )
