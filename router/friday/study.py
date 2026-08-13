"""Project-scoped personal study workflow for build-up."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from friday.config import FridayConfig
from friday.paths import ensure_within, slugify
from friday.state import job_dir


STUDY_COACH_RULES = """[build-up study coach]
- 학습자가 먼저 설명하거나 시도하기 전에는 완성된 풀이를 바로 주지 않는다.
- 한 번에 핵심 질문 하나만 제시한다.
- 답변을 정확성, 빠진 가정, 반례 가능성 기준으로 평가한다.
- 출처의 주장, night의 해석, 추가 추론을 명확히 구분한다.
- 학습자가 직접 설명하거나 검증한 내용만 verified note 후보로 제안한다.
- 막히면 정답 대신 더 작은 힌트부터 제공한다.
"""


@dataclass(frozen=True)
class StudyInfo:
    study_id: str
    topic: str
    job_id: str
    status: str
    created_at: str
    updated_at: str
    source_research: str
    next_action: str
    review_dates: List[str]
    completed_reviews: List[str]
    calendar_event_ids: List[str]
    verified_notes_count: int
    path: Path


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _study_root(job_id: str, cfg: FridayConfig) -> Path:
    return ensure_within(job_dir(job_id, cfg) / "study", job_dir(job_id, cfg))


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".study_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _from_payload(path: Path, payload: Dict[str, Any]) -> StudyInfo:
    reviews = payload.get("review_dates", [])
    if not isinstance(reviews, list):
        reviews = []
    completed_reviews = payload.get("completed_reviews", [])
    if not isinstance(completed_reviews, list):
        completed_reviews = []
    calendar_event_ids = payload.get("calendar_event_ids", [])
    if not isinstance(calendar_event_ids, list):
        calendar_event_ids = []
    return StudyInfo(
        study_id=str(payload["study_id"]),
        topic=str(payload.get("topic") or ""),
        job_id=str(payload["job_id"]),
        status=str(payload.get("status") or "active"),
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        source_research=str(payload.get("source_research") or ""),
        next_action=str(payload.get("next_action") or ""),
        review_dates=[str(item) for item in reviews],
        completed_reviews=[str(item) for item in completed_reviews],
        calendar_event_ids=[str(item) for item in calendar_event_ids],
        verified_notes_count=int(payload.get("verified_notes_count") or 0),
        path=path.parent,
    )


def start_study(
    topic: str,
    job_id: str,
    cfg: FridayConfig,
    *,
    source_research: Optional[Path] = None,
) -> StudyInfo:
    from friday.sandbox import require_write

    topic = topic.strip()
    if not topic:
        raise ValueError("공부할 주제를 입력해 주세요.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_id = f"{stamp}-{slugify(topic)[:48]}"
    root = _study_root(job_id, cfg)
    target = ensure_within(root / base_id, job_dir(job_id, cfg))
    suffix = 2
    while target.exists():
        target = ensure_within(root / f"{base_id}-{suffix}", job_dir(job_id, cfg))
        suffix += 1
    require_write(target, job_id, cfg, context="study start")
    target.mkdir(parents=True, exist_ok=False)

    today = date.today()
    review_dates = [str(today + timedelta(days=days)) for days in (1, 7, 30)]
    now = _now()
    source = str(source_research.resolve()) if source_research else ""
    payload: Dict[str, Any] = {
        "study_id": target.name,
        "topic": topic,
        "job_id": job_id,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "source_research": source,
        "next_action": "자료를 보지 않고 현재 이해를 3~5문장으로 설명한다.",
        "review_dates": review_dates,
        "completed_reviews": [],
        "calendar_event_ids": [],
        "verified_notes_count": 0,
    }
    _atomic_json(target / "study.json", payload)

    source_line = f"- 연결된 research: `{source}`\n" if source else ""
    (target / "README.md").write_text(
        f"# {topic}\n\n"
        f"- 상태: active\n"
        f"- 생성: {now}\n"
        f"{source_line}"
        f"- 복습: {', '.join(review_dates)}\n\n"
        "## 사용 원칙\n\n"
        "먼저 스스로 설명하고, 예제와 반례로 검증한 뒤 verified note로 남긴다.\n",
        encoding="utf-8",
    )
    (target / "notes.md").write_text(
        "# Verified Notes\n\n"
        "직접 설명하거나 예제·반례로 검증한 내용만 기록합니다.\n\n",
        encoding="utf-8",
    )
    (target / "questions.md").write_text(
        f"# Questions — {topic}\n\n"
        "- [ ] 이 주제를 자료 없이 설명할 수 있는가?\n"
        "- [ ] 직접 예제를 만들 수 있는가?\n"
        "- [ ] 실패하는 반례나 한계를 설명할 수 있는가?\n"
        "- [ ] 핵심 가정과 표기법을 구분할 수 있는가?\n",
        encoding="utf-8",
    )
    (target / "progress.md").write_text(
        f"# Progress — {topic}\n\n"
        "- Understanding: 0/5\n"
        "- Verified examples: 0\n"
        "- Verified counterexamples: 0\n"
        "- Blocked on: -\n"
        f"- Next action: {payload['next_action']}\n",
        encoding="utf-8",
    )
    info = _from_payload(target / "study.json", payload)
    return schedule_study_reviews(info, cfg)


def load_study(study_id: str, job_id: str, cfg: FridayConfig) -> Optional[StudyInfo]:
    path = ensure_within(_study_root(job_id, cfg) / study_id / "study.json", job_dir(job_id, cfg))
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _from_payload(path, payload) if isinstance(payload, dict) else None


def list_studies(job_id: str, cfg: FridayConfig, *, include_closed: bool = True) -> List[StudyInfo]:
    root = _study_root(job_id, cfg)
    if not root.exists():
        return []
    studies: List[StudyInfo] = []
    for manifest in root.glob("*/study.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            info = _from_payload(manifest, payload)
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if include_closed or info.status == "active":
            studies.append(info)
    return sorted(studies, key=lambda item: item.updated_at, reverse=True)


def resolve_study(selector: str, job_id: str, cfg: FridayConfig) -> Optional[StudyInfo]:
    value = selector.strip()
    studies = list_studies(job_id, cfg)
    if not studies:
        return None
    if not value or value.lower() in {"latest", "최근", "마지막"}:
        return studies[0]
    if value.isdigit():
        index = int(value)
        return studies[index - 1] if 1 <= index <= len(studies) else None
    exact = load_study(value, job_id, cfg)
    if exact:
        return exact
    matches = [item for item in studies if item.study_id.startswith(value)]
    return matches[0] if len(matches) == 1 else None


def _update_study(info: StudyInfo, cfg: FridayConfig, **changes: Any) -> StudyInfo:
    manifest = info.path / "study.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.update(changes)
    payload["updated_at"] = _now()
    _atomic_json(manifest, payload)
    return _from_payload(manifest, payload)


def append_study_note(info: StudyInfo, text: str, cfg: FridayConfig) -> StudyInfo:
    from friday.sandbox import require_write

    note = text.strip()
    if not note:
        raise ValueError("저장할 note가 비어 있습니다.")
    target = ensure_within(info.path / "notes.md", job_dir(info.job_id, cfg))
    require_write(target, info.job_id, cfg, context="study verified note")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"## {stamp}\n\n{note}\n\n")
    return _update_study(
        info,
        cfg,
        verified_notes_count=info.verified_notes_count + 1,
        next_action="방금 기록한 설명의 반례 또는 적용 한계를 만든다.",
    )


def close_study(info: StudyInfo, cfg: FridayConfig, reflection: str = "") -> StudyInfo:
    from friday.sandbox import require_write

    target = ensure_within(info.path / "progress.md", job_dir(info.job_id, cfg))
    require_write(target, info.job_id, cfg, context="study close")
    if reflection.strip():
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## Session reflection\n\n{reflection.strip()}\n")
    return _update_study(
        info,
        cfg,
        status="closed",
        next_action=f"{info.review_dates[0]}에 자료 없이 다시 설명한다." if info.review_dates else "복습한다.",
    )


def schedule_study_reviews(info: StudyInfo, cfg: FridayConfig) -> StudyInfo:
    """Create the D+1/D+7/D+30 calendar entries exactly once."""
    if info.calendar_event_ids:
        return info
    from friday.calendar_mgr import cal_add

    labels = ("D+1", "D+7", "D+30")
    event_ids: List[str] = []
    for index, review_date in enumerate(info.review_dates):
        label = labels[index] if index < len(labels) else f"review {index + 1}"
        event = cal_add(
            f"build-up 복습 · {info.topic} ({label})",
            review_date,
            duration_min=30,
            note=f"study:{info.study_id} · 자료 없이 먼저 설명하기",
            cfg=cfg,
            dedupe_key=f"study:{info.study_id}:{review_date}",
        )
        event_ids.append(str(event["id"]))
    return _update_study(info, cfg, calendar_event_ids=event_ids)


def due_studies(job_id: str, cfg: FridayConfig, *, on_date: Optional[date] = None) -> List[StudyInfo]:
    target = str(on_date or date.today())
    return [
        info for info in list_studies(job_id, cfg)
        if any(
            review <= target and review not in info.completed_reviews
            for review in info.review_dates
        )
    ]


def complete_study_review(
    info: StudyInfo,
    cfg: FridayConfig,
    *,
    on_date: Optional[date] = None,
) -> tuple[StudyInfo, str]:
    """Complete the earliest due review and return its scheduled date."""
    target = str(on_date or date.today())
    pending = sorted(
        review for review in info.review_dates
        if review <= target and review not in info.completed_reviews
    )
    if not pending:
        raise ValueError("현재 완료 처리할 복습 회차가 없습니다.")
    from friday.sandbox import require_write

    completed_date = pending[0]
    completed = sorted({*info.completed_reviews, completed_date})
    future = sorted(review for review in info.review_dates if review not in completed)
    next_action = (
        f"{future[0]}에 자료 없이 다시 설명한다."
        if future else
        "D+1·D+7·D+30 복습을 모두 완료했다. 취약한 개념만 새 질문으로 연다."
    )
    progress = ensure_within(info.path / "progress.md", job_dir(info.job_id, cfg))
    require_write(progress, info.job_id, cfg, context="study review complete")
    with progress.open("a", encoding="utf-8") as handle:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        handle.write(f"\n- Review completed: {completed_date} ({stamp})\n")
    return _update_study(
        info,
        cfg,
        completed_reviews=completed,
        next_action=next_action,
    ), completed_date


def study_context(info: StudyInfo, cfg: FridayConfig, *, max_chars: int = 6_000) -> str:
    """Build bounded coach context without ever truncating the coach rules."""
    required = [
        STUDY_COACH_RULES.strip(),
        f"[active study]\ntopic: {info.topic}\nstatus: {info.status}\n"
        f"verified notes: {info.verified_notes_count}\n"
        f"completed reviews: {len(info.completed_reviews)}/{len(info.review_dates)}\n"
        f"next action: {info.next_action}",
    ]
    optional: List[str] = []
    if info.source_research:
        optional.append(f"linked research: {info.source_research}")
        try:
            source_dir = ensure_within(
                Path(info.source_research).expanduser().resolve(),
                job_dir(info.job_id, cfg),
            )
            guide = source_dir / "07-study-guide.md"
            report = source_dir / "06-report.md"
            if guide.is_file():
                optional.append("[linked study guide]\n" + guide.read_text(encoding="utf-8")[:2_600])
            if report.is_file():
                optional.append("[linked report excerpt]\n" + report.read_text(encoding="utf-8")[:1_400])
        except (OSError, ValueError):
            optional.append("[linked research unavailable or outside the current job]")
    for filename in ("progress.md", "questions.md", "notes.md"):
        path = info.path / filename
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            optional.append(f"[{filename}]\n{text[-900:]}")

    prefix = "\n\n".join(required)
    remaining = max(0, max_chars - len(prefix) - 2)
    tail = "\n\n".join(optional)
    if len(tail) > remaining:
        tail = tail[: max(0, remaining - 17)].rstrip() + "\n...[truncated]"
    return prefix + ("\n\n" + tail if tail else "")


def format_study_list(studies: List[StudyInfo]) -> str:
    if not studies:
        return "아직 공부 세션이 없습니다. `/study start 주제`로 시작하세요."
    lines: List[str] = []
    for index, info in enumerate(studies, start=1):
        lines.append(
            f"{index}. {info.topic} · {info.status} · verified {info.verified_notes_count}\n"
            f"   {info.study_id} · next: {info.next_action}"
        )
    return "\n".join(lines)
