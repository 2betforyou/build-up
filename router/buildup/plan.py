"""Data structures for the State Machine + Constrained Planner (Tier-2 redesign)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ── Exception ─────────────────────────────────────────────────────────────────

class PlanningError(Exception):
    """Raised when plan building fails unrecoverably. Triggers legacy fallback."""

    def __init__(self, message: str, reason: str = "unknown"):
        super().__init__(message)
        self.message = message
        # "parse_failure" | "validation_error" | "llm_error"
        self.reason = reason


# ── Core dataclasses ──────────────────────────────────────────────────────────

@dataclass
class PlanStep:
    step_id: int                        # 1-based
    action: str                         # registered tool name
    params: Dict[str, Any]
    description: str                    # Korean human-readable
    risk_level: str = "low"             # copied from ToolDef
    required_capability: str = ""       # copied from ToolDef
    condition: Optional[str] = None     # "step_N.success" | "step_N.failure"
    max_retries: int = 1


@dataclass
class ExecutionPlan:
    plan_id: str
    user_input: str
    rationale: str
    steps: List[PlanStep]
    requires_confirmation: bool = False


@dataclass
class StepOutcome:
    step_id: int
    success: bool
    result: str
    duration_ms: int
    retries_used: int = 0


@dataclass
class PlanResult:
    plan: ExecutionPlan
    outcomes: List[StepOutcome]
    final_response: str
    success: bool
    aborted: bool = False


# ── Condition evaluator ───────────────────────────────────────────────────────

_COND_RE = re.compile(r"^step_(\d+)\.(success|failure)$")


def evaluate_condition(condition: Optional[str], outcomes: List[StepOutcome]) -> bool:
    """Evaluate a step condition string against completed outcomes.

    Supported grammar: "step_N.success" or "step_N.failure"
    Returns True (permissive default) for unrecognized or None conditions.
    Never uses eval().
    """
    if condition is None:
        return True
    m = _COND_RE.match(condition.strip())
    if not m:
        return True  # Unknown pattern → proceed
    step_id = int(m.group(1))
    check = m.group(2)
    for outcome in outcomes:
        if outcome.step_id == step_id:
            return outcome.success if check == "success" else not outcome.success
    return True  # Referenced step not yet executed → proceed
