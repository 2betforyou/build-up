"""Deterministic scoring router for planning model selection.

Flow:
  StructuredIntent → score signals → total score → model choice

Model tiers:
  fast  — lightweight, quick tasks (score < main_threshold)
  coder — code-related tasks (code intent + score >= coder_threshold)
  main  — complex reasoning (score >= main_threshold)
  main  — fallback when StructuredIntent is None (conservative)
  research — internal tier forced by the build-up active-paper planner
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

_CODE_INTENTS = frozenset({"code_edit", "debug", "experiment_run", "inspect_code"})


@dataclass
class RoutingDecision:
    model: str                  # actual model string (e.g. "gemma4:e4b")
    tier: str                   # "fast" | "coder" | "main" | "research"
    score: int                  # total weighted score
    reasons: List[str]          # human-readable signals that fired


def select_model(
    structured_intent,          # StructuredIntent | None
    cfg,                        # BuildupConfig
    weights: Optional[Dict[str, int]] = None,
) -> RoutingDecision:
    """Return which model to use for planning, with score and reasons.

    Falls back to main_model conservatively when structured_intent is None.
    """
    from buildup.config import ROUTING_WEIGHTS

    w = weights if weights is not None else ROUTING_WEIGHTS
    main_threshold  = w.get("_main_threshold",  3)
    coder_threshold = w.get("_coder_threshold", 3)

    if structured_intent is None:
        return RoutingDecision(
            model=cfg.main_model,
            tier="main",
            score=0,
            reasons=["structuring_failed → conservative fallback to main"],
        )

    score = 0
    reasons: List[str] = []

    # Intent score
    intent_key = f"intent:{structured_intent.intent}"
    if intent_key in w:
        v = w[intent_key]
        score += v
        reasons.append(f"{intent_key}={v:+d}")

    # Mode score
    mode_key = f"mode:{structured_intent.interaction_mode}"
    if mode_key in w:
        v = w[mode_key]
        score += v
        reasons.append(f"{mode_key}={v:+d}")

    # Confidence score
    conf = structured_intent.confidence
    if conf >= 0.85:
        v = w.get("confidence:high", -1)
        score += v
        reasons.append(f"confidence:high({conf:.2f})={v:+d}")
    elif conf < 0.65:
        v = w.get("confidence:low", 2)
        score += v
        reasons.append(f"confidence:low({conf:.2f})={v:+d}")

    # Decide tier
    is_code_intent = structured_intent.intent in _CODE_INTENTS
    if is_code_intent and score >= coder_threshold:
        tier, model = "coder", cfg.coder_model
    elif score >= main_threshold:
        tier, model = "main", cfg.main_model
    else:
        tier, model = "fast", cfg.fast_model

    return RoutingDecision(model=model, tier=tier, score=score, reasons=reasons)
