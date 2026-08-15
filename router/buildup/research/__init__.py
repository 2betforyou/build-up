"""Public research primitives for Build-up."""

from .citations import audit_report, verify_exact_passage
from .engine import (
    EngineResult,
    ResearchEngine,
    ResearchModelResponseError,
    ResearchQualityError,
    choose_depth,
    create_state,
)
from .evaluation import evaluate_run, format_evaluation
from .models import (
    Claim,
    Evidence,
    ResearchBudget,
    ResearchContract,
    ResearchDepth,
    ResearchGap,
    ResearchState,
    ResearchTask,
    Source,
)
from .providers import SearchBroker, SearchProviderError
from .reader import SafeWebReader
from .store import ResearchStore

__all__ = [
    "Claim",
    "EngineResult",
    "Evidence",
    "ResearchBudget",
    "ResearchContract",
    "ResearchDepth",
    "ResearchEngine",
    "ResearchGap",
    "ResearchModelResponseError",
    "ResearchQualityError",
    "ResearchState",
    "ResearchStore",
    "ResearchTask",
    "SafeWebReader",
    "SearchBroker",
    "SearchProviderError",
    "Source",
    "audit_report",
    "choose_depth",
    "create_state",
    "evaluate_run",
    "format_evaluation",
    "verify_exact_passage",
]
