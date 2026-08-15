"""Compatibility facade for Build-up's adaptive deep-research engine."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from buildup.config import BuildupConfig
from buildup.jobs import append_action_log, cmd_job_new
from buildup.paths import slugify
from buildup.state import get_current_job, job_dir

from .research.engine import ResearchEngine, create_state
from .research.evaluation import evaluate_run
from .research.prompts import PLAN_SCHEMA
from .research.store import ResearchStore


# Stable artifact names retained for integrations. They now describe durable
# workflow boundaries instead of personas simulated inside one model call.
ROLE_KEYS = (
    "01_research_contract",
    "02_task_plan",
    "03_source_registry",
    "04_evidence_ledger",
    "05_claim_gap_audit",
    "06_final_report",
)

ROLE_FILES = {
    "01_research_contract": "01-contract.json",
    "02_task_plan": "02-plan.json",
    "03_source_registry": "03-sources.jsonl",
    "04_evidence_ledger": "04-evidence.jsonl",
    "05_claim_gap_audit": "05-claims.jsonl",
    "06_final_report": "06-report.md",
}

# Kept for import compatibility; planning is the first strict model contract.
DEEP_RESEARCH_SCHEMA = PLAN_SCHEMA


@dataclass(frozen=True)
class DeepResearchResult:
    query: str
    report: str
    run_dir: Path
    job_id: str
    engine: str
    source_count: int
    model: str
    model_calls: int
    role_files: Dict[str, Path] = field(default_factory=dict)
    study_guide_path: Optional[Path] = None
    metadata_path: Optional[Path] = None
    evidence_count: int = 0
    claim_count: int = 0
    rounds: int = 0
    coverage: float = 0.0
    resumed: bool = False
    knowledge_vault_path: Optional[Path] = None
    knowledge_proposal_id: str = ""
    knowledge_claims_added: int = 0
    knowledge_conflicts: int = 0
    knowledge_invalid: int = 0
    knowledge_error: str = ""


def run_deep_research(
    query: str,
    cfg: BuildupConfig,
    session: Any,
    logger: Any,
    *,
    job_id: Optional[str] = None,
    max_results: Optional[int] = None,
    search_fn: Optional[Callable[..., Tuple[List[Dict[str, Any]], str]]] = None,
    chat_fn: Optional[Callable[..., str]] = None,
    reader_fn: Optional[Callable[[str], Any]] = None,
    status: Optional[Callable[[str], None]] = None,
    now: Optional[datetime] = None,
    session_id: str = "",
    workspace_key: str = "",
    depth: str = "auto",
    resume: str = "",
    max_rounds: Optional[int] = None,
    max_searches: Optional[int] = None,
    max_sources: Optional[int] = None,
    ingest_knowledge: Optional[bool] = None,
) -> DeepResearchResult:
    """Run or resume the bounded adaptive research workflow."""
    clean_query = query.strip()
    selected_job = job_id
    selected_max_results = 5 if max_results is None else max_results
    if not resume:
        if not clean_query:
            raise ValueError("딥 리서치 주제가 비어 있습니다.")
        # Validate every caller-controlled policy before creating a job or run
        # directory. Invalid CLI/API input must never leave orphan artifacts.
        create_state(
            clean_query,
            "preflight",
            selected_job or "preflight",
            cfg,
            depth=depth,
            max_rounds=max_rounds,
            max_searches=max_searches,
            max_sources=max_sources,
            max_results_per_search=selected_max_results,
            session_id=session_id,
            workspace_key=workspace_key,
        )
    if not selected_job:
        selected_job = get_current_job(cfg, required=False)
    if not selected_job and resume:
        raise ValueError("research run을 재개하려면 job을 지정해야 합니다.")
    if not selected_job:
        selected_job, _ = cmd_job_new(
            f"research-{slugify(clean_query)[:28]}",
            cfg,
            template="research",
        )

    resumed = bool(resume)
    if resume:
        changed_policy = []
        if depth not in {"", "auto"}:
            changed_policy.append("depth")
        if max_rounds is not None:
            changed_policy.append("max_rounds")
        if max_searches is not None:
            changed_policy.append("max_searches")
        if max_sources is not None:
            changed_policy.append("max_sources")
        if max_results is not None:
            changed_policy.append("max_results_per_search")
        if changed_policy:
            raise ValueError(
                "재개 실행은 checkpoint에 저장된 research policy를 그대로 사용합니다. "
                "변경하려면 새 run을 시작하세요: " + ", ".join(changed_policy)
            )
        selected = resolve_research_run(resume, selected_job, cfg)
        if not selected:
            raise ValueError(f"재개할 research run을 찾지 못했습니다: {resume}")
        run_dir = Path(str(selected["run_dir"])).expanduser().resolve()
        store = ResearchStore(run_dir, selected_job, cfg)
        state = store.load()
        if clean_query and clean_query != state.query:
            raise ValueError("재개할 때 새 query로 기존 research contract를 바꿀 수 없습니다.")
        clean_query = state.query
    else:
        run_time = now or datetime.now().astimezone()
        run_dir = _unique_run_dir(
            job_dir(selected_job, cfg),
            run_time.strftime("%Y%m%d-%H%M%S"),
            clean_query,
        )
        store = ResearchStore(run_dir, selected_job, cfg)
        state = create_state(
            clean_query,
            run_dir.name,
            selected_job,
            cfg,
            depth=depth,
            max_rounds=max_rounds,
            max_searches=max_searches,
            max_sources=max_sources,
            max_results_per_search=selected_max_results,
            session_id=session_id,
            workspace_key=workspace_key,
        )
        store.initialize()
        store.checkpoint(state, event="initial_checkpoint")

    engine = ResearchEngine(
        cfg,
        session,
        logger,
        store,
        state,
        search_fn=search_fn,
        chat_fn=chat_fn,
        reader_fn=reader_fn,
        status=status,
        max_results_per_search=None,
    )
    completed = engine.run()
    state = completed.state
    role_files = {
        key: run_dir / filename
        for key, filename in ROLE_FILES.items()
        if (run_dir / filename).is_file()
    }
    try:
        report_relpath = (run_dir / "06-report.md").resolve().relative_to(
            job_dir(selected_job, cfg).resolve()
        )
        append_action_log(
            selected_job,
            cfg,
            "deep_research_resume" if resumed else "deep_research",
            f"{clean_query} → {report_relpath}",
        )
    except Exception as exc:  # result artifacts are authoritative; audit logging is best-effort
        if logger is not None:
            logger.warning("Research completed but action logging failed: %s", exc)
    knowledge_result = None
    knowledge_error = ""
    knowledge_enabled = (
        cfg.knowledge_auto_ingest if ingest_knowledge is None else ingest_knowledge
    )
    if knowledge_enabled:
        try:
            from buildup.knowledge import auto_ingest_research_run

            knowledge_result = auto_ingest_research_run(run_dir, selected_job, cfg)
        except Exception as exc:
            # The sealed research run is the source of truth. A derived-view
            # failure must be visible but must never invalidate completed work.
            knowledge_error = f"{type(exc).__name__}: {exc}"
            if logger is not None:
                logger.warning("Research completed but knowledge ingestion failed: %s", exc)
    return DeepResearchResult(
        query=clean_query,
        report=completed.report_path.read_text(encoding="utf-8"),
        run_dir=run_dir,
        job_id=selected_job,
        engine=", ".join(state.provider_names) or "unavailable",
        source_count=len(state.sources),
        evidence_count=len(state.evidence),
        claim_count=len(state.claims),
        rounds=state.round,
        coverage=state.coverage,
        model=cfg.research_model,
        model_calls=state.budget.model_calls_used,
        role_files=role_files,
        study_guide_path=completed.study_guide_path,
        metadata_path=run_dir / "metadata.json",
        resumed=resumed,
        knowledge_vault_path=knowledge_result.vault_dir if knowledge_result else None,
        knowledge_proposal_id=knowledge_result.proposal_id if knowledge_result else "",
        knowledge_claims_added=knowledge_result.claims_added if knowledge_result else 0,
        knowledge_conflicts=knowledge_result.conflicts if knowledge_result else 0,
        knowledge_invalid=knowledge_result.invalid if knowledge_result else 0,
        knowledge_error=knowledge_error,
    )


def list_research_runs(job_id: str, cfg: BuildupConfig) -> List[Dict[str, Any]]:
    root = job_dir(job_id, cfg) / "deep-research"
    if not root.exists():
        return []
    root_resolved = root.resolve()
    runs: List[Dict[str, Any]] = []
    for path in root.glob("*/metadata.json"):
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if path.is_symlink() or not resolved.is_relative_to(root_resolved):
            continue
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        value = dict(payload)
        value["run_dir"] = str(resolved.parent)
        runs.append(value)
    return sorted(
        runs,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )


def resolve_research_run(
    selector: str,
    job_id: str,
    cfg: BuildupConfig,
) -> Optional[Dict[str, Any]]:
    runs = list_research_runs(job_id, cfg)
    if not runs:
        return None
    value = selector.strip()
    if not value or value.lower() in {"latest", "최근", "마지막"}:
        return runs[0]
    if value.isdigit():
        index = int(value)
        return runs[index - 1] if 1 <= index <= len(runs) else None
    exact = [item for item in runs if item.get("run_id") == value]
    if exact:
        return exact[0]
    matches = [item for item in runs if str(item.get("run_id") or "").startswith(value)]
    return matches[0] if len(matches) == 1 else None


def latest_completed_research_run(
    job_id: str,
    cfg: BuildupConfig,
) -> Optional[Dict[str, Any]]:
    """Return the newest run that passed every release quality gate."""
    for run in list_research_runs(job_id, cfg):
        if run.get("status") != "completed":
            continue
        run_dir = Path(str(run.get("run_dir") or ""))
        try:
            store = ResearchStore(run_dir, job_id, cfg)
            state = store.load()
            if (
                state.status != "completed"
                or not state.citation_audit.get("passed")
                or not state.claim_audit.get("passed")
                or store.manifest_failures(state)
                or not (run_dir / "06-report.md").is_file()
                or not (run_dir / "07-study-guide.md").is_file()
            ):
                continue
        except (OSError, TypeError, ValueError):
            continue
        return run
    return None


def evaluate_research_run(
    selector: str,
    job_id: str,
    cfg: BuildupConfig,
) -> Dict[str, Any]:
    selected = resolve_research_run(selector, job_id, cfg)
    if not selected:
        raise ValueError(f"평가할 research run을 찾지 못했습니다: {selector}")
    result = evaluate_run(Path(str(selected["run_dir"])))
    ResearchStore(Path(str(selected["run_dir"])), job_id, cfg).write_evaluation(result)
    return result


def format_research_runs(runs: List[Dict[str, Any]]) -> str:
    if not runs:
        return "현재 job에 research run이 없습니다. /research 주제로 시작하세요."
    lines: List[str] = []
    for index, run in enumerate(runs, start=1):
        citation = run.get("citation_stats") if isinstance(run.get("citation_stats"), dict) else {}
        lines.append(
            f"{index}. {run.get('query', '')} · {run.get('status', 'unknown')} · "
            f"{run.get('depth', '?')}\n"
            f"   {run.get('run_id', '')} · rounds {run.get('round', 0)} · "
            f"sources {run.get('source_count', 0)} · evidence {run.get('evidence_count', 0)} · "
            f"coverage {float(run.get('coverage') or 0):.2f} · "
            f"citations {citation.get('citation_count', 0)}"
        )
    return "\n".join(lines)


def _unique_run_dir(base: Path, stamp: str, query: str) -> Path:
    root = base / "deep-research"
    stem = f"{stamp}-{slugify(query)[:48]}"
    candidate = root / stem
    suffix = 2
    while candidate.exists():
        candidate = root / f"{stem}-{suffix}"
        suffix += 1
    return candidate
