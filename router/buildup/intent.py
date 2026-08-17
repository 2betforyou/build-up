"""Natural language → structured command intent parser.

Architecture: two-tier intent resolution.

    Tier 1 — Rule-based pre-filter (regex + keyword matching)
        Fast, deterministic, no LLM call needed.
        Handles ~80 % of common natural-language requests.

    Tier 2 — LLM-assisted parsing (Ollama fast model)
        Used only when Tier 1 cannot determine intent with confidence.
        Enhanced with few-shot examples for better JSON reliability.

The public API is unchanged: call ``parse_intent()`` and get back either a
structured intent dict or ``None`` (meaning "just chat").
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from buildup.config import BuildupConfig
from buildup.ollama import chat
from buildup.paths import list_files
from buildup.prompts import date_context
from buildup.state import get_current_job


# ============================================================
# Date / time helpers
# ============================================================

def _get_next_weekday(target_weekday: int) -> str:
    """Return the next occurrence of *target_weekday* (0=Mon … 6=Sun)."""
    now = datetime.now()
    days_ahead = target_weekday - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def _get_next_monday() -> str:
    return _get_next_weekday(0)


_WEEKDAY_MAP: Dict[str, int] = {
    "월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3,
    "금요일": 4, "토요일": 5, "일요일": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _resolve_date_expr(text: str) -> Optional[str]:
    """Try to resolve a Korean/English relative-date expression to YYYY-MM-DD.

    Returns None if no known expression is found.
    """
    now = datetime.now()
    t = text.strip()

    # Absolute date already?
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # "4월 5일", "4/5" style
    m = re.search(r"(\d{1,2})\s*[월/]\s*(\d{1,2})\s*일?", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year
        candidate = datetime(year, month, day)
        if candidate.date() < now.date():
            year += 1
        return f"{year}-{month:02d}-{day:02d}"

    # Relative expressions
    if re.search(r"오늘|today", t, re.I):
        return now.strftime("%Y-%m-%d")
    if re.search(r"내일|tomorrow", t, re.I):
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if re.search(r"모레|내일\s*모레|the\s*day\s*after\s*tomorrow", t, re.I):
        return (now + timedelta(days=2)).strftime("%Y-%m-%d")
    if re.search(r"글피", t):
        return (now + timedelta(days=3)).strftime("%Y-%m-%d")

    # "다음 주 X요일" / "이번 주 X요일"
    for wd_name, wd_num in _WEEKDAY_MAP.items():
        if wd_name in t.lower():
            if re.search(r"다음\s*주|next\s*week", t, re.I):
                # next week's weekday
                days_ahead = wd_num - now.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                days_ahead += 7  # next week
                # but cap: if already > 7 ahead, subtract 7
                if days_ahead > 14:
                    days_ahead -= 7
                return (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
            else:
                # this week or next occurrence
                return _get_next_weekday(wd_num)

    # "이번 주" without specific day
    if re.search(r"이번\s*주|this\s*week", t, re.I):
        return None  # ambiguous, let LLM handle or default
    if re.search(r"다음\s*주|next\s*week", t, re.I):
        return (now + timedelta(days=(7 - now.weekday()))).strftime("%Y-%m-%d")  # next Monday

    return None


def _resolve_time_expr(text: str) -> Optional[str]:
    """Try to extract HH:MM from natural language."""
    # Explicit HH:MM
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"

    # "오후 3시 30분", "오전 10시", "3시 반"
    m = re.search(
        r"(오전|오후|아침|저녁|밤)?\s*(\d{1,2})\s*시\s*(?:(\d{1,2})\s*분|반)?",
        text,
    )
    if m:
        period, hour, minute = m.group(1), int(m.group(2)), m.group(3)
        mins = 30 if "반" in text[m.start():m.end() + 2] else (int(minute) if minute else 0)
        if period in ("오후", "저녁", "밤") and hour < 12:
            hour += 12
        elif period == "오전" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= mins <= 59:
            return f"{hour:02d}:{mins:02d}"

    # English: "3pm", "3:30pm", "at 15:00"
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 0
        if m.group(3).lower() == "pm" and h < 12:
            h += 12
        elif m.group(3).lower() == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"

    return None


# ============================================================
# Tier 1: Rule-based intent patterns
# ============================================================

_DEEP_RESEARCH_RE = re.compile(r"(?:딥\s*리서치|심층\s*리서치|deep\s*research)", re.I)


def _rule_wiki(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect explicit Knowledge Vault actions without asking a model."""
    claim_match = re.search(r"\b(C[0-9A-Fa-f]{14})\b", text)
    if claim_match and re.search(r"(?:확인|검증)(?:했어|했다|했음|완료)", text):
        return {
            "intent": "wiki_verify",
            "params": {"claim_id": claim_match.group(1).upper()},
            "description": f"Wiki claim 검증 승격: {claim_match.group(1).upper()}",
        }
    if claim_match and re.search(r"(?:잘못|틀렸|거짓|reject|기각)", text, re.I):
        return {
            "intent": "wiki_reject",
            "params": {"claim_id": claim_match.group(1).upper()},
            "description": f"Wiki claim 기각: {claim_match.group(1).upper()}",
        }
    if re.search(r"(?:위키|지식)\s*(?:상태)?\s*(?:점검|검사|lint)", text, re.I):
        return {
            "intent": "wiki_lint",
            "params": {},
            "description": "Knowledge Vault schema·citation·staleness 점검",
        }
    if re.search(r"(?:충돌하는|disputed|stale).*(?:주장|claim|지식)", text, re.I):
        return {
            "intent": "wiki_review",
            "params": {},
            "description": "검토가 필요한 Wiki claim 조회",
        }
    if re.search(r"(?:이\s*)?논문.*기존\s*지식.*충돌", text, re.I):
        return {
            "intent": "wiki_review",
            "params": {},
            "description": "논문과 기존 grounded claim의 충돌 검토",
        }
    if re.search(r"(?:리서치|연구).*(?:지식|위키).*(?:반영|추가|넣)", text, re.I):
        return {
            "intent": "wiki_add",
            "params": {"selector": "latest"},
            "description": "최근 완료 research를 Knowledge Vault에 반영",
        }
    match = re.fullmatch(
        r"내가\s+(.+?)(?:에\s*대해)?\s*지금까지\s*(?:뭘|무엇을)\s*"
        r"알고\s*있(?:지|어)?[?？]?",
        text.strip(),
        re.I,
    )
    if match:
        return {
            "intent": "wiki_ask",
            "params": {"query": match.group(1).strip()},
            "description": f"Knowledge Vault 질문: {match.group(1).strip()}",
        }
    return None


def _rule_deep_research(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect an explicit request for the adaptive research workflow."""
    if not _DEEP_RESEARCH_RE.search(text):
        return None

    query = _DEEP_RESEARCH_RE.sub(" ", text)
    query = re.sub(
        r"(?:해\s*줘|해주세요|해줘|진행\s*해\s*줘|수행\s*해\s*줘|"
        r"조사\s*해\s*줘|만들어\s*줘|작성\s*해\s*줘)",
        " ",
        query,
        flags=re.I,
    )
    query = re.sub(r"(?:에\s*대해|에\s*관해|관해서)\s*$", "", query).strip()
    query = re.sub(r"^(?:로|으로)\s+|\s+(?:로|으로)$", "", query).strip(" .,:;-")
    query = re.sub(r"(?:을|를)\s*$", "", query).strip()
    if not query:
        return None
    return {
        "intent": "deep_research",
        "params": {"query": query},
        "description": f"근거 기반 적응형 딥 리서치: {query}",
    }


def _rule_paper_translate(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect PDF/paper translation while leaving ordinary translation to chat."""
    if not re.search(r"번역|translate", text, re.I):
        return None
    if not re.search(r"\.pdf\b|논문|paper|이\s*거|이것", text, re.I):
        return None
    if re.search(
        r"abstract|초록|introduction|서론|method(?:ology)?|방법론|"
        r"experiment|실험|result|결과|conclusion|결론|"
        r"\d+\s*(?:페이지|쪽)|page\s*\d+",
        text,
        re.I,
    ):
        return None  # scoped translation stays in the active-paper agent workflow

    source = ""
    quoted = re.search(r"[\"']([^\"']+\.pdf)[\"']", text, re.I)
    if quoted:
        source = quoted.group(1).strip()
    else:
        unquoted = re.search(r"((?:~|/|\.{1,2}/)?[^\s\"'<>]+\.pdf)\b", text, re.I)
        if unquoted:
            source = unquoted.group(1).strip()
    return {
        "intent": "paper_translate",
        "params": {"source": source},
        "description": f"논문 PDF 번역{': ' + source if source else ' (active paper)'}",
    }


def _rule_search(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect web search intent. Distinguishes simple search vs deep_search.
    
    deep_search triggers when:
      - "정리해줘", "정리", "요약", "분석", "보고서", "파일로" etc.
      - OR complex research-style queries (동향, 서베이, 리뷰)
    """
    search_signals = (
        r"(검색|찾아\s*봐|찾아\s*줘|서치|search|look\s*up)",
        r"(최근|최신|latest|recent).*(동향|논문|뉴스|소식|트렌드|paper|news)",
        r"(인터넷|웹|web)에서\s*(찾|검색|조사)",
        r"(동향|트렌드|서베이|survey).*(정리|분석|조사|요약|알려|보여)",
        r"(정리|분석|조사|요약).*(동향|트렌드|서베이|survey|논문|뉴스)",
    )
    if not any(re.search(pat, text, re.I) for pat in search_signals):
        return None

    # Detect if this is a deep/research search
    deep_signals = (
        r"정리",
        r"요약",
        r"분석",
        r"보고서",
        r"파일로",
        r"저장",
        r"문서로",
        r"md로",
        r"마크다운",
        r"리포트",
        r"report",
        r"summarize",
        r"analyze",
        r"동향",
        r"서베이",
        r"survey",
        r"리뷰",
        r"review",
        r"조사",
    )
    is_deep = any(re.search(pat, text, re.I) for pat in deep_signals)

    # Extract query: remove action words
    query = text
    for pat in [
        r"(인터넷|웹|web)에서\s*",
        r"(검색|서치)\s*(해\s*줘|해|줘)?",
        r"(찾아\s*봐|찾아\s*줘)",
        r"(좀|한번|한 번)\s*",
        r"(search|look\s*up)\s*(for)?\s*",
        r"(정리\s*해\s*줘|정리해\s*줘|정리\s*줘|정리)",
        r"(요약\s*해\s*줘|요약해\s*줘|요약\s*줘)",
        r"(분석\s*해\s*줘|분석해\s*줘|분석\s*줘)",
        r"(파일로|문서로|md로|마크다운으로)\s*(저장|만들어|작성)?\s*(해\s*줘|줘)?",
        r"(보고서|리포트)\s*(작성|만들어)?\s*(해\s*줘|줘)?",
    ]:
        query = re.sub(pat, "", query, flags=re.I)
    query = re.sub(r"\s+", " ", query).strip()

    if not query:
        return None

    # Determine whether to save to file
    save_to_file = bool(re.search(
        r"(파일로|저장|문서로|md로|마크다운|save|file)", text, re.I
    ))

    if is_deep:
        return {
            "intent": "deep_search",
            "params": {
                "query": query,
                "save_to_file": save_to_file,
            },
            "description": f"심층 검색 + 분석: {query}"
                           f"{' (파일 저장)' if save_to_file else ''}",
        }

    return {
        "intent": "search",
        "params": {"query": query},
        "description": f"웹 검색: {query}",
    }


def _rule_files(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect file listing intent."""
    if re.search(
        r"(파일|파일들|파일\s*목록|파일\s*리스트|files)\s*(보여|목록|리스트|list|확인|뭐|있)",
        text, re.I,
    ):
        return {
            "intent": "files",
            "params": {},
            "description": "현재 job 파일 목록 조회",
        }
    if re.search(r"^(파일|files)\s*$", text.strip(), re.I):
        return {
            "intent": "files",
            "params": {},
            "description": "현재 job 파일 목록 조회",
        }
    return None


def _rule_import(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect file import intent."""
    import_signals = (
        r"(가져와|가져오|가져다|import|불러와|불러오|복사해|복사\s*해\s*줘|갖고\s*와)",
    )
    if not any(re.search(pat, text, re.I) for pat in import_signals):
        return None

    # Try to find a file path
    m = re.search(r"((?:~/|/|\./)[\w/.\\-]+\.\w+)", text)
    if not m:
        # Try bare filename
        m = re.search(r"([\w.-]+\.\w{1,6})", text)
    path = m.group(1) if m else ""

    if not path:
        return None  # can't import without a path

    return {
        "intent": "import",
        "params": {"path": path},
        "description": f"'{path}'를 현재 job으로 가져오기",
    }


def _rule_write(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect new-file creation with content intent."""
    create_signals = (
        r"(새로\s*(만들|작성)|새\s*파일|새로운\s*파일|create\s*file|write\s*file)",
        r"(파일[을를]?\s*(만들[어]?|생성|작성))\s*(줘|해|해\s*줘)?",
    )
    if not any(re.search(pat, text, re.I) for pat in create_signals):
        return None

    # Must have write/save signal
    if not re.search(r"(저장|작성|쓰[어]?\s*줘|write|save)", text, re.I):
        return None

    file_match = re.search(r"([\w.-]+\.\w{1,6})", text)
    relpath = file_match.group(1) if file_match else ""

    # Extract content after colon
    content = ""
    colon_match = re.search(r"[:：]\s*(.+)$", text, re.DOTALL)
    if colon_match:
        content = colon_match.group(1).strip()

    return {
        "intent": "write",
        "params": {"relpath": relpath, "content": content},
        "description": f"'{relpath}' 파일 생성 및 내용 저장",
    }


def _rule_read(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect file read intent."""
    # Exclude write-context: "내용을 저장" should not match as read
    if re.search(r"내용[을를]?\s*(저장|쓰[어]?|작성)", text, re.I):
        return None

    read_signals = (
        r"(읽어|읽어\s*줘|보여\s*줘|열어|열어\s*줘|내용\s*보여|내용\s*확인|내용\s*봐|확인해\s*줘|출력)\s*(해\s*줘|줘)?",
    )
    # Must reference a file
    file_match = re.search(r"([\w.-]+\.\w{1,6})", text)
    if not file_match:
        return None
    if not any(re.search(pat, text, re.I) for pat in read_signals):
        return None

    relpath = file_match.group(1)

    # Check if it's a PDF
    if relpath.lower().endswith(".pdf"):
        return {
            "intent": "read",
            "params": {"relpath": relpath},
            "description": f"'{relpath}' 파일 읽기",
        }

    return {
        "intent": "read",
        "params": {"relpath": relpath},
        "description": f"'{relpath}' 파일 읽기",
    }


def _rule_rewrite(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect file rewrite intent."""
    rewrite_signals = (
        r"(수정|편집|고쳐|다듬어|다듬|수정해|고치|보완|개선|rewrite|edit|fix|refine|polish)",
    )
    file_match = re.search(r"([\w.-]+\.\w{1,6})", text)
    if not file_match:
        return None
    if not any(re.search(pat, text, re.I) for pat in rewrite_signals):
        return None

    relpath = file_match.group(1)

    # Extract instruction: the part that describes what to do
    instruction = text
    # Remove file reference
    instruction = instruction.replace(relpath, "")
    # Remove common action verbs (keep the descriptive part)
    for pat in [
        r"(파일)?\s*(을|를)?\s*",
        r"(좀|한번|한 번)\s*",
        r"(해\s*줘|줘|해\s*봐)\s*$",
    ]:
        instruction = re.sub(pat, "", instruction, flags=re.I)
    instruction = re.sub(r"\s+", " ", instruction).strip()

    if not instruction:
        instruction = "내용을 다듬어줘"

    return {
        "intent": "rewrite",
        "params": {"relpath": relpath, "instruction": instruction},
        "description": f"'{relpath}' 수정: {instruction}",
    }


def _rule_trash(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect file trash/delete intent."""
    trash_signals = (
        r"(삭제|지워|지우|없애|버려|제거|trash|delete|remove)",
    )
    file_match = re.search(r"([\w.-]+\.\w{1,6})", text)
    if not file_match:
        return None
    if not any(re.search(pat, text, re.I) for pat in trash_signals):
        return None

    # Make sure this is about a file, not a calendar event
    if re.search(r"일정|이벤트|event|schedule|약속|미팅|회의", text, re.I):
        return None

    relpath = file_match.group(1)
    return {
        "intent": "trash",
        "params": {"relpath": relpath},
        "description": f"'{relpath}' 파일을 trash로 이동",
    }


def _rule_export(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect job export intent."""
    if not re.search(r"(내보내|export|추출|반출)", text, re.I):
        return None
    # Make sure it's not ICS export
    if re.search(r"(ics|캘린더|calendar|일정)", text, re.I):
        return None

    # Try to find destination path
    m = re.search(r"((?:~/|/|\./)[\w/.\\-]+)", text)
    dest = m.group(1) if m else ""
    # Strip trailing Korean particles
    if dest:
        dest = re.sub(r"[으로에]$", "", dest)

    return {
        "intent": "export",
        "params": {"dest": dest} if dest else {},
        "description": f"현재 job 내보내기{' → ' + dest if dest else ''}",
    }


def _rule_job_new(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect new job creation intent."""
    if not re.search(
        r"(새\s*(작업|프로젝트|job)|작업\s*(만들|생성|시작)|프로젝트\s*(만들|생성|시작)|"
        r"새로\s*(시작|만들)|new\s*(job|project|task))",
        text, re.I,
    ):
        return None

    # Try to extract label
    label = text
    for pat in [
        r"새\s*(작업|프로젝트|job)\s*(폴더)?\s*(만들|생성|시작)\s*(해\s*줘|줘)?",
        r"(작업|프로젝트)\s*(만들|생성|시작)\s*(해\s*줘|줘)?",
        r"new\s*(job|project|task)\s*",
    ]:
        label = re.sub(pat, "", label, flags=re.I)
    label = re.sub(r"\s+", " ", label).strip()
    label = label.strip("을를이가로 ") or None

    # Template detection
    template = None
    for tpl_name in ("paper", "code", "report", "ops"):
        if tpl_name in text.lower():
            template = tpl_name
            break
    if re.search(r"논문|paper|학술", text, re.I):
        template = template or "paper"
    elif re.search(r"코드|code|개발|프로그래밍", text, re.I):
        template = template or "code"
    elif re.search(r"보고서|report|문서", text, re.I):
        template = template or "report"

    return {
        "intent": "job_new",
        "params": {"label": label, "template": template},
        "description": f"새 작업 생성{': ' + label if label else ''}"
                       f"{' (템플릿: ' + template + ')' if template else ''}",
    }


def _rule_job_use(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect job switching intent."""
    if not re.search(
        r"(작업|job|프로젝트)\s*(전환|바꿔|변경|이동|switch|use|change)",
        text, re.I,
    ):
        return None

    # Try to find job ID (timestamp-based)
    m = re.search(r"(\d{8}-\d{6}-[\w-]+)", text)
    if m:
        return {
            "intent": "job_use",
            "params": {"job_id": m.group(1)},
            "description": f"작업 전환: {m.group(1)}",
        }
    # No explicit id in the sentence — offer a picker instead of paying for
    # a full Tier-2 agent call just to ask "which job?".
    return {
        "intent": "job_use",
        "params": {},
        "description": "작업 목록에서 전환할 job 선택",
    }


_SESSION_WORD_RE = re.compile(r"(대화|세션|얘기)")
_SESSION_TEMPORAL_RE = re.compile(r"(이전|저번|아까|지난)")
_SESSION_ACTION_RE = re.compile(r"(목록|리스트|이어|재개|불러|열어|바꿔|전환|변경|보여)")
_SESSION_RESUME_EN_RE = re.compile(r"\bresume\b|continue\s*(chat|session|conversation)", re.I)


def _rule_session_resume(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Detect a request to browse or resume a past conversation.

    Doesn't require the temporal/action word to sit right next to "대화/세션"
    — "저번에 하던 세션 보여줘" should match just as well as "이전 세션".
    """
    matched = _SESSION_RESUME_EN_RE.search(text)
    if not matched and _SESSION_WORD_RE.search(text):
        matched = _SESSION_TEMPORAL_RE.search(text) or _SESSION_ACTION_RE.search(text)
    if not matched:
        return None
    return {
        "intent": "session_resume",
        "params": {},
        "description": "대화 목록에서 이어갈 세션 선택",
    }


# ============================================================
# Tier 1 dispatcher
# ============================================================

# Order matters: more specific patterns first to avoid false matches.
_RULE_CHAIN: List[Tuple[str, Any]] = [
    ("paper_translate", _rule_paper_translate),
    ("search",      _rule_search),
    ("files",       _rule_files),
    ("trash",       _rule_trash),   # before rewrite/read (delete signals)
    ("write",       _rule_write),   # before read (creation signals)
    ("rewrite",     _rule_rewrite),
    ("import",      _rule_import),
    ("read",        _rule_read),
    ("export",      _rule_export),
    ("job_new",     _rule_job_new),
    ("session_resume", _rule_session_resume),
    ("job_use",     _rule_job_use),
]

_PRIORITY_RULES: List[Tuple[str, Any]] = [
    ("wiki", _rule_wiki),
    ("deep_research", _rule_deep_research),
]


_CONDITIONAL_RE = re.compile(
    r"(만약|이라면|않다면|없다면|있다면|경우에?|조건|먼저\s|그\s*다음|그리고\s*나서|"
    r"최상단|최하단|맨\s*위|맨\s*아래|기존[에]?|이미\s*있|이전\s*내용|날짜가\s*없|날짜가\s*있)",
    re.I,
)


def _is_complex_request(text: str) -> bool:
    """Return True when the input is too complex for Tier-1 rule matching.

    Multi-sentence inputs with conditional / sequential logic must be handled
    by the full agent (Tier-2) — rule-based extraction will misfire.
    """
    sentences = [s.strip() for s in re.split(r"[.。！？!?\n]", text) if s.strip()]
    return len(sentences) >= 2 and bool(_CONDITIONAL_RE.search(text))


def _try_rules(text: str, cfg: BuildupConfig) -> Optional[Dict[str, Any]]:
    """Run all Tier-1 rules.  Return the first match or None."""
    for _name, rule_fn in _PRIORITY_RULES:
        result = rule_fn(text, cfg)
        if result is not None:
            return result
    if _is_complex_request(text):
        return None  # too complex → fall through to agent
    for _name, rule_fn in _RULE_CHAIN:
        result = rule_fn(text, cfg)
        if result is not None:
            return result
    return None


# ============================================================
# Tier 2: LLM-assisted parsing (enhanced)
# ============================================================

_FEW_SHOT_EXAMPLES = """
예시:
입력: "todo.md 파일을 새로 만들어서 이 내용을 저장해줘: 논문 정리하기"
출력: {"intent": "write", "params": {"relpath": "todo.md", "content": "논문 정리하기"}, "description": "todo.md 파일 생성 및 내용 저장"}

입력: "draft.md 읽어줘"
출력: {"intent": "read", "params": {"relpath": "draft.md"}, "description": "draft.md 파일 읽기"}

입력: "draft.md 문체를 좀 다듬어줘"
출력: {"intent": "rewrite", "params": {"relpath": "draft.md", "instruction": "문체를 좀 다듬어줘"}, "description": "draft.md 문체 다듬기"}

입력: "transformer attention 최신 논문 찾아봐"
출력: {"intent": "search", "params": {"query": "transformer attention 최신 논문"}, "description": "웹 검색: transformer attention 최신 논문"}

입력: "파일 목록 보여줘"
출력: {"intent": "files", "params": {}, "description": "현재 job 파일 목록 조회"}

입력: "~/Downloads/paper.pdf 가져와"
출력: {"intent": "import", "params": {"path": "~/Downloads/paper.pdf"}, "description": "paper.pdf를 현재 job으로 가져오기"}

입력: "LLM safety에서 logit 기반 방법론을 설명해줘"
출력: {"intent": "chat", "params": {}, "description": "일반 대화"}

입력: "오늘 날씨 어때?"
출력: {"intent": "chat", "params": {}, "description": "일반 대화"}
"""

INTENT_SYSTEM_PROMPT_V2 = """당신은 사용자의 자연어 입력을 build-up CLI 명령으로 변환하는 파서다.

{date_context}
현재 job: {current_job}
현재 job 파일: {file_list}

사용 가능한 intent:
- job_new: 새 작업 생성 (params: label, template)
- job_use: 기존 작업 전환 (params: job_id)
- import: 파일 가져오기 (params: path)
- write: 새 파일 생성 및 내용 저장 (params: relpath, content)
- read: 파일 읽기 (params: relpath)
- rewrite: 파일 수정 (params: relpath, instruction)
- trash: 파일 삭제 (params: relpath)
- export: 내보내기 (params: dest)
- files: 파일 목록 조회 (params: 없음)
- search: 웹 검색 (params: query)
- deep_search: 심층 검색 + 분석 정리 (params: query, save_to_file)
- deep_research: 근거 검증·gap 보충·인용 감사를 포함한 딥 리서치 (params: query)
- paper_translate: 논문 PDF 한국어 번역 (params: source)
- chat: 일반 대화 (질문, 설명 요청, 의견 등 — 명령이 아닌 경우)

{few_shot}

규칙:
1. 반드시 JSON만 반환하라. 다른 텍스트나 설명을 절대 추가하지 마라.
2. 형식: {{"intent": "...", "params": {{...}}, "description": "..."}}
3. 날짜는 YYYY-MM-DD, 시간은 HH:MM 형식으로 변환하라.
4. "내일"={tomorrow}, "모레"={day_after}, "다음주 월요일"={next_monday}
5. 사용자가 정보를 묻거나, 설명을 요청하거나, 의견을 구하면 반드시 "chat"이다.
6. 사용자가 research에게 무언가를 실행하라고 지시할 때만 명령 intent로 분류하라.
7. 현재 job에 있는 파일을 언급하면 해당 파일 작업 intent로 분류하라.
"""


def _build_intent_context(cfg: BuildupConfig) -> Dict[str, str]:
    """Build context dict for the LLM intent prompt."""
    now = datetime.now()
    current_job = get_current_job(cfg, required=False)
    file_list = ""
    if current_job:
        try:
            files = list_files(cfg.workspace_dir / current_job)
            file_list = ", ".join(files[:20]) if files else "(비어 있음)"
        except Exception:
            file_list = "(알 수 없음)"

    # Resolve example dates for few-shot
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    day_after = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    week_end = (now - timedelta(days=now.weekday()) + timedelta(days=6)).strftime("%Y-%m-%d")

    few_shot = _FEW_SHOT_EXAMPLES
    few_shot = few_shot.replace("TOMORROW_DATE", tomorrow)
    few_shot = few_shot.replace("WEEK_START", week_start)
    few_shot = few_shot.replace("WEEK_END", week_end)

    return {
        "date_context": date_context(),
        "current_job": current_job or "(없음)",
        "file_list": file_list or "(없음)",
        "few_shot": few_shot,
        "tomorrow": tomorrow,
        "day_after": day_after,
        "next_monday": _get_next_monday(),
    }


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """Robustly extract a JSON object from LLM output."""
    raw = raw.strip()

    # Strip markdown code fences
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()

    # Try direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Find the outermost { ... }
    # Use a simple brace-matching approach for reliability
    start = raw.find("{")
    if start == -1:
        return None

    depth = 0
    end = -1
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None

    candidate = raw[start:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Try fixing common LLM JSON mistakes
        # Trailing commas before }
        fixed = re.sub(r",\s*}", "}", candidate)
        fixed = re.sub(r",\s*]", "]", fixed)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            return None


def _llm_parse_intent(
    user_input: str,
    cfg: BuildupConfig,
    session: requests.Session,
    logger: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """Tier 2: Use LLM to parse intent with enhanced prompt."""
    context = _build_intent_context(cfg)
    prompt = INTENT_SYSTEM_PROMPT_V2.format(**context)

    try:
        raw = chat(
            session, cfg, cfg.fast_model,
            [{"role": "system", "content": prompt},
             {"role": "user", "content": user_input}],
            keep_alive="2m", logger=logger,
        )
    except Exception as exc:
        logger.warning("LLM intent parse failed: %s", exc)
        return None

    parsed = _extract_json(raw)
    if parsed is None:
        logger.warning("LLM intent JSON extraction failed: %s", raw[:300])
        return None

    intent = parsed.get("intent", "chat")
    if intent == "chat":
        return None

    # Ensure required structure
    if "params" not in parsed:
        parsed["params"] = {}
    if "description" not in parsed:
        parsed["description"] = str(parsed.get("intent", ""))

    return parsed


# ============================================================
# Chat detection heuristic (avoid unnecessary LLM call)
# ============================================================

def _is_likely_chat(text: str) -> bool:
    """Quick heuristic: is this clearly just a question / conversation?

    If True, we skip both rule matching and LLM intent parsing.
    Conservative: only returns True for clearly non-actionable inputs.
    """
    t = text.strip()

    # Very short greetings / questions
    if len(t) < 4:
        return True

    # Pure questions without action-related keywords
    question_only = bool(re.search(r"[?？]$", t))
    has_action_keyword = bool(re.search(
        r"(잡아|넣어|추가|등록|만들|생성|가져|삭제|지워|없애|제거|취소|"
        r"보여|목록|리스트|list|내보내|export|import|수정|편집|고쳐|"
        r"다듬|rewrite|edit|fix|search|검색|찾아|읽어|열어|복사|번역|translate|"
        r"일정|스케줄|schedule|미팅|회의|예약|약속|"
        r"파일|files|작업|job|프로젝트|위키|wiki|지식|claim|반영|검증)",
        t, re.I,
    ))

    if question_only and not has_action_keyword:
        return True

    # Clearly conversational openers
    chat_patterns = (
        r"^(안녕|hi|hello|hey)",
        r"^(고마워|감사|thanks|thank\s*you)",
        r"^(뭐야|뭐지|뭔데)\s*\??$",
        r"(설명해|설명\s*해\s*줘|explain|what\s+is|what\s+are|어떻게|왜|why|how)",
        r"^(맞아|그렇|ㅇㅇ|ㅇㅋ|ㄴㄴ|ok|okay)\s*\??$",
    )
    # Only classify as chat if NO action keywords are present
    if not has_action_keyword:
        for pat in chat_patterns:
            if re.search(pat, t, re.I):
                return True

    return False


# ============================================================
# Public API
# ============================================================

def is_chat(text: str) -> bool:
    """Tier-0 chat detection — no LLM call.  True ⇒ skip all intent parsing."""
    clean = text.strip()
    # A knowledge query is read-only, but it still needs deterministic vault
    # routing rather than an unconstrained chat answer.
    if re.fullmatch(
        r"내가\s+.+?(?:에\s*대해)?\s*지금까지\s*(?:뭘|무엇을)\s*"
        r"알고\s*있(?:지|어)?[?？]?",
        clean,
        re.I,
    ):
        return False
    return _is_likely_chat(clean)


def parse_intent_instant(
    user_input: str,
    cfg: BuildupConfig,
) -> Optional[Dict[str, Any]]:
    """Tier-0 + Tier-1 only — zero LLM calls.

    Returns a structured intent dict if a rule matches, or None when the input
    is clearly chat or no rule fires (→ caller should fall through to agent).
    """
    text = user_input.strip()
    if not text:
        return None
    wiki = _rule_wiki(text, cfg)
    if wiki is not None:
        return wiki
    if _is_likely_chat(text):
        return None
    return _try_rules(text, cfg)


def parse_intent(
    user_input: str,
    cfg: BuildupConfig,
    session: requests.Session,
    logger: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """Parse natural language into a structured intent.

    Two-tier resolution:
        1. Rule-based pattern matching (fast, deterministic)
        2. LLM-assisted parsing (fallback for ambiguous inputs)

    Returns a dict with 'intent', 'params', 'description', or None if chat.
    """
    text = user_input.strip()
    if not text:
        return None

    wiki = _rule_wiki(text, cfg)
    if wiki is not None:
        logger.info("Intent: %s (rule-based)", wiki.get("intent"))
        return wiki

    # Quick chat detection → skip everything
    if _is_likely_chat(text):
        logger.debug("Intent: chat (heuristic)")
        return None

    # Tier 1: Rule-based
    result = _try_rules(text, cfg)
    if result is not None:
        logger.info("Intent: %s (rule-based)", result.get("intent"))
        return result

    # Tier 2: LLM-assisted
    logger.info("Intent: falling back to LLM parsing")
    result = _llm_parse_intent(text, cfg, session, logger)
    if result is not None:
        logger.info("Intent: %s (LLM)", result.get("intent"))
    else:
        logger.debug("Intent: chat (LLM fallback)")
    return result


# ============================================================
# Multi-step intent splitting
# ============================================================

_STEP_SEP = re.compile(
    r"(?:^|(?<=[줘요다\.\!]))\s*"
    r"(?:그리고|그\s*다음(?:에|으로)?|그\s*후(?:에)?|이후에?|그런\s*다음(?:에)?|다음으로)\s+",
    re.I,
)

_ACTION_KEYWORD = re.compile(
    r"(잡아|추가|등록|만들|생성|가져|삭제|지워|없애|제거|취소|"
    r"보여|목록|내보내|export|import|수정|편집|고쳐|다듬|rewrite|"
    r"검색|찾아|읽어|열어|저장|작성|써|write|search|파일|job|일정)",
    re.I,
)


def parse_multi_intents(
    user_input: str,
    cfg: BuildupConfig,
    session: "requests.Session",
    logger: "logging.Logger",
) -> List[Dict[str, Any]]:
    """Split user input into sequential steps and parse each as an intent.

    Returns a list of intent dicts (length ≥ 2) if multi-step is detected,
    otherwise returns an empty list (caller should fall back to parse_intent).
    """
    text = user_input.strip()

    # Split on sequential connectors
    parts = _STEP_SEP.split(text)
    parts = [p.strip() for p in parts if p.strip()]

    # Need at least 2 parts, each with an action keyword
    if len(parts) < 2:
        return []
    if not all(_ACTION_KEYWORD.search(p) for p in parts):
        return []

    results: List[Dict[str, Any]] = []
    for part in parts:
        intent = parse_intent(part, cfg, session, logger)
        if intent is None or intent.get("intent") == "chat":
            # If any part can't be parsed as an action, abort multi-step
            return []
        results.append(intent)

    logger.info("Multi-step intent: %d steps detected", len(results))
    return results
