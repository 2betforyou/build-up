"""Deterministic dispatch harness for Friday.

The harness exercises the cheap, local part of Friday's dispatch stack:
task framing, Tier-0 chat detection, Tier-1 rule parsing, optional Tier-1b
embedding classification, and permission policy evaluation.  It deliberately
does not execute intents or call the Tier-2 agent unless the caller opts into
embedding classification.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, List, Optional, Protocol

from friday.config import FridayConfig
from friday.intent import is_chat, parse_intent_instant
from friday.permission import evaluate_intent_policy
from friday.task_frame import (
    build_task_frame,
    enrich_intent_with_frame,
    frame_clarification,
)


class IntentClassifier(Protocol):
    def classify_and_extract(
        self,
        user_input: str,
        cfg: FridayConfig,
    ) -> Optional[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class HarnessCase:
    name: str
    text: str
    expected_tier: Optional[str] = None
    expected_intent: Optional[str] = None
    expected_policy_auto: Optional[bool] = None
    expected_policy_category: Optional[str] = None


@dataclass(frozen=True)
class HarnessResult:
    name: str
    text: str
    passed: bool
    tier: str
    intent: Optional[str]
    intent_source: str
    confidence: Optional[float]
    needs_agent: bool
    policy_auto: Optional[bool]
    policy_category: Optional[str]
    frame_task_type: str
    frame_risk: str
    clarification: str
    failures: List[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CASES: tuple[HarnessCase, ...] = (
    HarnessCase(
        name="chat_greeting",
        text="안녕",
        expected_tier="0",
        expected_intent="chat",
    ),
    HarnessCase(
        name="calendar_list_rule",
        text="이번 주 일정 보여줘",
        expected_tier="1a",
        expected_intent="cal_list",
        expected_policy_auto=True,
        expected_policy_category="safe-readonly",
    ),
    HarnessCase(
        name="calendar_add_confirm",
        text="내일 오후 3시에 랩미팅 잡아줘",
        expected_tier="1a",
        expected_intent="cal_add",
        expected_policy_auto=False,
        expected_policy_category="confirm-always",
    ),
    HarnessCase(
        name="web_search_rule",
        text="LLM safety 검색해줘",
        expected_tier="1a",
        expected_intent="search",
        expected_policy_auto=True,
        expected_policy_category="safe-search",
    ),
    HarnessCase(
        name="complex_agent_fallback",
        text="연구 아이디어를 정리해서 다음 액션까지 뽑아줘",
        expected_tier="2",
    ),
)


def make_cases(texts: Iterable[str]) -> list[HarnessCase]:
    """Build ad-hoc harness cases from raw user inputs."""
    return [
        HarnessCase(name=f"input_{idx}", text=text)
        for idx, text in enumerate(texts, 1)
    ]


def run_harness(
    cfg: FridayConfig,
    *,
    current_job: Optional[str],
    cases: Optional[Iterable[HarnessCase]] = None,
    classifier: Optional[IntentClassifier] = None,
) -> list[HarnessResult]:
    """Run dispatch classification checks without executing user actions."""
    selected_cases = list(cases or DEFAULT_CASES)
    return [
        _run_case(case, cfg, current_job=current_job, classifier=classifier)
        for case in selected_cases
    ]


def _run_case(
    case: HarnessCase,
    cfg: FridayConfig,
    *,
    current_job: Optional[str],
    classifier: Optional[IntentClassifier],
) -> HarnessResult:
    frame = build_task_frame(case.text, cfg, current_job)
    clarification = frame_clarification(frame)

    tier = "2"
    intent_source = "none"
    intent: Optional[dict[str, Any]] = None
    policy_auto: Optional[bool] = None
    policy_category: Optional[str] = None
    needs_agent = False

    if clarification and frame.task_type != "chat":
        tier = "frame"
    elif is_chat(case.text):
        tier = "0"
        intent_source = "heuristic"
        intent = {"intent": "chat", "params": {}, "confidence": None}
    else:
        intent = parse_intent_instant(case.text, cfg)
        if intent is not None:
            tier = "1a"
            intent_source = "rules"
        elif classifier is not None:
            intent = classifier.classify_and_extract(case.text, cfg)
            if intent is not None:
                tier = "1b"
                intent_source = "embedding"

        if intent is not None:
            intent = enrich_intent_with_frame(intent, frame)
            action = str(intent.get("intent") or "")
            params = intent.get("params", {})
            if not isinstance(params, dict):
                params = {}
            needs_agent = action == "write" and not params.get("content")
            if needs_agent:
                tier = "2"
            elif action != "chat":
                decision = evaluate_intent_policy(
                    intent,
                    cfg,
                    current_job,
                    confidence=_coerce_confidence(intent.get("confidence")),
                )
                policy_auto = decision.auto_execute
                policy_category = decision.category

    result_intent = str(intent.get("intent")) if intent else None
    confidence = _coerce_confidence(intent.get("confidence")) if intent else None
    failures = _check_expectations(
        case,
        tier=tier,
        intent=result_intent,
        policy_auto=policy_auto,
        policy_category=policy_category,
    )

    return HarnessResult(
        name=case.name,
        text=case.text,
        passed=not failures,
        tier=tier,
        intent=result_intent,
        intent_source=intent_source,
        confidence=confidence,
        needs_agent=needs_agent,
        policy_auto=policy_auto,
        policy_category=policy_category,
        frame_task_type=frame.task_type,
        frame_risk=frame.risk,
        clarification=clarification,
        failures=failures,
    )


def _coerce_confidence(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_expectations(
    case: HarnessCase,
    *,
    tier: str,
    intent: Optional[str],
    policy_auto: Optional[bool],
    policy_category: Optional[str],
) -> list[str]:
    failures: list[str] = []
    if case.expected_tier is not None and tier != case.expected_tier:
        failures.append(f"tier: expected {case.expected_tier}, got {tier}")
    if case.expected_intent is not None and intent != case.expected_intent:
        failures.append(f"intent: expected {case.expected_intent}, got {intent}")
    if (
        case.expected_policy_auto is not None
        and policy_auto != case.expected_policy_auto
    ):
        failures.append(
            f"policy_auto: expected {case.expected_policy_auto}, got {policy_auto}"
        )
    if (
        case.expected_policy_category is not None
        and policy_category != case.expected_policy_category
    ):
        failures.append(
            "policy_category: expected "
            f"{case.expected_policy_category}, got {policy_category}"
        )
    return failures


def results_to_json(results: Iterable[HarnessResult]) -> str:
    return json.dumps(
        [result.to_dict() for result in results],
        ensure_ascii=False,
        indent=2,
    )


def format_harness_results(results: Iterable[HarnessResult]) -> str:
    rows = list(results)
    total = len(rows)
    passed = sum(1 for row in rows if row.passed)
    lines = [f"Dispatch harness: {passed}/{total} passed"]
    for row in rows:
        mark = "PASS" if row.passed else "FAIL"
        policy = "-"
        if row.policy_category is not None:
            policy = f"{row.policy_category} auto={row.policy_auto}"
        confidence = "-" if row.confidence is None else f"{row.confidence:.2f}"
        lines.append(
            f"[{mark}] {row.name}: tier={row.tier} "
            f"intent={row.intent or '-'} source={row.intent_source} "
            f"conf={confidence} policy={policy}"
        )
        lines.append(f"  input: {row.text}")
        if row.needs_agent:
            lines.append("  note: Tier-1 recognized a write intent but content is missing, so Tier-2 should handle it.")
        if row.clarification:
            lines.append(f"  clarification: {row.clarification}")
        for failure in row.failures:
            lines.append(f"  - {failure}")
    return "\n".join(lines)
