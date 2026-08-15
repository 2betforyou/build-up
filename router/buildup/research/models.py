"""Durable domain models for Build-up's evidence-first research engine."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional


SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stable_id(prefix: str, *parts: str, length: int = 12) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8", errors="replace")
    return f"{prefix}{hashlib.sha256(payload).hexdigest()[:length].upper()}"


class ResearchDepth(str, Enum):
    AUTO = "auto"
    SHALLOW = "shallow"
    STANDARD = "standard"
    DEEP = "deep"

    @classmethod
    def parse(cls, value: str) -> "ResearchDepth":
        try:
            return cls((value or "auto").strip().lower())
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(f"지원하지 않는 research depth입니다: {value!r} ({choices})") from exc


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass
class ResearchBudget:
    """Hard, persisted limits and their consumption counters."""

    max_rounds: int
    max_tasks: int
    max_searches: int
    max_sources: int
    max_model_calls: int
    max_runtime_seconds: int
    searches_used: int = 0
    model_calls_used: int = 0
    runtime_seconds_used: float = 0.0

    def can_search(self, count: int = 1) -> bool:
        return self.searches_used + count <= self.max_searches

    def can_call_model(self, count: int = 1) -> bool:
        return self.model_calls_used + count <= self.max_model_calls

    def consume_search(self) -> None:
        if not self.can_search():
            raise ResearchBudgetExceeded("searches", self.max_searches)
        self.searches_used += 1

    def consume_model_call(self) -> None:
        if not self.can_call_model():
            raise ResearchBudgetExceeded("model_calls", self.max_model_calls)
        self.model_calls_used += 1

    def runtime_available(self) -> bool:
        return self.runtime_seconds_used < self.max_runtime_seconds

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ResearchBudget":
        result = cls(
            max_rounds=_persisted_bounded_int(
                value.get("max_rounds"), default=3, minimum=1,
                maximum=1_000_000, name="budget.max_rounds",
            ),
            max_tasks=_persisted_bounded_int(
                value.get("max_tasks"), default=8, minimum=1,
                maximum=1_000_000, name="budget.max_tasks",
            ),
            max_searches=_persisted_bounded_int(
                value.get("max_searches"), default=24, minimum=1,
                maximum=1_000_000, name="budget.max_searches",
            ),
            max_sources=_persisted_bounded_int(
                value.get("max_sources"), default=30, minimum=1,
                maximum=1_000_000, name="budget.max_sources",
            ),
            max_model_calls=_persisted_bounded_int(
                value.get("max_model_calls"), default=20, minimum=1,
                maximum=1_000_000, name="budget.max_model_calls",
            ),
            max_runtime_seconds=_persisted_bounded_int(
                value.get("max_runtime_seconds"), default=1800, minimum=1,
                maximum=31_536_000, name="budget.max_runtime_seconds",
            ),
            searches_used=_persisted_bounded_int(
                value.get("searches_used"), default=0, minimum=0,
                maximum=1_000_000, name="budget.searches_used",
            ),
            model_calls_used=_persisted_bounded_int(
                value.get("model_calls_used"), default=0, minimum=0,
                maximum=1_000_000, name="budget.model_calls_used",
            ),
            runtime_seconds_used=_persisted_nonnegative_float(
                value.get("runtime_seconds_used"),
                default=0.0,
                name="budget.runtime_seconds_used",
            ),
        )
        if result.searches_used > result.max_searches:
            raise ValueError("persisted research searches_used exceeds max_searches")
        if result.model_calls_used > result.max_model_calls:
            raise ValueError("persisted research model_calls_used exceeds max_model_calls")
        return result


class ResearchBudgetExceeded(RuntimeError):
    def __init__(self, resource: str, limit: int):
        self.resource = resource
        self.limit = limit
        super().__init__(f"research budget exhausted: {resource} (limit={limit})")


@dataclass
class ResearchContract:
    question: str
    objective: str
    audience: str = "general"
    deliverable: str = "evidence-grounded Markdown report"
    subquestions: List[str] = field(default_factory=list)
    in_scope: List[str] = field(default_factory=list)
    out_of_scope: List[str] = field(default_factory=list)
    source_requirements: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    freshness: str = "current where relevant"
    assumptions: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Dict[str, Any], *, fallback_question: str = "") -> "ResearchContract":
        return cls(
            question=str(value.get("question") or fallback_question).strip(),
            objective=str(value.get("objective") or value.get("question") or fallback_question).strip(),
            audience=str(value.get("audience") or "general").strip(),
            deliverable=str(value.get("deliverable") or "evidence-grounded Markdown report").strip(),
            subquestions=_string_list(value.get("subquestions")),
            in_scope=_string_list(value.get("in_scope")),
            out_of_scope=_string_list(value.get("out_of_scope")),
            source_requirements=_string_list(value.get("source_requirements")),
            success_criteria=_string_list(value.get("success_criteria")),
            freshness=str(value.get("freshness") or "current where relevant").strip(),
            assumptions=_string_list(value.get("assumptions")),
        )


@dataclass
class ResearchTask:
    id: str
    question: str
    queries: List[str]
    rationale: str = ""
    priority: int = 3
    critical: bool = False
    parent_gap_id: str = ""
    round_created: int = 1
    status: str = TaskStatus.PENDING.value
    attempts: int = 0
    source_ids: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    error: str = ""

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ResearchTask":
        question = str(value.get("question") or "").strip()
        identifier = str(value.get("id") or stable_id("T", question))
        status = str(value.get("status") or TaskStatus.PENDING.value)
        if status not in {item.value for item in TaskStatus}:
            raise ValueError(f"invalid persisted research task status: {status!r}")
        return cls(
            id=identifier,
            question=question,
            queries=_string_list(value.get("queries")) or ([question] if question else []),
            rationale=str(value.get("rationale") or "").strip(),
            priority=_bounded_int(value.get("priority"), 3, 1, 5),
            critical=_persisted_bool(value.get("critical"), default=False, name="task.critical"),
            parent_gap_id=str(value.get("parent_gap_id") or ""),
            round_created=_persisted_bounded_int(
                value.get("round_created"), default=1, minimum=1,
                maximum=1000, name="task.round_created",
            ),
            status=status,
            attempts=_persisted_bounded_int(
                value.get("attempts"), default=0, minimum=0,
                maximum=1000, name="task.attempts",
            ),
            source_ids=_string_list(value.get("source_ids")),
            evidence_ids=_string_list(value.get("evidence_ids")),
            error=str(value.get("error") or ""),
        )


@dataclass
class Source:
    id: str
    url: str
    normalized_url: str
    title: str
    provider: str
    search_query: str
    retrieved_at: str
    snippet: str = ""
    content: str = ""
    content_type: str = ""
    source_type: str = "web"
    author: str = ""
    published_at: str = ""
    content_sha256: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Source":
        return cls(
            id=str(value.get("id") or ""),
            url=str(value.get("url") or ""),
            normalized_url=str(value.get("normalized_url") or value.get("url") or ""),
            title=str(value.get("title") or "Untitled"),
            provider=str(value.get("provider") or "unknown"),
            search_query=str(value.get("search_query") or ""),
            retrieved_at=str(value.get("retrieved_at") or now_iso()),
            snippet=str(value.get("snippet") or ""),
            content=str(value.get("content") or ""),
            content_type=str(value.get("content_type") or ""),
            source_type=str(value.get("source_type") or "web"),
            author=str(value.get("author") or ""),
            published_at=str(value.get("published_at") or ""),
            content_sha256=str(value.get("content_sha256") or ""),
            metadata=_dict(value.get("metadata")),
        )


@dataclass
class Evidence:
    id: str
    source_id: str
    task_id: str
    passage: str
    locator: str
    stance: str
    claim_hint: str
    relevance: float
    credibility: float
    extracted_at: str
    passage_sha256: str
    verified_exact: bool
    notes: str = ""

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Evidence":
        stance = str(value.get("stance") or "context")
        if stance not in {"supports", "contradicts", "context"}:
            raise ValueError(f"invalid persisted evidence stance: {stance!r}")
        return cls(
            id=str(value.get("id") or ""),
            source_id=str(value.get("source_id") or ""),
            task_id=str(value.get("task_id") or ""),
            passage=str(value.get("passage") or ""),
            locator=str(value.get("locator") or ""),
            stance=stance,
            claim_hint=str(value.get("claim_hint") or ""),
            relevance=_bounded_float(value.get("relevance"), 0.5),
            credibility=_bounded_float(value.get("credibility"), 0.5),
            extracted_at=str(value.get("extracted_at") or now_iso()),
            passage_sha256=str(value.get("passage_sha256") or ""),
            verified_exact=_persisted_bool(
                value.get("verified_exact"), default=False, name="evidence.verified_exact"
            ),
            notes=str(value.get("notes") or ""),
        )


@dataclass
class Claim:
    id: str
    text: str
    evidence_ids: List[str]
    contradicting_evidence_ids: List[str] = field(default_factory=list)
    confidence: float = 0.0
    critical: bool = False
    status: str = "supported"

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Claim":
        text = str(value.get("text") or value.get("claim") or "").strip()
        status = str(value.get("status") or "supported")
        if status not in {"supported", "contested", "unsupported"}:
            raise ValueError(f"invalid persisted claim status: {status!r}")
        return cls(
            id=str(value.get("id") or stable_id("C", text)),
            text=text,
            evidence_ids=_string_list(value.get("evidence_ids")),
            contradicting_evidence_ids=_string_list(value.get("contradicting_evidence_ids")),
            confidence=_bounded_float(value.get("confidence"), 0.0),
            critical=_persisted_bool(value.get("critical"), default=False, name="claim.critical"),
            status=status,
        )


@dataclass
class ResearchGap:
    id: str
    question: str
    reason: str
    importance: int
    suggested_queries: List[str]
    round_created: int = 1
    status: str = "open"

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ResearchGap":
        question = str(value.get("question") or value.get("description") or "").strip()
        status = str(value.get("status") or "open")
        if status not in {"open", "resolved"}:
            raise ValueError(f"invalid persisted research gap status: {status!r}")
        return cls(
            id=str(value.get("id") or stable_id("G", question)),
            question=question,
            reason=str(value.get("reason") or "").strip(),
            importance=_bounded_int(value.get("importance"), 3, 1, 5),
            suggested_queries=_string_list(value.get("suggested_queries")) or ([question] if question else []),
            round_created=_persisted_bounded_int(
                value.get("round_created"), default=1, minimum=1,
                maximum=1000, name="gap.round_created",
            ),
            status=status,
        )


@dataclass
class ResearchState:
    run_id: str
    job_id: str
    query: str
    depth: str
    budget: ResearchBudget
    status: str = RunStatus.RUNNING.value
    phase: str = "created"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    schema_version: int = SCHEMA_VERSION
    round: int = 0
    contract: Optional[ResearchContract] = None
    tasks: List[ResearchTask] = field(default_factory=list)
    sources: List[Source] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)
    claims: List[Claim] = field(default_factory=list)
    gaps: List[ResearchGap] = field(default_factory=list)
    coverage: float = 0.0
    target_coverage: float = 0.85
    initial_task_limit: int = 4
    queries_per_task: int = 2
    max_results_per_search: int = 5
    stop_reason: str = ""
    report: str = ""
    used_claim_ids: List[str] = field(default_factory=list)
    provider_names: List[str] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    citation_audit: Dict[str, Any] = field(default_factory=dict)
    claim_audit: Dict[str, Any] = field(default_factory=dict)
    writer_attempts: int = 0
    writer_corrections_used: int = 0
    session_id: str = ""
    workspace_key: str = ""

    def touch(self) -> None:
        self.updated_at = now_iso()

    def source(self, source_id: str) -> Optional[Source]:
        return next((item for item in self.sources if item.id == source_id), None)

    def evidence_item(self, evidence_id: str) -> Optional[Evidence]:
        return next((item for item in self.evidence if item.id == evidence_id), None)

    def completed_tasks(self) -> Iterable[ResearchTask]:
        return (item for item in self.tasks if item.status == TaskStatus.COMPLETED.value)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ResearchState":
        schema_version = _persisted_bounded_int(
            value.get("schema_version"), default=SCHEMA_VERSION,
            minimum=SCHEMA_VERSION, maximum=SCHEMA_VERSION, name="schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported research state schema: {schema_version} "
                f"(expected {SCHEMA_VERSION})"
            )
        depth = str(value.get("depth") or ResearchDepth.STANDARD.value)
        parsed_depth = ResearchDepth.parse(depth)
        if parsed_depth is ResearchDepth.AUTO:
            raise ValueError("persisted research depth must be resolved, not auto")
        status = str(value.get("status") or RunStatus.RUNNING.value)
        if status not in {item.value for item in RunStatus}:
            raise ValueError(f"invalid persisted research status: {status!r}")
        budget = ResearchBudget.from_dict(_dict(value.get("budget")))
        depth_defaults = {
            ResearchDepth.SHALLOW.value: (0.72, 2, 1),
            ResearchDepth.STANDARD.value: (0.85, 4, 2),
            ResearchDepth.DEEP.value: (0.90, 6, 3),
        }
        target_default, tasks_default, queries_default = depth_defaults.get(
            parsed_depth.value, depth_defaults[ResearchDepth.STANDARD.value]
        )
        return cls(
            run_id=str(value["run_id"]),
            job_id=str(value["job_id"]),
            query=str(value["query"]),
            depth=parsed_depth.value,
            budget=budget,
            status=status,
            phase=str(value.get("phase") or "created"),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
            schema_version=schema_version,
            round=_persisted_bounded_int(
                value.get("round"), default=0, minimum=0,
                maximum=budget.max_rounds, name="round",
            ),
            contract=(
                ResearchContract.from_dict(_dict(value.get("contract")), fallback_question=str(value["query"]))
                if value.get("contract") else None
            ),
            tasks=[ResearchTask.from_dict(_dict(item)) for item in _list(value.get("tasks"))],
            sources=[Source.from_dict(_dict(item)) for item in _list(value.get("sources"))],
            evidence=[Evidence.from_dict(_dict(item)) for item in _list(value.get("evidence"))],
            claims=[Claim.from_dict(_dict(item)) for item in _list(value.get("claims"))],
            gaps=[ResearchGap.from_dict(_dict(item)) for item in _list(value.get("gaps"))],
            coverage=_persisted_bounded_float(
                value.get("coverage"), default=0.0, name="coverage"
            ),
            target_coverage=_persisted_bounded_float(
                value.get("target_coverage"), default=target_default,
                name="target_coverage",
            ),
            initial_task_limit=_persisted_bounded_int(
                value.get("initial_task_limit"), default=tasks_default,
                minimum=1, maximum=1000, name="initial_task_limit",
            ),
            queries_per_task=_persisted_bounded_int(
                value.get("queries_per_task"), default=queries_default,
                minimum=1, maximum=10, name="queries_per_task",
            ),
            max_results_per_search=_persisted_bounded_int(
                value.get("max_results_per_search"),
                default=5,
                minimum=1,
                maximum=10,
                name="max_results_per_search",
            ),
            stop_reason=str(value.get("stop_reason") or ""),
            report=str(value.get("report") or ""),
            used_claim_ids=_string_list(value.get("used_claim_ids")),
            provider_names=_string_list(value.get("provider_names")),
            errors=[_dict(item) for item in _list(value.get("errors"))],
            citation_audit=_dict(value.get("citation_audit")),
            claim_audit=_dict(value.get("claim_audit")),
            writer_attempts=_persisted_bounded_int(
                value.get("writer_attempts"), default=0, minimum=0,
                maximum=1000, name="writer_attempts",
            ),
            writer_corrections_used=_persisted_bounded_int(
                value.get("writer_corrections_used"), default=0, minimum=0,
                maximum=1, name="writer_corrections_used",
            ),
            session_id=str(value.get("session_id") or ""),
            workspace_key=str(value.get("workspace_key") or ""),
        )


def _list(value: Any) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"persisted research value must be a list; got {type(value).__name__}")
    return value


def _dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"persisted research value must be an object; got {type(value).__name__}")
    return value


def _string_list(value: Any) -> List[str]:
    items = _list(value)
    if any(not isinstance(item, str) for item in items):
        raise ValueError("persisted research string list contains a non-string value")
    return [item.strip() for item in items if item.strip()]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _bounded_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            return default
        return max(0.0, min(1.0, parsed))
    except (TypeError, ValueError):
        return default


def _persisted_bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
    name: str,
) -> int:
    """Parse persisted policy without silently changing a corrupted value."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"invalid persisted research policy {name}: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"invalid persisted research policy {name}: {value!r}"
        ) from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"invalid persisted research policy {name}: {value!r}")
    if not minimum <= parsed <= maximum:
        raise ValueError(
            f"persisted research policy {name} must be between "
            f"{minimum} and {maximum}; got {parsed}"
        )
    return parsed


def _persisted_bounded_float(
    value: Any,
    *,
    default: float,
    name: str,
) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"invalid persisted research policy {name}: {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"invalid persisted research policy {name}: {value!r}"
        ) from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(
            f"persisted research policy {name} must be finite and between 0 and 1"
        )
    return parsed


def _persisted_nonnegative_float(
    value: Any,
    *,
    default: float,
    name: str,
) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"invalid persisted research value {name}: {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid persisted research value {name}: {value!r}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"persisted research value {name} must be finite and non-negative")
    return parsed


def _persisted_bool(value: Any, *, default: bool, name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"invalid persisted research boolean {name}: {value!r}")
    return value
