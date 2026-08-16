"""Task framing and lightweight context resolution for natural-language input.

This layer does not execute anything. It turns a raw utterance into a small,
auditable frame: what Build-up thinks the user wants, what targets were resolved,
what is missing, and whether the next layer should ask a clarification question.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from buildup.config import BuildupConfig
from buildup.jobs import cmd_files, read_action_log
from buildup.state import job_dir


@dataclass(frozen=True)
class TargetCandidate:
    """A resolved target that a later planner or executor can use."""

    kind: str
    value: str
    label: str
    source: str
    confidence: float = 0.75


@dataclass(frozen=True)
class TaskFrame:
    """Build-up's first-pass understanding of a user request."""

    raw_input: str
    task_type: str
    goal: str
    current_job: Optional[str]
    targets: List[TargetCandidate] = field(default_factory=list)
    candidate_targets: List[TargetCandidate] = field(default_factory=list)
    missing_slots: List[str] = field(default_factory=list)
    ambiguities: List[str] = field(default_factory=list)
    risk: str = "read_only"
    confidence: float = 0.5
    proposed_steps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def has_blocking_clarification(self) -> bool:
        return bool(self.missing_slots or self.ambiguities)

    def agent_context(self) -> str:
        """Return a compact context block for the Tier-2 planner."""
        if self.task_type in {"chat", "unknown"} and not self.targets:
            return ""
        lines = [
            "[build-up task frame]",
            f"task_type: {self.task_type}",
            f"goal: {self.goal}",
            f"risk: {self.risk}",
            f"confidence: {self.confidence:.2f}",
        ]
        if self.targets:
            lines.append(
                "targets: "
                + ", ".join(f"{t.kind}:{t.value} ({t.source})" for t in self.targets)
            )
        if self.proposed_steps:
            lines.append("proposed_steps: " + " -> ".join(self.proposed_steps))
        return "\n".join(lines)


_FILE_EXT_RE = re.compile(
    r"(?P<path>[\w가-힣 ._/\-]+?\.(?:md|txt|pdf|py|json|yaml|yml|csv|log|tex|toml|html|css|js|ts|docx|pptx|xlsx))",
    re.I,
)


def _file_candidates(current_job: Optional[str], cfg: BuildupConfig) -> List[str]:
    if not current_job:
        return []
    try:
        return [
            f for f in cmd_files(current_job, cfg)
            if f and not f.endswith("/") and not Path(f).name.startswith(".")
        ]
    except Exception:
        return []


def _recent_file_candidates(current_job: Optional[str], cfg: BuildupConfig) -> List[TargetCandidate]:
    """Return recently touched/imported files, newest first."""
    if not current_job:
        return []

    seen: set[str] = set()
    results: List[TargetCandidate] = []

    def add(value: str, source: str, confidence: float = 0.8) -> None:
        value = value.strip()
        if not value or value in seen:
            return
        seen.add(value)
        results.append(TargetCandidate("file", value, value, source, confidence))

    try:
        entries = read_action_log(current_job, cfg)
    except Exception:
        entries = []

    for entry in reversed(entries[-50:]):
        action = entry.get("action", "")
        detail = entry.get("detail", "")
        if not detail:
            continue
        if action in {"import", "write", "load_paper", "trash"}:
            add(detail.splitlines()[0].strip(), f"action_log:{action}", 0.85)
        elif action in {"rewrite", "edit", "deep_search"}:
            # Common formats: "src -> dst | instruction", "query -> file.md".
            arrow = re.split(r"\s*(?:->|→)\s*", detail, maxsplit=1)
            if len(arrow) == 2:
                add(arrow[1].split("|", 1)[0].strip(), f"action_log:{action}", 0.82)
                add(arrow[0].strip(), f"action_log:{action}", 0.72)
            else:
                add(detail.split(":", 1)[0].strip(), f"action_log:{action}", 0.7)

    # Fall back to filesystem modification time so "recent file" works even if
    # the action log missed a direct/manual file change.
    try:
        base = job_dir(current_job, cfg)
        paths = [
            p for p in base.rglob("*")
            if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
        ]
        for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
            add(str(path.relative_to(base)), "mtime", 0.65)
    except Exception:
        pass

    return results


def _detect_task_type(text: str) -> str:
    t = text.lower()
    if re.search(r"딥\s*리서치|심층\s*리서치|deep\s*research", t):
        return "deep_research"
    if re.search(r"번역|translate", t) and re.search(r"\.pdf\b|논문|paper|이\s*거|이것", t):
        return "paper_translation"
    if re.search(r"arxiv\.org/(abs|pdf)/|arxiv\s*:?\s*\d{4}\.\d{4,5}|https?://\S+\.pdf", t):
        return "paper_review"
    if re.search(r"일정|스케줄|미팅|회의|약속|calendar|schedule", t):
        if re.search(r"추가|잡아|넣어|등록|삭제|지워|import|export|add|delete", t):
            return "calendar_mutation"
        return "calendar_query"
    if re.search(r"검색|찾아봐|웹|구글|search|research", t):
        return "web_research" if re.search(r"정리|분석|보고서|리포트|저장", t) else "search"
    if re.search(r"쉘|터미널|명령|실행|shell|terminal|run command", t):
        return "shell"
    if re.search(r"\bgit\b|커밋|브랜치|diff|status", t):
        return "git"
    if re.search(r"(파일|문서).*(목록|리스트|list)|\bfiles\b|list\s+files", t):
        return "file_list"
    if re.search(r"요약|정리|summar", t):
        return "file_summarize" if _mentions_file_like(text) else "summarize"
    if re.search(r"다듬|수정|고쳐|편집|rewrite|edit|polish|refactor", t):
        return "file_rewrite" if _mentions_file_like(text) else "edit_or_rewrite"
    if re.search(r"읽어|열어|보여|내용|read|open", t) and _mentions_file_like(text):
        return "file_read"
    if re.search(r"가져와|import|복사", t):
        return "file_import"
    if re.search(r"파일|문서|pdf|논문|readme", t) and not re.search(
        r"쓰고\s*싶|쓸\s*(거|래|건데)|작성하고\s*싶|쓰려고|만들고\s*싶", t
    ):
        return "file_request"
    return "chat" if re.search(r"^(안녕|고마워|thanks|hello|hi)\b", t) else "unknown"


def _mentions_file_like(text: str) -> bool:
    return bool(_FILE_EXT_RE.search(text) or re.search(r"파일|문서|pdf|논문|readme|방금|아까|최근|그거|그 파일|this file|last file", text, re.I))


def _explicit_file_mentions(text: str, files: List[str]) -> List[TargetCandidate]:
    lower = text.lower()
    results: List[TargetCandidate] = []
    seen: set[str] = set()

    for match in _FILE_EXT_RE.finditer(text):
        raw = match.group("path").strip(" .")
        # Prefer a real workspace file when the basename matches.
        raw_base = Path(raw).name.lower()
        matched = next(
            (f for f in files if f.lower() == raw.lower() or Path(f).name.lower() == raw_base),
            None,
        )
        # Never turn arbitrary surrounding prose ending in ".pdf" into a
        # phantom file candidate.  If it is not already a known workspace
        # file, only accept it when it resolves to a real local file.
        if matched is None:
            candidate_path = Path(raw).expanduser()
            if not candidate_path.is_file():
                continue
            matched = str(candidate_path)
        if matched not in seen:
            seen.add(matched)
            results.append(TargetCandidate("file", matched, matched, "explicit_path", 0.95))

    for f in files:
        base = Path(f).name.lower()
        stem = Path(f).stem.lower()
        if f.lower() in lower or base in lower or (stem and len(stem) >= 4 and stem in lower):
            if f not in seen:
                seen.add(f)
                results.append(TargetCandidate("file", f, f, "workspace_match", 0.9))

    if "readme" in lower:
        for f in files:
            if Path(f).name.lower() == "readme.md" and f not in seen:
                seen.add(f)
                results.append(TargetCandidate("file", f, f, "readme_alias", 0.9))
    return results


def _resolve_targets(text: str, current_job: Optional[str], cfg: BuildupConfig) -> tuple[List[TargetCandidate], List[TargetCandidate], List[str], List[str]]:
    files = _file_candidates(current_job, cfg)
    recent = _recent_file_candidates(current_job, cfg)
    explicit = _explicit_file_mentions(text, files)
    lower = text.lower()

    missing: List[str] = []
    ambiguities: List[str] = []
    targets: List[TargetCandidate] = []
    candidates: List[TargetCandidate] = []

    if explicit:
        targets = explicit[:1]
        if len(explicit) > 1:
            candidates = explicit[:5]
            ambiguities.append("target_file")
        return targets, candidates, missing, ambiguities

    asks_recent = bool(re.search(r"방금|아까|최근|last|recent|그 파일|그거|this file", lower))
    asks_pdf = bool(re.search(r"\bpdf\b|논문", lower))
    if asks_recent:
        pool = [c for c in recent if not asks_pdf or c.value.lower().endswith(".pdf")]
        if len(pool) == 1:
            return [pool[0]], [], missing, ambiguities
        if len(pool) > 1:
            return [], pool[:5], missing, ["target_file"]
        missing.append("target_file")
        return [], [], missing, ambiguities

    if asks_pdf:
        pdfs = [
            TargetCandidate("file", f, f, "workspace_pdf", 0.75)
            for f in files if f.lower().endswith(".pdf")
        ]
        if len(pdfs) == 1:
            return [pdfs[0]], [], missing, ambiguities
        if len(pdfs) > 1:
            return [], pdfs[:5], missing, ["target_file"]
        missing.append("target_file")
        return [], [], missing, ambiguities

    return targets, candidates, missing, ambiguities


def _risk_for(task_type: str, text: str) -> str:
    if task_type == "paper_review":
        return "local_write"
    if task_type in {"calendar_mutation"}:
        return "calendar_write"
    if task_type in {"shell"}:
        return "shell_exec"
    if task_type in {"git"}:
        return "git"
    if task_type in {"file_rewrite", "file_import"}:
        return "local_write"
    if task_type == "file_summarize" and re.search(r"저장|작성|파일로|save|write", text, re.I):
        return "local_write"
    if task_type in {"web_research", "deep_research"}:
        return "external_read"
    if task_type == "paper_translation":
        return "local_write"
    if re.search(r"삭제|지워|trash|delete|remove", text, re.I):
        return "destructive"
    return "read_only"


def _steps_for(task_type: str, targets: List[TargetCandidate]) -> List[str]:
    if task_type == "paper_review":
        return ["resolve_paper_source", "extract_pages", "build_evidence_ledger", "write_senior_review"]
    target = targets[0].value if targets else "대상 확인"
    if task_type == "file_summarize":
        return [f"{target} 읽기", "핵심 내용 요약", "필요하면 답변 또는 파일로 저장"]
    if task_type == "file_rewrite":
        return [f"{target} 읽기", "수정본 생성", "diff 확인"]
    if task_type == "file_read":
        return [f"{target} 읽기", "내용 표시"]
    if task_type == "web_research":
        return ["웹 검색", "결과 선별", "요약/리포트 작성"]
    if task_type == "deep_research":
        return ["웹 원문 수집", "단일 호출 역할별 연구", "격리된 결과 파일 저장"]
    if task_type == "paper_translation":
        return ["PDF 페이지 추출", "번역과 핵심 의미 분리", "한국어 Markdown 저장"]
    if task_type == "calendar_mutation":
        return ["날짜/시간/제목 확인", "일정 변경"]
    return []


def build_task_frame(
    text: str,
    cfg: BuildupConfig,
    current_job: Optional[str],
) -> TaskFrame:
    """Build a deterministic first-pass task frame."""
    task_type = _detect_task_type(text)
    if task_type == "paper_review":
        targets, candidates, missing, ambiguities = [], [], [], []
    else:
        targets, candidates, missing, ambiguities = _resolve_targets(text, current_job, cfg)

    file_task_needs_target = task_type in {
        "file_summarize", "file_rewrite", "file_read", "file_request",
    }
    if file_task_needs_target and not targets and not candidates and "target_file" not in missing:
        if _mentions_file_like(text):
            missing.append("target_file")

    confidence = 0.55
    if task_type not in {"unknown", "chat"}:
        confidence += 0.15
    if targets:
        confidence += 0.2
    if missing or ambiguities:
        confidence -= 0.25
    confidence = max(0.05, min(confidence, 0.95))

    return TaskFrame(
        raw_input=text,
        task_type=task_type,
        goal=text.strip(),
        current_job=current_job,
        targets=targets,
        candidate_targets=candidates,
        missing_slots=missing,
        ambiguities=ambiguities,
        risk=_risk_for(task_type, text),
        confidence=confidence,
        proposed_steps=_steps_for(task_type, targets),
    )



def bind_target_candidate(frame: TaskFrame, target: TargetCandidate) -> TaskFrame:
    """Resolve a previously presented target candidate without reparsing text.

    This is used by the interactive shell after a clarification prompt.  The
    selected candidate is injected directly so that an answer such as "1번"
    cannot be reinterpreted as a paper-shelf index and the original natural
    language request is not reparsed with a filename appended to it.
    """
    missing = [slot for slot in frame.missing_slots if slot != "target_file"]
    ambiguities = [slot for slot in frame.ambiguities if slot != "target_file"]
    return replace(
        frame,
        targets=[target],
        candidate_targets=[],
        missing_slots=missing,
        ambiguities=ambiguities,
        confidence=max(frame.confidence, target.confidence, 0.9),
        proposed_steps=_steps_for(frame.task_type, [target]),
    )

def _looks_like_active_paper_request(frame: TaskFrame) -> bool:
    """Return True when a research-mode utterance should bind to active paper."""
    text = frame.raw_input.lower()
    if frame.task_type in {
        "calendar_query", "calendar_mutation", "search", "web_research",
        "shell", "git", "file_import", "file_rewrite",
    }:
        return False
    if re.search(r"논문|paper|arxiv|pdf|리뷰|review|method|방법론|실험|저자", text, re.I):
        return True
    if frame.task_type in {"summarize", "file_summarize", "file_request", "unknown"}:
        return bool(re.search(r"요약|정리|설명|읽어|summar|explain|read", text, re.I))
    return False


def bind_active_paper_target(
    frame: TaskFrame,
    paper_id: Optional[str],
    *,
    paper_title: str = "",
) -> TaskFrame:
    """Resolve research-mode paper references to the selected active paper.

    This prevents the generic file-clarification layer from blocking requests
    like "이 논문 요약해줘" after the user has already selected a paper shelf item.
    """
    if not paper_id or not _looks_like_active_paper_request(frame):
        return frame

    target = TargetCandidate(
        kind="active_paper",
        value=paper_id,
        label=paper_title or paper_id,
        source="buildup_research_active_paper",
        confidence=0.96,
    )
    missing = [slot for slot in frame.missing_slots if slot != "target_file"]
    ambiguities = [slot for slot in frame.ambiguities if slot != "target_file"]
    if frame.task_type == "paper_translation":
        task_type = frame.task_type
    else:
        task_type = "paper_summarize" if re.search(r"요약|정리|summar", frame.raw_input, re.I) else "paper_request"
    return replace(
        frame,
        task_type=task_type,
        targets=[target],
        candidate_targets=[],
        missing_slots=missing,
        ambiguities=ambiguities,
        risk="read_only",
        confidence=max(frame.confidence, 0.9),
        proposed_steps=[
            "active paper 확인",
            "summary/review/evidence 읽기",
            "논문 내용 기반으로 답변",
        ],
    )


def frame_clarification(frame: TaskFrame) -> str:
    """Human-friendly clarification prompt for a blocking TaskFrame."""
    if not frame.has_blocking_clarification():
        return ""

    lines: List[str] = []
    if "target_file" in frame.missing_slots:
        if not frame.current_job:
            lines.append("현재 job이 없어서 어떤 파일을 대상으로 할지 알 수 없어요. 먼저 job을 만들거나 사용할 job을 선택해 주세요.")
        else:
            lines.append("어떤 파일을 대상으로 할지 알 수 없어요. 파일명을 조금 더 구체적으로 알려주세요.")

    if "target_file" in frame.ambiguities and frame.candidate_targets:
        lines.append("대상 파일 후보가 여러 개 있어요:")
        for idx, cand in enumerate(frame.candidate_targets[:5], 1):
            lines.append(f"{idx}. {cand.value}  ({cand.source})")
        lines.append("파일명이나 번호로 다시 말해 주세요.")

    return "\n".join(lines).strip()


def enrich_intent_with_frame(intent: Dict[str, Any], frame: TaskFrame) -> Dict[str, Any]:
    """Fill obvious missing intent params from the resolved task frame."""
    if not intent or not frame.targets:
        return intent
    enriched = dict(intent)
    params = dict(enriched.get("params") or {})
    name = str(enriched.get("intent") or "")
    target = frame.targets[0].value

    if name in {"read", "rewrite", "trash"} and not params.get("relpath"):
        params["relpath"] = target
    elif name in {"import"} and not params.get("path"):
        params["path"] = target
    elif name == "paper_translate" and not params.get("source"):
        params["source"] = target

    enriched["params"] = params
    return enriched


def format_task_frame_debug(frame: TaskFrame) -> str:
    return json.dumps(frame.to_dict(), ensure_ascii=False, indent=2)


def format_execution_preview(frame: TaskFrame, intent: Dict[str, Any], reason: str = "") -> str:
    """Short preview for confirmation prompts."""
    params = intent.get("params", {}) if intent else {}
    lines = [
        "이렇게 이해했어요:",
        f"- 작업: {intent.get('description') or frame.goal}",
        f"- 유형: {frame.task_type}",
        f"- 위험도: {frame.risk}",
    ]
    if frame.targets:
        lines.append(f"- 대상: {', '.join(t.value for t in frame.targets)}")
    elif params.get("relpath"):
        lines.append(f"- 대상: {params.get('relpath')}")
    elif params.get("path"):
        lines.append(f"- 대상: {params.get('path')}")
    if reason:
        lines.append(f"- 확인 이유: {reason}")
    if frame.proposed_steps:
        lines.append("- 예상 단계: " + " -> ".join(frame.proposed_steps))
    lines.append("")
    lines.append("진행할까요? (y/n)")
    return "\n".join(lines)
