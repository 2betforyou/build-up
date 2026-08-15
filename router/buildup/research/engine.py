"""Adaptive, checkpointed, evidence-first deep research orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from buildup.config import BuildupConfig

from .citations import (
    append_used_sources,
    audit_report,
    normalize_text,
    passage_sha256,
    verify_exact_passage,
)
from .models import (
    Claim,
    Evidence,
    ResearchBudget,
    ResearchBudgetExceeded,
    ResearchContract,
    ResearchDepth,
    ResearchGap,
    ResearchState,
    ResearchTask,
    RunStatus,
    Source,
    TaskStatus,
    now_iso,
    stable_id,
)
from .prompts import (
    ASSESSMENT_SCHEMA,
    AUDIT_SCHEMA,
    EVIDENCE_SCHEMA,
    PLAN_SCHEMA,
    WRITER_SCHEMA,
    assessment_messages,
    audit_messages,
    evidence_messages,
    plan_messages,
    writer_messages,
)
from .providers import SearchBroker
from .reader import ReaderResult, SafeWebReader, is_public_web_url, normalize_url
from .store import ResearchStore


SearchFn = Callable[..., Tuple[List[Dict[str, Any]], str]]
ChatFn = Callable[..., str]
ReaderFn = Callable[[str], Any]
StatusFn = Callable[[str], None]


class ResearchQualityError(RuntimeError):
    pass


class ResearchModelResponseError(ValueError):
    pass


@dataclass(frozen=True)
class DepthProfile:
    depth: ResearchDepth
    initial_tasks: int
    queries_per_task: int
    min_coverage: float
    max_rounds: int
    max_sources: int


@dataclass(frozen=True)
class EngineResult:
    state: ResearchState
    run_dir: Path
    report_path: Path
    study_guide_path: Path


def choose_depth(query: str, requested: str, cfg: BuildupConfig) -> ResearchDepth:
    depth = ResearchDepth.parse(requested)
    if depth is not ResearchDepth.AUTO:
        return depth
    clean = query.lower()
    complex_markers = (
        "비교", "분석", "설계", "동향", "시장", "법", "정책", "최신", "근거",
        "trade-off", "architecture", "survey", "systematic", "benchmark", "landscape",
    )
    shallow_markers = ("정의", "무엇", "간단히", "요약", "what is", "quick overview")
    score = sum(marker in clean for marker in complex_markers)
    if len(clean) > 180:
        score += 2
    if any(marker in clean for marker in shallow_markers) and len(clean) < 80 and score == 0:
        return ResearchDepth.SHALLOW
    return ResearchDepth.DEEP if score >= 3 else ResearchDepth.STANDARD


def depth_profile(depth: ResearchDepth, cfg: BuildupConfig) -> DepthProfile:
    if depth is ResearchDepth.SHALLOW:
        return DepthProfile(
            depth,
            initial_tasks=2,
            queries_per_task=1,
            min_coverage=min(cfg.research_min_coverage, 0.72),
            max_rounds=min(cfg.research_max_rounds, 1),
            max_sources=min(cfg.research_max_sources, 10),
        )
    if depth is ResearchDepth.DEEP:
        return DepthProfile(
            depth,
            initial_tasks=min(6, cfg.research_max_tasks),
            queries_per_task=3,
            min_coverage=max(cfg.research_min_coverage, 0.90),
            max_rounds=cfg.research_max_rounds,
            max_sources=cfg.research_max_sources,
        )
    return DepthProfile(
        depth,
        initial_tasks=min(4, cfg.research_max_tasks),
        queries_per_task=2,
        min_coverage=cfg.research_min_coverage,
        max_rounds=min(cfg.research_max_rounds, 2),
        max_sources=min(cfg.research_max_sources, 20),
    )


def create_state(
    query: str,
    run_id: str,
    job_id: str,
    cfg: BuildupConfig,
    *,
    depth: str = "auto",
    max_rounds: Optional[int] = None,
    max_searches: Optional[int] = None,
    max_sources: Optional[int] = None,
    max_results_per_search: int = 5,
    session_id: str = "",
    workspace_key: str = "",
) -> ResearchState:
    selected_max_results = _max_results_limit(max_results_per_search)
    selected = choose_depth(query, depth or cfg.research_default_depth, cfg)
    profile = depth_profile(selected, cfg)
    budget = ResearchBudget(
        max_rounds=_budget_limit("max_rounds", max_rounds, profile.max_rounds),
        max_tasks=max(1, cfg.research_max_tasks),
        max_searches=_budget_limit(
            "max_searches", max_searches, cfg.research_max_searches
        ),
        max_sources=_budget_limit("max_sources", max_sources, profile.max_sources),
        max_model_calls=max(1, cfg.research_max_model_calls),
        max_runtime_seconds=cfg.research_max_runtime_seconds,
    )
    return ResearchState(
        run_id=run_id,
        job_id=job_id,
        query=query,
        depth=selected.value,
        budget=budget,
        target_coverage=profile.min_coverage,
        initial_task_limit=profile.initial_tasks,
        queries_per_task=profile.queries_per_task,
        max_results_per_search=selected_max_results,
        session_id=session_id,
        workspace_key=workspace_key or f"job:{job_id}",
    )


class ResearchEngine:
    def __init__(
        self,
        cfg: BuildupConfig,
        session: Any,
        logger: Optional[logging.Logger],
        store: ResearchStore,
        state: ResearchState,
        *,
        search_fn: Optional[SearchFn] = None,
        chat_fn: Optional[ChatFn] = None,
        reader_fn: Optional[ReaderFn] = None,
        status: Optional[StatusFn] = None,
        max_results_per_search: Optional[int] = None,
    ):
        self.cfg = cfg
        self.session = session
        self.logger = logger or logging.getLogger("buildup.research")
        self.store = store
        self.state = state
        self.search_fn = search_fn
        self.chat_fn = chat_fn
        self.reader_fn = reader_fn
        self.status = status or (lambda _: None)
        if (
            max_results_per_search is not None
            and int(max_results_per_search) != state.max_results_per_search
        ):
            raise ValueError(
                "max_results_per_search does not match the persisted research policy"
            )
        self.max_results_per_search = state.max_results_per_search
        derived_profile = depth_profile(ResearchDepth.parse(state.depth), cfg)
        self.profile = DepthProfile(
            depth=derived_profile.depth,
            initial_tasks=min(state.initial_task_limit, state.budget.max_tasks),
            queries_per_task=state.queries_per_task,
            min_coverage=state.target_coverage,
            max_rounds=state.budget.max_rounds,
            max_sources=state.budget.max_sources,
        )
        self._segment_started = time.monotonic()

    def run(self) -> EngineResult:
        with self.store.lease():
            if self.state.status == RunStatus.COMPLETED.value:
                return self._completed_result()
            self.state.status = RunStatus.RUNNING.value
            self._reset_resumable_tasks()
            self.store.checkpoint(self.state, event="run_started_or_resumed")
            try:
                if not self.state.contract:
                    self._plan()
                if not self.state.report:
                    self._research_until_stopped()
                    self._write_and_audit()
                elif not (
                    self.state.citation_audit.get("passed")
                    and self.state.claim_audit.get("passed")
                ):
                    self._write_and_audit()
                self.state.phase = "finalizing"
                guide_path = self.store.write_study_guide(self._study_guide())
                self.state.status = RunStatus.COMPLETED.value
                self.state.phase = "completed"
                self.state.stop_reason = self.state.stop_reason or "quality gates passed"
                self._update_runtime()
                self.store.checkpoint(self.state, event="run_completed")
                self.store.write_manifest(self.state)
                return EngineResult(
                    state=self.state,
                    run_dir=self.store.run_dir,
                    report_path=self.store.run_dir / "06-report.md",
                    study_guide_path=guide_path,
                )
            except KeyboardInterrupt:
                self.state.status = RunStatus.INTERRUPTED.value
                self.state.stop_reason = "interrupted by user"
                self._update_runtime()
                self.store.checkpoint(self.state, event="run_interrupted")
                raise
            except Exception as exc:
                self.state.status = RunStatus.FAILED.value
                self.state.errors.append({
                    "at": now_iso(),
                    "phase": self.state.phase,
                    "type": type(exc).__name__,
                    "error": str(exc),
                })
                self._update_runtime()
                self.store.checkpoint(self.state, event="run_failed")
                raise

    def _plan(self) -> None:
        self._assert_runtime()
        self.state.phase = "planning"
        self.status("[1/6] 연구 계약과 독립 조사 과제를 설계하는 중...")
        self.store.checkpoint(self.state, event="planning_started")
        payload = self._call_json(
            "planning",
            self.cfg.research_model,
            plan_messages(self.state.query, self.state.depth, self.profile.initial_tasks),
            PLAN_SCHEMA,
        )
        contract_data = _require_dict(payload, "contract")
        contract = ResearchContract.from_dict(contract_data, fallback_question=self.state.query)
        if not contract.question or not contract.objective:
            raise ResearchModelResponseError("research contract question/objective가 비어 있습니다.")
        task_rows = _require_list(payload, "tasks")
        tasks: List[ResearchTask] = []
        seen: set[str] = set()
        for row in task_rows:
            if not isinstance(row, dict):
                continue
            task = ResearchTask.from_dict(row)
            key = normalize_text(task.question).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            task.id = stable_id("T", task.question)
            task.queries = _unique(task.queries)[: self.profile.queries_per_task]
            task.round_created = 1
            tasks.append(task)
            if len(tasks) >= min(self.profile.initial_tasks, self.state.budget.max_tasks):
                break
        if not tasks:
            tasks = [ResearchTask(
                id=stable_id("T", self.state.query),
                question=self.state.query,
                queries=[self.state.query],
                rationale="direct fallback task for the research contract",
                critical=True,
            )]
        self.state.contract = contract
        self.state.tasks = tasks
        self.state.phase = "planned"
        self.store.checkpoint(self.state, event="planning_completed")

    def _research_until_stopped(self) -> None:
        while True:
            self._assert_runtime()
            if self.state.phase == "gap_analysis":
                # A previous process stopped during the assessment model call.
                self._assess_round()
                continue
            if self.state.phase == "round_assessed":
                if self._research_is_sufficient():
                    self.state.stop_reason = "coverage and critical-claim gates reached"
                    break
                if self.state.round >= self.state.budget.max_rounds:
                    self.state.stop_reason = "maximum research rounds reached"
                    break
                added = self._add_gap_tasks()
                if not added:
                    self.state.stop_reason = "no actionable gaps within remaining budget"
                    break
                self.state.phase = "followup_planned"
                self.store.checkpoint(self.state, event="followup_tasks_added")
                continue

            continuing_round = self.state.phase in {
                "retrieval", "evidence_extraction", "task_failed", "task_started"
            }
            if continuing_round and self.state.round > 0:
                pending = [
                    task for task in self.state.tasks
                    if task.status == TaskStatus.PENDING.value
                    and task.round_created <= self.state.round
                ]
            else:
                if self.state.round >= self.state.budget.max_rounds:
                    self.state.stop_reason = "maximum research rounds reached"
                    break
                pending = [
                    task for task in self.state.tasks
                    if task.status == TaskStatus.PENDING.value
                    and task.round_created <= self.state.round + 1
                ]
                if pending:
                    self.state.round += 1
                    self.state.phase = "retrieval"
                    self.status(
                        f"[2/6] 연구 라운드 {self.state.round}/{self.state.budget.max_rounds}: "
                        f"{len(pending)}개 과제를 조사하는 중..."
                    )
                    self.store.checkpoint(self.state, event="round_started")
            if not pending:
                self.state.stop_reason = self.state.stop_reason or "no pending research tasks"
                break
            for task in sorted(pending, key=lambda item: (not item.critical, -item.priority, item.id)):
                if not self._can_process_task():
                    task.status = TaskStatus.BLOCKED.value
                    task.error = "budget reserved for synthesis and audit"
                    continue
                try:
                    self._process_task(task)
                except ResearchBudgetExceeded:
                    raise
                except Exception as exc:
                    task.status = TaskStatus.FAILED.value
                    task.error = f"{type(exc).__name__}: {exc}"
                    self.state.phase = "task_failed"
                    self.state.errors.append({
                        "at": now_iso(),
                        "phase": "task",
                        "task_id": task.id,
                        "type": type(exc).__name__,
                        "error": str(exc),
                    })
                    self.store.checkpoint(self.state, event="task_failed")
            if not self.state.evidence:
                if self.state.round >= self.state.budget.max_rounds or not self.state.budget.can_search():
                    raise ResearchQualityError("검증 가능한 원문 근거를 수집하지 못했습니다.")
                self._add_empty_evidence_followups()
                self.state.phase = "followup_planned"
                self.store.checkpoint(self.state, event="empty_evidence_followup_added")
                continue

            self._assess_round()

        if not self.state.claims:
            raise ResearchQualityError("근거에서 지지 가능한 주장을 구성하지 못했습니다.")
        self.state.phase = "research_complete"
        self.store.checkpoint(self.state, event="research_stopped")

    def _process_task(self, task: ResearchTask) -> None:
        task.status = TaskStatus.RUNNING.value
        task.attempts += 1
        task.error = ""
        self.store.checkpoint(self.state, event="task_started")
        rows = self._search_task(task)
        task_sources = self._ingest_sources(task, rows)
        if not task_sources:
            task.status = TaskStatus.BLOCKED.value
            task.error = "no readable public sources"
            self.store.checkpoint(self.state, event="task_blocked")
            return
        self.state.phase = "evidence_extraction"
        payload = self._call_json(
            f"evidence-{task.id}",
            self.cfg.research_model,
            evidence_messages(task, task_sources, self.cfg.research_source_chars),
            EVIDENCE_SCHEMA,
        )
        accepted = self._accept_evidence(task, payload, task_sources)
        task.status = TaskStatus.COMPLETED.value if accepted else TaskStatus.BLOCKED.value
        task.error = "" if accepted else "model returned no exact, valid evidence passages"
        self.store.checkpoint(self.state, event="task_completed" if accepted else "task_blocked")

    def _search_task(self, task: ResearchTask) -> List[Tuple[Dict[str, Any], str]]:
        queries = _unique(task.queries or [task.question])[: self.profile.queries_per_task]
        scheduled: List[str] = []
        for query in queries:
            if not self.state.budget.can_search():
                break
            self.state.budget.consume_search()
            scheduled.append(query)
        self.store.checkpoint(self.state, event="search_batch_scheduled")
        if not scheduled:
            return []
        results_by_query: Dict[str, Tuple[List[Dict[str, Any]], str]] = {}
        errors_by_query: Dict[str, Dict[str, Any]] = {}
        workers = min(max(1, self.cfg.research_max_concurrency), len(scheduled))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="buildup-search") as pool:
            futures = {pool.submit(self._perform_search, query): query for query in scheduled}
            for future in as_completed(futures):
                query = futures[future]
                try:
                    rows, provider = future.result()
                    results_by_query[query] = (rows, provider)
                except Exception as exc:
                    errors_by_query[query] = {
                        "at": now_iso(),
                        "phase": "search",
                        "query": query,
                        "type": type(exc).__name__,
                        "error": str(exc),
                    }
        results: List[Tuple[Dict[str, Any], str]] = []
        for query in scheduled:
            if query in errors_by_query:
                self.state.errors.append(errors_by_query[query])
            rows, provider = results_by_query.get(query, ([], ""))
            if provider and provider not in self.state.provider_names:
                self.state.provider_names.append(provider)
            for row in rows:
                value = dict(row)
                value.setdefault("query", query)
                results.append((value, provider))
        return results

    def _perform_search(self, query: str) -> Tuple[List[Dict[str, Any]], str]:
        if self.search_fn:
            rows, provider = self.search_fn(
                query, self.cfg, self.session, max_results=self.max_results_per_search
            )
            return list(rows), str(provider)
        with requests.Session() as network_session:
            response = SearchBroker(self.cfg, session=network_session).search(
                query, max_results=self.max_results_per_search
            )
        return [item.legacy_dict() for item in response.results], response.provider

    def _ingest_sources(
        self,
        task: ResearchTask,
        rows: Sequence[Tuple[Dict[str, Any], str]],
    ) -> List[Source]:
        by_url = {item.normalized_url: item for item in self.state.sources}
        accepted: List[Source] = []
        accepted_ids: set[str] = set()
        seen_for_task: set[str] = set()
        for row, provider in rows:
            raw_url = str(row.get("href") or row.get("url") or "").strip()
            if not is_public_web_url(raw_url):
                continue
            try:
                url = normalize_url(raw_url)
            except ValueError:
                continue
            if self.search_fn is None and self.reader_fn is None:
                if not is_public_web_url(url, resolve_dns=True):
                    self.state.errors.append({
                        "at": now_iso(),
                        "phase": "source_validation",
                        "task_id": task.id,
                        "url": url,
                        "error": "source hostname did not resolve exclusively to public addresses",
                    })
                    continue
            if url in seen_for_task:
                continue
            seen_for_task.add(url)
            existing = by_url.get(url)
            if existing:
                if existing.id not in accepted_ids:
                    accepted.append(existing)
                    accepted_ids.add(existing.id)
                if existing.id not in task.source_ids:
                    task.source_ids.append(existing.id)
                continue
            if len(self.state.sources) >= self.state.budget.max_sources:
                break

            snippet = str(row.get("body") or row.get("content") or "").strip()
            content = str(row.get("raw_content") or "").strip()
            title = str(row.get("title") or "Untitled").strip()
            content_type = str(row.get("content_type") or "")
            metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
            final_url = url
            read_result: Optional[ReaderResult] = None
            requires_raw_pdf = (
                content_type == "application/pdf"
                or urlparse(url).path.lower().endswith(".pdf")
            )
            if len(normalize_text(content)) < 160 or requires_raw_pdf:
                try:
                    read = self._read_url(url)
                    requires_raw_pdf = requires_raw_pdf or (
                        read.content_type == "application/pdf"
                        or urlparse(read.final_url).path.lower().endswith(".pdf")
                    )
                    if requires_raw_pdf and not read.raw_bytes:
                        raise ValueError(
                            "PDF evidence requires exact original bytes for archival"
                        )
                    read_result = read
                    content = read.content
                    title = read.title or title
                    content_type = read.content_type
                    final_url = read.final_url
                    metadata.update(read.metadata)
                    metadata["reader_content_sha256"] = read.content_sha256
                except Exception as exc:
                    if requires_raw_pdf:
                        content = ""
                    metadata["reader_error"] = f"{type(exc).__name__}: {exc}"
                    self.state.errors.append({
                        "at": now_iso(),
                        "phase": "reader",
                        "task_id": task.id,
                        "url": url,
                        "type": type(exc).__name__,
                        "error": str(exc),
                    })
            # Search-result snippets are discovery metadata, not source text.
            # Only provider-supplied raw page content or reader output may enter
            # the exact-evidence ledger.
            content = normalize_text(content)[: self.cfg.research_source_chars]
            if len(content) < 80:
                continue
            normalized_final = normalize_url(final_url)
            existing = by_url.get(normalized_final)
            if existing:
                if existing.id not in accepted_ids:
                    accepted.append(existing)
                    accepted_ids.add(existing.id)
                if existing.id not in task.source_ids:
                    task.source_ids.append(existing.id)
                by_url[url] = existing
                continue
            source_id = stable_id("S", normalized_final)
            if read_result and read_result.raw_bytes:
                raw_sha256 = hashlib.sha256(read_result.raw_bytes).hexdigest()
                if read_result.raw_sha256 and read_result.raw_sha256 != raw_sha256:
                    raise ValueError("reader raw PDF hash does not match its bytes")
                raw_path = self.store.archive_pdf_source(
                    source_id, read_result.raw_bytes
                )
                metadata.update({
                    "raw_artifact": str(raw_path.relative_to(self.store.run_dir)),
                    "raw_sha256": raw_sha256,
                    "raw_bytes": len(read_result.raw_bytes),
                })
            source = Source(
                id=source_id,
                url=normalized_final,
                normalized_url=normalized_final,
                title=title[:500],
                provider=provider or str(row.get("provider") or "unknown"),
                search_query=str(row.get("query") or ""),
                retrieved_at=now_iso(),
                snippet=snippet[:4000],
                content=content,
                content_type=content_type,
                source_type=_source_type(normalized_final, content_type),
                author=str(row.get("author") or ""),
                published_at=str(row.get("published_at") or ""),
                content_sha256=_sha256_text(content),
                metadata=metadata,
            )
            self.state.sources.append(source)
            by_url[source.normalized_url] = source
            by_url[url] = source
            accepted.append(source)
            accepted_ids.add(source.id)
            task.source_ids.append(source.id)
        return accepted

    def _read_url(self, url: str) -> ReaderResult:
        if self.reader_fn:
            value = self.reader_fn(url)
            if isinstance(value, ReaderResult):
                return value
            if isinstance(value, str):
                content = value
                return ReaderResult(
                    url=url,
                    final_url=url,
                    title=urlparse(url).hostname or url,
                    content=content,
                    content_type="text/plain",
                    retrieved_at=now_iso(),
                    content_sha256=_sha256_text(content),
                    locator_kind="text",
                    metadata={"injected": True},
                )
            if isinstance(value, dict):
                content = str(value.get("content") or value.get("raw_content") or "")
                return ReaderResult(
                    url=url,
                    final_url=str(value.get("final_url") or url),
                    title=str(value.get("title") or urlparse(url).hostname or url),
                    content=content,
                    content_type=str(value.get("content_type") or "text/plain"),
                    retrieved_at=str(value.get("retrieved_at") or now_iso()),
                    content_sha256=str(value.get("content_sha256") or _sha256_text(content)),
                    locator_kind=str(value.get("locator_kind") or "text"),
                    metadata=dict(value.get("metadata") or {}),
                )
            raise TypeError("reader_fn must return ReaderResult, dict, or str")
        with requests.Session() as network_session:
            return SafeWebReader(self.cfg, session=network_session).read(url)

    def _accept_evidence(
        self,
        task: ResearchTask,
        payload: Dict[str, Any],
        task_sources: Sequence[Source],
    ) -> int:
        sources = {item.id: item for item in task_sources}
        known = {item.id for item in self.state.evidence}
        accepted = 0
        for row in _require_list(payload, "evidence")[:8]:
            if not isinstance(row, dict):
                continue
            source_id = str(row.get("source_id") or "")
            passage = str(row.get("passage") or "").strip()
            source = sources.get(source_id)
            if not source:
                self._record_rejected_evidence(task, source_id, "unknown source ID")
                continue
            if len(passage) > self.cfg.research_evidence_passage_chars:
                self._record_rejected_evidence(task, source_id, "passage exceeds configured length limit")
                continue
            match = verify_exact_passage(source.content, passage)
            if not match.valid:
                self._record_rejected_evidence(task, source_id, match.reason)
                continue
            identifier = stable_id("E", source_id, match.normalized_passage, length=14)
            if identifier in known:
                if identifier not in task.evidence_ids:
                    task.evidence_ids.append(identifier)
                continue
            stance = str(row.get("stance") or "context")
            if stance not in {"supports", "contradicts", "context"}:
                stance = "context"
            item = Evidence(
                id=identifier,
                source_id=source_id,
                task_id=task.id,
                passage=passage[: self.cfg.research_evidence_passage_chars],
                locator=str(row.get("locator") or f"normalized offset {match.normalized_offset}"),
                stance=stance,
                claim_hint=str(row.get("claim_hint") or "").strip(),
                relevance=_float01(row.get("relevance"), 0.5),
                credibility=_float01(row.get("credibility"), _source_credibility(source.source_type)),
                extracted_at=now_iso(),
                passage_sha256=passage_sha256(passage),
                verified_exact=True,
                notes=str(row.get("notes") or "").strip(),
            )
            self.state.evidence.append(item)
            known.add(identifier)
            task.evidence_ids.append(identifier)
            accepted += 1
        return accepted

    def _record_rejected_evidence(self, task: ResearchTask, source_id: str, reason: str) -> None:
        self.state.errors.append({
            "at": now_iso(),
            "phase": "evidence_validation",
            "task_id": task.id,
            "source_id": source_id,
            "error": reason,
        })

    def _assess_round(self) -> None:
        self._assert_runtime()
        self.state.phase = "gap_analysis"
        self.status(f"[3/6] 라운드 {self.state.round} 근거의 충돌·누락·커버리지를 감사하는 중...")
        payload = self._call_json(
            f"assessment-round-{self.state.round}",
            self.cfg.research_model,
            assessment_messages(
                self.state.contract or ResearchContract(self.state.query, self.state.query),
                self.state.tasks,
                self.state.evidence,
                self.state.sources,
                self.state.round,
                self.cfg.research_source_chars,
            ),
            ASSESSMENT_SCHEMA,
        )
        evidence_by_id = {
            item.id: item for item in self.state.evidence if item.verified_exact
        }
        claims: List[Claim] = []
        for row in _require_list(payload, "claims"):
            if not isinstance(row, dict):
                continue
            item = Claim.from_dict(row)
            if not item.text:
                continue
            item.evidence_ids = [
                value
                for value in item.evidence_ids
                if value in evidence_by_id and evidence_by_id[value].stance == "supports"
            ]
            item.contradicting_evidence_ids = [
                value
                for value in item.contradicting_evidence_ids
                if value in evidence_by_id and evidence_by_id[value].stance == "contradicts"
            ]
            if not item.evidence_ids:
                item.status = "unsupported"
                item.confidence = 0.0
            elif item.contradicting_evidence_ids:
                item.status = "contested"
            else:
                item.status = "supported"
            item.id = stable_id("C", item.text, length=14)
            claims.append(item)
        self.state.claims = _dedupe_claims(claims)
        self.state.coverage = min(
            _float01(payload.get("coverage"), 0.0),
            self._task_coverage(),
        )
        self._merge_gaps(payload)
        if bool(payload.get("sufficient")):
            self.state.stop_reason = str(
                payload.get("stop_reason") or "assessor reported sufficient coverage"
            )
        self.state.phase = "round_assessed"
        self.store.checkpoint(self.state, event="round_assessed")

    def _merge_gaps(self, payload: Dict[str, Any]) -> None:
        current_rows = [row for row in _require_list(payload, "gaps") if isinstance(row, dict)]
        current_keys = {
            normalize_text(str(row.get("question") or "")).lower() for row in current_rows
        }
        for old in self.state.gaps:
            if old.status == "open" and normalize_text(old.question).lower() not in current_keys:
                old.status = "resolved"
        existing = {normalize_text(item.question).lower(): item for item in self.state.gaps}
        for row in current_rows:
            gap = ResearchGap.from_dict({**row, "round_created": self.state.round})
            key = normalize_text(gap.question).lower()
            if not key:
                continue
            if key in existing:
                prior = existing[key]
                prior.reason = gap.reason
                prior.importance = gap.importance
                prior.suggested_queries = gap.suggested_queries
                prior.status = "open"
            else:
                self.state.gaps.append(gap)
                existing[key] = gap

    def _research_is_sufficient(self) -> bool:
        unsupported_critical_tasks = [
            item
            for item in self.state.tasks
            if item.critical
            and (
                item.status != TaskStatus.COMPLETED.value
                or not item.evidence_ids
            )
        ]
        unsupported_critical = [
            item for item in self.state.claims if item.critical and not item.evidence_ids
        ]
        material_gaps = [
            item for item in self.state.gaps if item.status == "open" and item.importance >= 4
        ]
        return (
            self.state.coverage >= self.profile.min_coverage
            and not unsupported_critical_tasks
            and not unsupported_critical
            and not material_gaps
        )

    def _task_coverage(self) -> float:
        tasks = [item for item in self.state.tasks if item.round_created <= self.state.round]
        if not tasks:
            return 0.0
        valid_evidence = {item.id for item in self.state.evidence if item.verified_exact}
        total = sum(2 if item.critical else 1 for item in tasks)
        covered = sum(
            2 if item.critical else 1
            for item in tasks
            if item.status == TaskStatus.COMPLETED.value
            and valid_evidence.intersection(item.evidence_ids)
        )
        return round(covered / total, 3) if total else 0.0

    def _add_gap_tasks(self) -> int:
        room = self.state.budget.max_tasks - len(self.state.tasks)
        if room <= 0 or not self.state.budget.can_search():
            return 0
        task_questions = {normalize_text(item.question).lower() for item in self.state.tasks}
        added = 0
        for gap in sorted(
            (item for item in self.state.gaps if item.status == "open"),
            key=lambda item: (-item.importance, item.id),
        ):
            key = normalize_text(gap.question).lower()
            if not key or key in task_questions:
                continue
            task = ResearchTask(
                id=stable_id("T", gap.id, gap.question),
                question=gap.question,
                queries=_unique(gap.suggested_queries or [gap.question])[
                    : self.profile.queries_per_task
                ],
                rationale=f"follow-up for gap {gap.id}: {gap.reason}",
                priority=gap.importance,
                critical=gap.importance >= 4,
                parent_gap_id=gap.id,
                round_created=self.state.round + 1,
            )
            self.state.tasks.append(task)
            task_questions.add(key)
            added += 1
            if added >= room:
                break
        return added

    def _add_empty_evidence_followups(self) -> None:
        if len(self.state.tasks) >= self.state.budget.max_tasks:
            return
        question = f"Find a primary or official source that directly answers: {self.state.query}"
        if any(item.question == question for item in self.state.tasks):
            return
        self.state.tasks.append(ResearchTask(
            id=stable_id("T", question),
            question=question,
            queries=[f'"{self.state.query}" official documentation evidence'],
            rationale="No exact evidence was accepted in the preceding round",
            priority=5,
            critical=True,
            round_created=self.state.round + 1,
        ))

    def _write_and_audit(self) -> None:
        self._assert_runtime()
        if not self.state.report:
            self.state.phase = "writing"
            self.status("[4/6] 검증된 주장과 근거 원장만으로 보고서를 작성하는 중...")
            self.state.writer_attempts += 1
            payload = self._call_json(
                "writer",
                self.cfg.research_model,
                writer_messages(
                    self.state.contract or ResearchContract(self.state.query, self.state.query),
                    self.state.claims,
                    self.state.gaps,
                    self.state.evidence,
                    self.state.sources,
                    self.cfg.research_source_chars,
                ),
                WRITER_SCHEMA,
            )
            self._accept_draft(payload)
            self._audit_draft()
        elif not self.state.claim_audit:
            # A process may have stopped after the draft checkpoint but before
            # the independent audit. Resume that exact draft instead of
            # spending another writer call.
            self.state.citation_audit = audit_report(
                self.state.report,
                self.state.evidence,
                self.state.sources,
                self.state.claims,
                used_claim_ids=self.state.used_claim_ids,
            )
            self._audit_draft()
        if self._quality_passed():
            self._finalize_report()
            return

        if self.state.writer_corrections_used >= 1:
            raise ResearchQualityError(
                self._quality_failure_message() + " / bounded writer correction already used"
            )
        correction_calls = 2
        if not self.state.budget.can_call_model(correction_calls):
            raise ResearchQualityError(self._quality_failure_message())
        feedback = {
            "citation_audit": self.state.citation_audit,
            "claim_audit": self.state.claim_audit,
            "instruction": "Remove unsupported claims and repair citations; add no new facts.",
        }
        self.state.phase = "writer_correction"
        self.status("[6/6] 감사 결과를 반영해 보고서를 한 번만 교정하는 중...")
        self.state.writer_attempts += 1
        self.state.writer_corrections_used += 1
        corrected = self._call_json(
            "writer-correction",
            self.cfg.research_model,
            writer_messages(
                self.state.contract or ResearchContract(self.state.query, self.state.query),
                self.state.claims,
                self.state.gaps,
                self.state.evidence,
                self.state.sources,
                self.cfg.research_source_chars,
                audit_feedback=feedback,
            ),
            WRITER_SCHEMA,
        )
        self._accept_draft(corrected)
        self._audit_draft()
        if not self._quality_passed():
            raise ResearchQualityError(self._quality_failure_message())
        self._finalize_report()

    def _accept_draft(self, payload: Dict[str, Any]) -> None:
        report = str(payload.get("report") or "").strip()
        if not report:
            raise ResearchModelResponseError("writer report가 비어 있습니다.")
        self.state.report = report
        self.state.used_claim_ids = _unique(
            str(item) for item in _require_list(payload, "used_claim_ids") if str(item).strip()
        )
        if not self.state.used_claim_ids:
            raise ResearchModelResponseError("writer가 used_claim_ids를 선언하지 않았습니다.")
        self.state.citation_audit = audit_report(
            report,
            self.state.evidence,
            self.state.sources,
            self.state.claims,
            used_claim_ids=self.state.used_claim_ids,
        )
        self.store.checkpoint(self.state, event="draft_written")

    def _audit_draft(self) -> None:
        self.state.phase = "audit"
        self.status("[5/6] 인용 무결성과 주장-근거 함의를 독립적으로 감사하는 중...")
        if not self.state.budget.can_call_model():
            self.state.claim_audit = {
                "passed": False,
                "summary": "model-call budget exhausted before independent audit",
                "claim_results": [],
            }
            self.store.checkpoint(self.state, event="audit_budget_exhausted")
            return
        payload = self._call_json(
            "claim-audit",
            self.cfg.reviewer_model,
            audit_messages(
                self.state.report,
                self.state.claims,
                self.state.evidence,
                self.state.sources,
                self.state.used_claim_ids,
                self.cfg.research_source_chars,
            ),
            AUDIT_SCHEMA,
        )
        self.state.claim_audit = self._validate_claim_audit(payload)
        self.store.checkpoint(self.state, event="audit_completed")

    def _validate_claim_audit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        known_claims = {item.id: item for item in self.state.claims}
        known_evidence = {
            item.id: item for item in self.state.evidence if item.verified_exact
        }
        expected = [item for item in self.state.used_claim_ids if item in known_claims]
        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for row in _require_list(payload, "claim_results"):
            if not isinstance(row, dict):
                continue
            claim_id = str(row.get("claim_id") or "")
            if claim_id not in expected or claim_id in seen:
                continue
            seen.add(claim_id)
            allowed_evidence = {
                item
                for item in known_claims[claim_id].evidence_ids
                if item in known_evidence and known_evidence[item].stance == "supports"
            }
            evidence_ids = [
                str(item)
                for item in row.get("evidence_ids", [])
                if str(item) in allowed_evidence
            ] if isinstance(row.get("evidence_ids"), list) else []
            results.append({
                "claim_id": claim_id,
                "supported": bool(row.get("supported")) and bool(evidence_ids),
                "evidence_ids": evidence_ids,
                "reason": str(row.get("reason") or ""),
            })
        missing = [item for item in expected if item not in seen]
        failed = [item["claim_id"] for item in results if not item["supported"]]
        passed = bool(payload.get("overall_passed")) and not missing and not failed and bool(expected)
        return {
            "passed": passed,
            "summary": str(payload.get("summary") or ""),
            "claim_results": results,
            "missing_claim_ids": missing,
            "unsupported_claim_ids": failed,
            "auditor_model": self.cfg.reviewer_model,
        }

    def _quality_passed(self) -> bool:
        return (
            bool(self.state.citation_audit.get("passed"))
            and bool(self.state.claim_audit.get("passed"))
        )

    def _quality_failure_message(self) -> str:
        citation = "; ".join(self.state.citation_audit.get("issues", []))
        citation = citation or "citation audit failed"
        claim = self.state.claim_audit.get("summary") or "claim support audit failed"
        return f"보고서 품질 gate를 통과하지 못했습니다: {citation} / {claim}"

    def _finalize_report(self) -> None:
        final = append_used_sources(
            self.state.report,
            self.state.citation_audit,
            self.state.evidence,
            self.state.sources,
        )
        final_audit = audit_report(
            final,
            self.state.evidence,
            self.state.sources,
            self.state.claims,
            used_claim_ids=self.state.used_claim_ids,
        )
        if not final_audit.get("passed"):
            raise ResearchQualityError("결정론적 Sources 부록 생성 후 citation audit가 실패했습니다.")
        self.state.report = final
        self.state.citation_audit = final_audit
        self.store.write_report(final)
        self.store.checkpoint(self.state, event="report_finalized")

    def _call_json(
        self,
        phase: str,
        model: str,
        messages: List[Dict[str, str]],
        schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._assert_runtime()
        self.state.budget.consume_model_call()
        self.store.checkpoint(self.state, event="model_call_started")
        if self.chat_fn is None:
            from buildup.ollama import chat

            fn = chat
        else:
            fn = self.chat_fn
        raw = fn(
            self.session,
            self.cfg,
            model,
            messages,
            keep_alive="15m",
            logger=self.logger,
            think=False,
            json_mode=True,
            json_schema=schema,
            sanitize_thinking=False,
        )
        self._assert_runtime()
        try:
            payload = _json_object(raw)
            _validate_schema_value(payload, schema, phase)
            return payload
        except Exception as exc:
            self.store.record_invalid_model_response(phase, str(raw))
            raise ResearchModelResponseError(
                f"{phase} JSON 응답이 올바르지 않습니다: {exc}"
            ) from exc

    def _can_process_task(self) -> bool:
        # Preserve assessment, writer, first audit, correction, and re-audit.
        reserve = 5
        return (
            self.state.budget.can_search()
            and self.state.budget.model_calls_used + reserve < self.state.budget.max_model_calls
            and len(self.state.sources) < self.state.budget.max_sources
            and self.state.budget.runtime_available()
        )

    def _assert_runtime(self) -> None:
        self._update_runtime()
        if not self.state.budget.runtime_available():
            raise ResearchBudgetExceeded("runtime_seconds", self.state.budget.max_runtime_seconds)

    def _update_runtime(self) -> None:
        now = time.monotonic()
        self.state.budget.runtime_seconds_used += max(0.0, now - self._segment_started)
        self._segment_started = now

    def _reset_resumable_tasks(self) -> None:
        for task in self.state.tasks:
            if (
                task.status in {TaskStatus.RUNNING.value, TaskStatus.FAILED.value}
                and task.attempts < 2
            ):
                task.status = TaskStatus.PENDING.value
                task.error = ""

    def _study_guide(self) -> str:
        contract = self.state.contract or ResearchContract(self.state.query, self.state.query)
        lines = [
            f"# Study Guide — {contract.question}",
            "",
            "연결된 보고서: [06-report.md](06-report.md)",
            "",
            "## 먼저 자료 없이 답할 질문",
            "",
        ]
        questions = contract.subquestions or [contract.question]
        lines.extend(f"- [ ] {item}" for item in questions)
        lines.extend(["", "## 검증된 핵심 주장", ""])
        for claim in self.state.claims:
            refs = " ".join(f"[{item}]" for item in claim.evidence_ids)
            lines.append(f"- {claim.text} {refs}".rstrip())
        lines.extend(["", "## 남은 불확실성", ""])
        open_gaps = [item for item in self.state.gaps if item.status == "open"]
        lines.extend(f"- {item.question} — {item.reason}" for item in open_gaps)
        if not open_gaps:
            lines.append("- 현재 실행에서 중대한 미해결 gap은 식별되지 않았습니다.")
        lines.extend([
            "",
            "## Self-check",
            "",
            "- [ ] 핵심 결론을 자료 없이 설명했다.",
            "- [ ] 근거와 추론을 구분했다.",
            "- [ ] 반례 또는 적용 한계를 하나 이상 만들었다.",
            "- [ ] Evidence ID를 열어 원문 passage와 대조했다.",
            "",
        ])
        return "\n".join(lines)

    def _completed_result(self) -> EngineResult:
        """Validate a completed run, repairing only a missing final seal."""
        report_path = self.store.run_dir / "06-report.md"
        guide_path = self.store.run_dir / "07-study-guide.md"
        manifest_path = self.store.run_dir / "manifest.json"
        if not report_path.is_file():
            raise ResearchQualityError(
                f"완료된 research run의 보고서가 없습니다: {report_path}"
            )
        if manifest_path.is_file():
            failures = self.store.manifest_failures(self.state)
            if failures:
                raise ResearchQualityError(
                    "완료된 research manifest 검증에 실패했습니다: " + ", ".join(failures)
                )
            if not guide_path.is_file():
                raise ResearchQualityError(
                    f"완료된 research run의 study guide가 없습니다: {guide_path}"
                )
            return self._result()

        # A crash can occur after the completed checkpoint and before the last
        # atomic manifest write. The guide is deterministic, so that one
        # narrowly defined state is safe to finish on resume.
        if not self._quality_passed():
            raise ResearchQualityError(
                "완료 상태의 research run이 품질 gate를 통과하지 않았습니다."
            )
        if not guide_path.is_file():
            guide_path = self.store.write_study_guide(self._study_guide())
        self.store.write_manifest(self.state)
        return EngineResult(
            self.state,
            self.store.run_dir,
            report_path,
            guide_path,
        )

    def _result(self) -> EngineResult:
        report_path = self.store.run_dir / "06-report.md"
        guide_path = self.store.run_dir / "07-study-guide.md"
        if not report_path.is_file():
            raise ValueError(f"완료된 research run의 보고서가 없습니다: {report_path}")
        return EngineResult(self.state, self.store.run_dir, report_path, guide_path)


def _json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    fence = "\x60\x60\x60"
    if raw.startswith(fence):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit(fence, 1)[0].strip()
    start = raw.find("{")
    if start < 0:
        raise ValueError("JSON object를 찾지 못했습니다.")
    value, _ = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(value, dict):
        raise ValueError("최상위 JSON 값이 object가 아닙니다.")
    return value


def _require_dict(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ResearchModelResponseError(f"{key}는 JSON object여야 합니다.")
    return value


def _require_list(payload: Dict[str, Any], key: str) -> List[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ResearchModelResponseError(f"{key}는 JSON array여야 합니다.")
    return value


def _validate_schema_value(value: Any, schema: Dict[str, Any], path: str) -> None:
    """Validate the JSON-Schema subset used in local Ollama contracts."""
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        missing = [
            key for key in schema.get("required", [])
            if isinstance(key, str) and key not in value
        ]
        if missing:
            raise ValueError(f"{path} missing required keys: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = [key for key in value if key not in properties]
            if extra:
                raise ValueError(f"{path} has unknown keys: {', '.join(extra)}")
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                _validate_schema_value(value[key], child, f"{path}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise ValueError(f"{path} must contain at least {minimum_items} items")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            raise ValueError(f"{path} must contain at most {maximum_items} items")
        child = schema.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, child, f"{path}[{index}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise ValueError(f"{path} must contain at least {minimum_length} characters")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            raise ValueError(f"{path} must contain at most {maximum_length} characters")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} must be a number")
        if not math.isfinite(float(value)):
            raise ValueError(f"{path} must be finite")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} must be an integer")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")
    if expected in {"number", "integer"} and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ValueError(f"{path} must be at least {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ValueError(f"{path} must be at most {maximum}")
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        raise ValueError(f"{path} must be one of {allowed}")


def _unique(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", str(value)).strip()
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _dedupe_claims(claims: Sequence[Claim]) -> List[Claim]:
    result: List[Claim] = []
    seen: set[str] = set()
    for item in claims:
        key = normalize_text(item.text).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _float01(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            return default
        return max(0.0, min(1.0, parsed))
    except (TypeError, ValueError):
        return default


def _budget_limit(name: str, requested: Optional[int], ceiling: int) -> int:
    if requested is None:
        return ceiling
    if requested < 1:
        raise ValueError(f"{name} must be positive; got {requested}")
    if requested > ceiling:
        raise ValueError(
            f"{name}={requested} exceeds the configured depth ceiling ({ceiling})"
        )
    return requested


def _max_results_limit(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("max_results_per_search must be an integer between 1 and 10")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "max_results_per_search must be an integer between 1 and 10"
        ) from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("max_results_per_search must be an integer between 1 and 10")
    if not 1 <= parsed <= 10:
        raise ValueError("max_results_per_search must be between 1 and 10")
    return parsed


def _source_type(url: str, content_type: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path.lower()
    if "pdf" in content_type or path.endswith(".pdf"):
        return "paper"
    if host.endswith((".gov", ".go.kr", ".europa.eu", ".int")):
        return "government"
    if host.endswith((".edu", ".ac.kr")):
        return "academic"
    if host in {"arxiv.org", "doi.org", "pubmed.ncbi.nlm.nih.gov"}:
        return "paper-index"
    if any(marker in host for marker in ("docs.", "developer.", "support.")):
        return "official-docs"
    if any(marker in host for marker in ("reddit.com", "medium.com", "substack.com")):
        return "community"
    return "web"


def _source_credibility(source_type: str) -> float:
    return {
        "government": 0.9,
        "academic": 0.85,
        "paper": 0.82,
        "paper-index": 0.8,
        "official-docs": 0.82,
        "community": 0.45,
        "web": 0.6,
    }.get(source_type, 0.5)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
