"""Intent permission policy for Build-up interactive shell.

This module decides whether a parsed natural-language intent may execute
immediately, or should require an explicit [y/n] confirmation first.

Policy goal:
- Auto-run only *local, non-destructive* actions inside the Build-up workspace.
- Keep destructive / external / ambiguous actions behind confirmation.
- Exception: web search is allowed to auto-run because it is read-only and
  user-triggered, even though it uses the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from buildup.config import BuildupConfig


ALWAYS_CONFIRM_INTENTS = {
    # Potentially destructive
    "trash",
    # Writes outside current workspace / copies data across boundaries
    "import",
    "export",
    # Explicit user judgment or cross-vault state changes
    "wiki_reject",
    "wiki_bind",
    "wiki_rollback",
}


@dataclass(frozen=True)
class IntentPolicyDecision:
    """Decision returned by the permission policy."""

    auto_execute: bool
    reason: str
    policy: str = "balanced"
    category: str = "unknown"
    # Confidence-gated downgrade: if set, this overrides auto_execute
    confidence_gated: bool = False


def _resolve_loose(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(child: Path, parent: Path) -> bool:
    child_r = _resolve_loose(child)
    parent_r = _resolve_loose(parent)
    return child_r == parent_r or parent_r in child_r.parents


def _current_job_base(cfg: BuildupConfig, current_job: Optional[str]) -> Optional[Path]:
    if not current_job:
        return None
    return _resolve_loose(cfg.workspace_dir / current_job)


def _safe_job_id(job_id: str) -> bool:
    try:
        from buildup.paths import validate_job_id

        validate_job_id(job_id)
    except (TypeError, ValueError):
        return False
    return True


def _relpath_within_current_job(relpath: str, cfg: BuildupConfig, current_job: Optional[str]) -> bool:
    if not relpath:
        return False
    job_base = _current_job_base(cfg, current_job)
    if job_base is None:
        return False
    candidate = _resolve_loose(job_base / relpath)
    return _is_within(candidate, job_base)


def _any_path_outside_job(params: Mapping[str, Any], cfg: BuildupConfig, current_job: Optional[str]) -> bool:
    job_base = _current_job_base(cfg, current_job)
    if job_base is None:
        return True

    keys = ("relpath", "path", "file_a", "file_b", "src", "dst")
    for key in keys:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            candidate = _resolve_loose(job_base / value)
            if not _is_within(candidate, job_base):
                return True
    return False


def evaluate_intent_policy(
    intent_obj: Mapping[str, Any],
    cfg: BuildupConfig,
    current_job: Optional[str],
    confidence: Optional[float] = None,
) -> IntentPolicyDecision:
    """Evaluate execution policy for a structured intent.

    confidence — embed classifier score (0.0–1.0).
      >= 0.85 : full policy applies
      0.75–0.85: write/edit operations require confirmation
      < 0.75  : caller should have fallen to agent; if reached here, read-only only
    """
    intent = str(intent_obj.get("intent", "") or "").strip()
    params = intent_obj.get("params", {})
    if not isinstance(params, Mapping):
        params = {}

    # ── Confidence-based capability downgrade ──────────────────────────
    # 0.75–0.85: write/edit/rewrite are demoted to confirm-required
    # (below 0.75 never reaches here — embed_classifier returns None)
    _WRITE_INTENTS = {"write", "edit_file", "rewrite", "trash"}
    if confidence is not None and confidence < 0.85 and intent in _WRITE_INTENTS:
        return IntentPolicyDecision(
            auto_execute=False,
            reason=f"분류 신뢰도({confidence:.2f})가 0.85 미만이므로 쓰기 작업은 확인이 필요합니다.",
            category="confidence-gated",
            confidence_gated=True,
        )

    if not intent or intent == "chat":
        return IntentPolicyDecision(
            auto_execute=False,
            reason="대화형 응답이므로 실행형 intent가 아닙니다.",
            category="chat",
        )

    if intent in ALWAYS_CONFIRM_INTENTS:
        return IntentPolicyDecision(
            auto_execute=False,
            reason="외부 영향, 파괴 가능성, 또는 경계 간 복사가 있는 작업입니다.",
            category="confirm-always",
        )

    if intent in {"files", "job_current", "job_list", "job_summary", "templates"}:
        return IntentPolicyDecision(
            auto_execute=True,
            reason="로컬 조회 전용 작업입니다.",
            category="safe-readonly",
        )

    if intent == "search":
        query = str(params.get("query", "") or "").strip()
        if query:
            return IntentPolicyDecision(
                auto_execute=True,
                reason="사용자가 요청한 읽기 전용 외부 검색입니다.",
                category="safe-search",
            )
        return IntentPolicyDecision(
            auto_execute=False,
            reason="검색어가 비어 있어 확인이 필요합니다.",
            category="ambiguous",
        )

    if intent == "deep_search":
        query = str(params.get("query", "") or "").strip()
        save_to_file = params.get("save_to_file", False)
        if query and not save_to_file:
            return IntentPolicyDecision(
                auto_execute=True,
                reason="사용자가 요청한 읽기 전용 심층 검색입니다.",
                category="safe-search",
            )
        if query and save_to_file:
            return IntentPolicyDecision(
                auto_execute=True,
                reason="심층 검색 결과를 현재 job 내부에 파일로 저장합니다 (비파괴).",
                category="safe-local-create",
            )
        return IntentPolicyDecision(
            auto_execute=False,
            reason="검색어가 비어 있어 확인이 필요합니다.",
            category="ambiguous",
        )

    if intent == "deep_research":
        query = str(params.get("query", "") or "").strip()
        return IntentPolicyDecision(
            auto_execute=bool(query),
            reason=(
                "웹 자료를 읽고 현재 job 안에 역할별 결과를 새로 저장합니다."
                if query else "연구 주제가 비어 있습니다."
            ),
            category="safe-local-create" if query else "ambiguous",
        )

    if intent in {"wiki_ask", "wiki_lint", "wiki_review"}:
        return IntentPolicyDecision(
            auto_execute=True,
            reason="Knowledge Vault의 로컬 원장과 파생 뷰를 읽는 비파괴 작업입니다.",
            category="safe-readonly",
        )

    if intent in {"wiki_add", "wiki_verify"}:
        return IntentPolicyDecision(
            auto_execute=True,
            reason=(
                "봉인된 research에서 append-only 지식을 추가합니다."
                if intent == "wiki_add"
                else "사용자가 직접 확인한 claim을 append-only 이벤트로 승격합니다."
            ),
            category="safe-local-create",
        )

    if intent == "paper_translate":
        return IntentPolicyDecision(
            auto_execute=True,
            reason="선택한 PDF를 읽고 허용된 job 또는 논문 라이브러리에 번역본을 저장합니다.",
            category="safe-local-create",
        )

    if intent in {"read", "readpdf"}:
        relpath = str(params.get("relpath", "") or "").strip()
        if _relpath_within_current_job(relpath, cfg, current_job):
            return IntentPolicyDecision(
                auto_execute=True,
                reason="현재 job 내부 파일을 읽는 비파괴 작업입니다.",
                category="safe-local-read",
            )
        return IntentPolicyDecision(
            auto_execute=False,
            reason="현재 job 내부 경로로 안전하게 확인되지 않았습니다.",
            category="unsafe-path",
        )

    if intent == "rewrite":
        relpath = str(params.get("relpath", "") or params.get("path", "") or "").strip()
        instruction = str(params.get("instruction", "") or "").strip()
        if not instruction:
            return IntentPolicyDecision(
                auto_execute=False,
                reason="수정 지시가 비어 있어 확인이 필요합니다.",
                category="ambiguous",
            )
        if _relpath_within_current_job(relpath, cfg, current_job):
            return IntentPolicyDecision(
                auto_execute=True,
                reason="현재 job 내부에서 수정본을 새로 생성하는 비파괴 작업입니다.",
                category="safe-local-rewrite",
            )
        return IntentPolicyDecision(
            auto_execute=False,
            reason="rewrite 대상이 현재 job 내부 경로로 확인되지 않았습니다.",
            category="unsafe-path",
        )

    if intent == "diff":
        if _any_path_outside_job(params, cfg, current_job):
            return IntentPolicyDecision(
                auto_execute=False,
                reason="비교 대상 파일이 현재 job 내부 경로로 확인되지 않았습니다.",
                category="unsafe-path",
            )
        return IntentPolicyDecision(
            auto_execute=True,
            reason="현재 job 내부 텍스트 파일 비교는 비파괴 로컬 작업입니다.",
            category="safe-local-diff",
        )

    if intent == "write":
        relpath = str(params.get("relpath", "") or "").strip()
        if _relpath_within_current_job(relpath, cfg, current_job):
            return IntentPolicyDecision(
                auto_execute=True,
                reason="현재 job 내부에 새 파일을 생성하는 비파괴 로컬 작업입니다.",
                category="safe-local-create",
            )
        return IntentPolicyDecision(
            auto_execute=False,
            reason="write 대상이 현재 job 내부 경로로 확인되지 않았습니다.",
            category="unsafe-path",
        )

    if intent == "job_new":
        return IntentPolicyDecision(
            auto_execute=True,
            reason="workspace 내부에 새 job 폴더를 만드는 비파괴 로컬 작업입니다.",
            category="safe-local-create",
        )

    if intent == "edit_file":
        relpath = str(params.get("relpath", "") or "").strip()
        if not params.get("old_string") or not relpath:
            return IntentPolicyDecision(
                auto_execute=False,
                reason="edit_file에 필요한 relpath 또는 old_string이 없습니다.",
                category="ambiguous",
            )
        if _relpath_within_current_job(relpath, cfg, current_job):
            return IntentPolicyDecision(
                auto_execute=True,
                reason="현재 job 내부 파일의 정밀 편집입니다.",
                category="safe-local-edit",
            )
        return IntentPolicyDecision(
            auto_execute=False,
            reason="편집 대상이 현재 job 내부 경로로 확인되지 않았습니다.",
            category="unsafe-path",
        )

    if intent in {"glob_files", "grep_files"}:
        return IntentPolicyDecision(
            auto_execute=True,
            reason="파일 검색은 읽기 전용 비파괴 작업입니다.",
            category="safe-readonly",
        )

    if intent == "job_use":
        job_id = str(params.get("job_id", "") or "").strip()
        if not job_id or _safe_job_id(job_id):
            return IntentPolicyDecision(
                auto_execute=True,
                reason="활성 job 전환은 로컬 상태만 바꾸는 비파괴 작업입니다.",
                category="safe-local-state",
            )
        return IntentPolicyDecision(
            auto_execute=False,
            reason="job_id가 안전한 형식으로 확인되지 않았습니다.",
            category="unsafe-path",
        )

    if intent == "session_resume":
        return IntentPolicyDecision(
            auto_execute=True,
            reason="세션 선택기 열기는 로컬 상태만 바꾸는 비파괴 작업입니다.",
            category="safe-local-state",
        )

    return IntentPolicyDecision(
        auto_execute=False,
        reason="정책에 등록되지 않은 intent라서 확인이 필요합니다.",
        category="unknown",
    )
