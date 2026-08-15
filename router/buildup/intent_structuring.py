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
calendar          - 일정 관리
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


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """Extract first valid JSON object from LLM output, tolerating think-tags and fences."""
    text = re.sub(r"```\w*\n?", "", raw).strip()
    text = re.sub(
        r"<(?:think|thought|thinking)>[\s\S]*?</(?:think|thought|thinking)>",
        "", text, flags=re.I,
    )
    text = re.sub(r"<(?:think|thought|thinking)>[\s\S]*$", "", text, flags=re.I).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "intent" in parsed:
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
            if isinstance(parsed, dict) and "intent" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass
        pos = end + 1


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
