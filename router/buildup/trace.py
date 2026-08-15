"""Execution trace recorder for Build-up.

Records every dispatch cycle as a structured JSONL entry:
  - request metadata
  - which tier handled it (0/1a/1b/2)
  - structured intent + confidence
  - policy decision
  - agent steps (tool, params, result, duration)
  - final response
  - success flag

Primary value: accumulating this data enables exemplar retrieval
and future router improvement.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from buildup.config import BuildupConfig


# ── Data classes ─────────────────────────────────────────────

@dataclass
class PlanTrace:
    """Metadata about a constrained-planner execution (Tier-2 state machine)."""
    plan_id: str
    steps_planned: int
    steps_executed: int
    requires_confirmation: bool
    user_confirmed: Optional[bool]    # None if no confirmation was needed
    validation_errors: List[str]


@dataclass
class StepTrace:
    step: int
    action: str
    params: Dict[str, Any]
    result_preview: str      # first 200 chars of result
    success: bool
    duration_ms: int


@dataclass
class DispatchTrace:
    trace_id: str
    timestamp: str
    session_job: Optional[str]
    user_input: str

    # Tier info
    tier: str                # "0" | "1a" | "1b" | "2"
    intent: Optional[str]
    confidence: Optional[float]

    # Policy
    policy_auto: Optional[bool]
    policy_category: Optional[str]

    # Agent steps (Tier-2 only)
    steps: List[StepTrace]

    # Outcome
    final_response_preview: str   # first 300 chars
    success: bool
    duration_ms: int

    # State machine plan metadata (optional — None for legacy ReAct traces)
    plan_trace: Optional[PlanTrace] = None

    # Structured intent from 5.2 Intent Structuring Layer (None for Tier-0/1/legacy)
    structured_intent: Optional[Dict[str, Any]] = None

    # Model routing decision (None for Tier-0/1/legacy)
    routing: Optional[Dict[str, Any]] = None

    # Deterministic first-pass task frame (None for older traces)
    task_frame: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "session_job": self.session_job,
            "user_input": self.user_input,
            "tier": self.tier,
            "intent": self.intent,
            "confidence": self.confidence,
            "policy_auto": self.policy_auto,
            "policy_category": self.policy_category,
            "steps": [
                {
                    "step": s.step,
                    "action": s.action,
                    "params": s.params,
                    "result_preview": s.result_preview,
                    "success": s.success,
                    "duration_ms": s.duration_ms,
                }
                for s in self.steps
            ],
            "final_response_preview": self.final_response_preview,
            "success": self.success,
            "duration_ms": self.duration_ms,
        }
        if self.plan_trace is not None:
            d["plan_trace"] = {
                "plan_id": self.plan_trace.plan_id,
                "steps_planned": self.plan_trace.steps_planned,
                "steps_executed": self.plan_trace.steps_executed,
                "requires_confirmation": self.plan_trace.requires_confirmation,
                "user_confirmed": self.plan_trace.user_confirmed,
                "validation_errors": self.plan_trace.validation_errors,
            }
        if self.structured_intent is not None:
            d["structured_intent"] = self.structured_intent
        if self.routing is not None:
            d["routing"] = self.routing
        if self.task_frame is not None:
            d["task_frame"] = self.task_frame
        return d


# ── Recorder ─────────────────────────────────────────────────

class TraceRecorder:
    """Collects trace data during a dispatch cycle and flushes to JSONL."""

    def __init__(self, cfg: BuildupConfig, session_job: Optional[str], user_input: str):
        self._cfg = cfg
        self._trace_id = str(uuid.uuid4())[:12]
        self._timestamp = datetime.now().isoformat(timespec="seconds")
        self._session_job = session_job
        self._user_input = user_input
        self._started = time.monotonic()

        self.tier: str = "2"
        self.intent: Optional[str] = None
        self.confidence: Optional[float] = None
        self.policy_auto: Optional[bool] = None
        self.policy_category: Optional[str] = None
        self.steps: List[StepTrace] = []
        self.plan_trace: Optional[PlanTrace] = None
        self.structured_intent: Optional[Dict[str, Any]] = None
        self.routing: Optional[Dict[str, Any]] = None
        self.task_frame: Optional[Dict[str, Any]] = None

    # ── Builder methods ───────────────────────────────────────

    def set_tier(self, tier: str) -> None:
        self.tier = tier

    def set_intent(self, intent: Optional[str], confidence: Optional[float] = None) -> None:
        self.intent = intent
        self.confidence = confidence

    def set_plan_trace(self, pt: "PlanTrace") -> None:
        self.plan_trace = pt

    def set_structured_intent(self, si: Any) -> None:
        """Record the StructuredIntent produced by the intent structuring layer."""
        try:
            self.structured_intent = {
                "intent": si.intent,
                "interaction_mode": si.interaction_mode,
                "entities": si.entities,
                "constraints": si.constraints,
                "desired_outputs": si.desired_outputs,
                "confidence": si.confidence,
                "alternate_intents": si.alternate_intents,
            }
        except Exception:
            pass

    def set_routing(self, decision: Any) -> None:
        """Record the RoutingDecision from model_router."""
        try:
            self.routing = {
                "model": decision.model,
                "tier": decision.tier,
                "score": decision.score,
                "reasons": decision.reasons,
            }
        except Exception:
            pass

    def set_task_frame(self, frame: Any) -> None:
        """Record the deterministic TaskFrame for natural-language debugging."""
        try:
            self.task_frame = frame.to_dict()
        except Exception:
            pass

    def set_policy(self, auto_execute: bool, category: str) -> None:
        self.policy_auto = auto_execute
        self.policy_category = category

    def add_step(
        self,
        step: int,
        action: str,
        params: Dict[str, Any],
        result: str,
        success: bool,
        duration_ms: int,
    ) -> None:
        self.steps.append(StepTrace(
            step=step,
            action=action,
            params=params,
            result_preview=result[:200],
            success=success,
            duration_ms=duration_ms,
        ))

    # ── Flush ─────────────────────────────────────────────────

    def flush(self, final_response: str, success: bool) -> None:
        """Write a completed trace to the configured Build-up data store."""
        duration_ms = int((time.monotonic() - self._started) * 1000)
        trace = DispatchTrace(
            trace_id=self._trace_id,
            timestamp=self._timestamp,
            session_job=self._session_job,
            user_input=self._user_input,
            tier=self.tier,
            intent=self.intent,
            confidence=self.confidence,
            policy_auto=self.policy_auto,
            policy_category=self.policy_category,
            steps=self.steps,
            final_response_preview=final_response[:300],
            success=success,
            duration_ms=duration_ms,
            plan_trace=self.plan_trace,
            structured_intent=self.structured_intent,
            routing=self.routing,
            task_frame=self.task_frame,
        )
        try:
            log_path = self._cfg.trace_file
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            pass  # trace failure must never crash Build-up

    @property
    def trace_id(self) -> str:
        return self._trace_id
