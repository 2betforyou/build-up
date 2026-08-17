"""Intent Structuring Layer — Section 5.2 of Build-up Technical Design.

Converts raw user text into a StructuredIntent using the fast model.
This runs as the STRUCTURING state in PlanStateMachine before planning,
giving the planner rich context instead of raw natural language.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from buildup.config import BuildupConfig


@dataclass
class StructuredIntent:
    intent: str                              # e.g. "debug", "qa", "summarize"
    interaction_mode: str                    # consult | inspect | draft | execute | workflow
    entities: Dict[str, Any]                 # project, artifact, target_files, etc.
    constraints: Dict[str, Any]             # allow_write, allow_exec, workspace_only
    desired_outputs: List[str]              # what the user wants back
    confidence: float                        # 0.0–1.0
    alternate_intents: List[Dict[str, Any]]  # [{"intent": "...", "score": 0.0}]
    raw: str = field(repr=False)            # original LLM output (for debugging)


_INTENT_ENUMS = """\
qa                - 질문/답변, 정보 조회
search            - 웹 검색
summarize         - 요약
inspect_logs      - 로그 분석
inspect_code      - 코드 분석/설명
code_edit         - 코드 수정/작성
document_write    - 문서 작성
experiment_run    - 실험/스크립트 실행
debug             - 버그 찾기/수정
workflow_multi_step - 여러 단계가 필요한 복합 작업
file_manage       - 파일 관리 (읽기/쓰기/검색)"""

_MODE_ENUMS = """\
consult   - 읽기 전용, 설명/분석만
inspect   - 읽기 + 검색, 쓰기 없음
draft     - 읽기 + 임시 파일 작성
execute   - 안전한 쉘 명령 실행 포함
workflow  - 여러 단계 복합 실행"""

_SYSTEM_PROMPT = f"""\
당신은 사용자 요청을 구조화된 JSON으로 변환하는 분석기다.
출력은 반드시 아래 스키마를 따르는 JSON 객체 하나만이어야 한다. 설명, 주석, 코드펜스 금지.

[intent 후보]
{_INTENT_ENUMS}

[interaction_mode 후보]
{_MODE_ENUMS}

[출력 스키마]
{{
  "intent": "intent_enum",
  "interaction_mode": "mode_enum",
  "entities": {{
    "project": "프로젝트명 또는 null",
    "artifact": "파일명/로그명/대상 또는 null",
    "target_files": []
  }},
  "constraints": {{
    "allow_write": true 또는 false,
    "allow_exec": true 또는 false,
    "workspace_only": true 또는 false
  }},
  "desired_outputs": ["원하는 결과 항목들"],
  "confidence": 0.0~1.0,
  "alternate_intents": [
    {{"intent": "intent_enum", "score": 0.0}}
  ]
}}"""


def _extract_json(raw: str, required_key: str = "intent") -> Optional[Dict[str, Any]]:
    """Extract first valid JSON object from LLM output, tolerating think-tags and fences."""
    text = re.sub(r"```\w*\n?", "", raw).strip()
    text = re.sub(
        r"<(?:think|thought|thinking)>[\s\S]*?</(?:think|thought|thinking)>",
        "", text, flags=re.I,
    )
    text = re.sub(r"<(?:think|thought|thinking)>[\s\S]*$", "", text, flags=re.I).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and required_key in parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            return None
        depth, end = 0, -1
        for i in range(start, len(text)):
            depth += text[i] == "{"
            depth -= text[i] == "}"
            if depth == 0:
                end = i
                break
        if end == -1:
            return None
        candidate = text[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and required_key in parsed:
                return parsed
        except json.JSONDecodeError:
            pass
        pos = end + 1


_RISKY_INTENT_GUIDANCE: Dict[str, str] = {
    "export": (
        "사용자가 현재 job 전체를 내보내라고 명시적으로 요청했는가. "
        "'알아서 해', '네가 판단해서' 같은 막연한 위임 표현은 확인된 요청이 아니다."
    ),
    "import": (
        "사용자가 특정 외부 경로의 파일을 현재 job으로 가져오라고 요청했고, "
        "그 경로가 텍스트에 실제로 적혀 있는가."
    ),
    "trash": (
        "사용자가 특정 파일을 삭제하거나 휴지통으로 옮기라고 요청했고, "
        "어떤 파일인지 텍스트에 실제로 적혀 있는가."
    ),
    "write": (
        "사용자가 새 파일을 만들라고 요청했고, 파일 경로와 저장할 내용이 "
        "텍스트에 실제로 적혀 있는가."
    ),
    "rewrite": (
        "사용자가 기존 파일을 수정하라고 요청했고, 어떤 파일을 어떻게 "
        "고칠지 텍스트에 실제로 적혀 있는가."
    ),
}


def verify_risky_intent(
    intent: str,
    text: str,
    session: requests.Session,
    cfg: BuildupConfig,
    logger: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """Confirm a consequential intent (export/trash/import/write/rewrite) with
    the small struct model and extract its parameters, instead of trusting
    embedding cosine-similarity plus a keyword regex.

    A nearest-neighbor label match on a vague sentence ("just handle it
    yourself") can look like "export" purely by surface similarity to a
    canned example. The struct model actually reads the sentence and checks
    whether the required specifics — a path, a filename, content — are
    really there. Returns None (→ fall through to the full agent) whenever
    the model isn't confident this is really the action, or a required
    parameter is missing.
    """
    from buildup.ollama import chat

    guidance = _RISKY_INTENT_GUIDANCE.get(intent, "")
    system = (
        "당신은 되돌리기 어렵거나 외부에 영향을 주는 작업을 실행하기 전에 "
        "사용자의 요청을 정확히 확인하는 검증기다. 확신이 없으면 "
        '"confirmed": false로 답하라.\n\n'
        f"판단 기준: {guidance}\n\n"
        "반드시 아래 JSON 스키마 하나만 출력하라. 설명, 코드펜스 금지.\n"
        '{"confirmed": true 또는 false, "relpath": "경로 또는 null", '
        '"path": "경로 또는 null", "content": "내용 또는 null", '
        '"instruction": "지시문 또는 null", "dest": "경로 또는 null"}'
    )
    try:
        raw = chat(
            session, cfg, cfg.struct_model,
            [{"role": "system", "content": system},
             {"role": "user", "content": text}],
            keep_alive="10m",
            logger=logger,
            think=False,
            json_mode=True,
        )
    except Exception as exc:
        logger.warning("Risky-intent verification LLM call failed: %s", exc)
        return None

    data = _extract_json(raw, required_key="confirmed")
    if not data or not data.get("confirmed"):
        return None

    if intent == "export":
        return {"dest": data.get("dest") or ""}
    if intent == "import":
        path = data.get("path")
        return {"path": path} if path else None
    if intent == "trash":
        relpath = data.get("relpath")
        return {"relpath": relpath} if relpath else None
    if intent == "write":
        relpath = data.get("relpath")
        if not relpath:
            return None
        return {"relpath": relpath, "content": data.get("content") or ""}
    if intent == "rewrite":
        relpath = data.get("relpath")
        instruction = data.get("instruction")
        if not relpath or not instruction:
            return None
        return {"relpath": relpath, "instruction": instruction}
    return None


def structure_intent(
    text: str,
    session: requests.Session,
    cfg: BuildupConfig,
    logger: logging.Logger,
) -> Optional[StructuredIntent]:
    """Call fast_model to produce a StructuredIntent from raw user text.

    Returns None on any failure so callers can proceed without structuring.
    Failure must never block the planner.
    """
    from buildup.ollama import chat

    try:
        raw = chat(
            session, cfg, cfg.fast_model,
            [{"role": "system", "content": _SYSTEM_PROMPT},
             {"role": "user", "content": text}],
            keep_alive="10m",
            logger=logger,
            think=False,
            json_mode=True,  # force JSON output regardless of instruction following
        )
    except Exception as exc:
        logger.warning("Intent structuring LLM call failed: %s", exc)
        return None

    data = _extract_json(raw)
    if not data:
        logger.warning("Intent structuring: could not parse JSON. raw=%s", raw[:300])
        return None

    try:
        return StructuredIntent(
            intent=str(data.get("intent") or "qa"),
            interaction_mode=str(data.get("interaction_mode") or "consult"),
            entities=dict(data.get("entities") or {}),
            constraints=dict(data.get("constraints") or {}),
            desired_outputs=list(data.get("desired_outputs") or []),
            confidence=float(data.get("confidence") or 0.7),
            alternate_intents=list(data.get("alternate_intents") or []),
            raw=raw,
        )
    except Exception as exc:
        logger.warning("Intent structuring: dataclass init failed: %s", exc)
        return None
