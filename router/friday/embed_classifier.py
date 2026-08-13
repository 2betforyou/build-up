"""Embedding-based intent classifier using Ollama /api/embed.

Architecture
────────────
1. INTENT_EXAMPLES  — ~10 Korean/English example phrases per intent label.
2. init()           — embed all examples in a single batch call; cache to disk.
3. classify()       — embed the query, return (label, max-cosine-similarity).
4. classify_and_extract() — classify + extract structured params via regex/rules.
                            Returns None when confidence is too low or params
                            cannot be extracted (→ caller falls through to agent).

Cache
─────
Example embeddings are stored in  <base_dir>/.embed_cache.json  and keyed by
(md5 of INTENT_EXAMPLES, embed_model).  Any change to examples or model name
invalidates the cache automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from friday.config import FridayConfig

logger = logging.getLogger(__name__)


# ============================================================
# Example phrase bank  (Korean + mixed variants)
# ============================================================

INTENT_EXAMPLES: Dict[str, List[str]] = {
    "read": [
        "todo.md 읽어줘",
        "draft.md 내용 보여줘",
        "notes.md 열어줘",
        "summary.md 출력해줘",
        "파일 확인해줘",
        "report.md 내용이 뭐야",
        "schedule.md 보여줘",
        "readme.md 읽어",
        "파일 내용 알려줘",
        "memo.txt 출력",
        "파일 읽기",
        "파일 내용 봐줘",
        "이 파일 띄워줘",
        "파일 프린트해줘",
    ],
    "write": [
        "todo.md 새로 만들어줘",
        "notes.md 파일 생성해줘",
        "메모를 파일에 저장해줘",
        "todo.md를 만들고 이 내용 넣어줘",
        "새 파일에 이 내용 써줘",
        "draft.md 새로 작성해줘",
        "파일 만들어서 저장",
        "새로 만들어서 저장해줘",
        "파일에 내용 기록해줘",
        "새 파일 생성해서 저장",
        "todo.md에 이 내용 추가해줘",
        "파일에 항목 추가해줘",
        "파일에 이걸 덧붙여줘",
        "파일에 새 항목 넣어줘",
        "파일 맨 위에 이거 추가해줘",
        "목록에 이 내용 추가해줘",
        "파일에 이 일정 추가해줘",
        "파일에 내용 추가",
    ],
    "rewrite": [
        "draft.md 수정해줘",
        "이 파일 좀 다듬어줘",
        "notes.md 고쳐줘",
        "report.md 편집해줘",
        "문체 좀 바꿔줘",
        "draft.md 내용 개선해줘",
        "파일 내용 refine해줘",
        "글 다듬어줘",
        "파일 수정해줘",
        "내용 편집해줘",
        "문체 다듬기",
        "draft.md polish해줘",
    ],
    "files": [
        "파일 목록 보여줘",
        "어떤 파일 있어?",
        "현재 파일들 뭐 있어",
        "파일 리스트",
        "job 안에 뭐 있어?",
        "파일 뭐뭐 있어",
        "목록 확인",
        "파일 현황",
        "파일들 알려줘",
        "어떤 파일들이 있나요",
    ],
    "search": [
        "transformer attention 최신 논문 찾아봐",
        "구글에서 검색해줘",
        "웹에서 찾아줘",
        "LLM 관련 논문 검색",
        "최신 뉴스 찾아줘",
        "검색해줘",
        "인터넷에서 찾아",
        "웹 검색",
        "web search",
        "논문 검색해줘",
        "검색 좀 해줘",
    ],
    "cal_add": [
        "내일 오후 3시에 미팅 잡아줘",
        "다음 주 월요일 10시에 회의 추가해줘",
        "오늘 오후 2시 랩미팅 등록해줘",
        "금요일 점심 약속 추가",
        "일정 새로 잡아줘",
        "스케줄 추가해줘",
        "약속 등록해줘",
        "4월 5일 세미나 넣어줘",
        "미팅 잡아줘",
        "일정 추가",
        "일정 등록해줘",
    ],
    "cal_list": [
        "이번 주 일정 보여줘",
        "다음 주 스케줄 알려줘",
        "오늘 일정 뭐야?",
        "이번 달 일정 확인",
        "캘린더 보여줘",
        "내 일정 알려줘",
        "이번 주 미팅 있어?",
        "내일 뭐 있어?",
        "일정 조회",
        "스케줄 확인해줘",
        "이번 주 뭐 있어?",
    ],
    "cal_delete": [
        "오늘 미팅 취소해줘",
        "내일 회의 삭제해줘",
        "랩미팅 취소",
        "일정 지워줘",
        "스케줄 삭제",
        "약속 취소해줘",
        "일정 취소",
        "미팅 삭제해줘",
    ],
    "trash": [
        "draft.md 삭제해줘",
        "파일 버려줘",
        "notes.md 지워줘",
        "필요없는 파일 삭제",
        "휴지통에 넣어줘",
        "파일 제거",
        "파일 삭제해줘",
        "이 파일 지워",
        "temp.md 버려줘",
    ],
    "import": [
        "파일 가져와줘",
        "~/Downloads/paper.pdf 불러와줘",
        "inbox에서 가져와줘",
        "파일 임포트",
        "가져와",
        "불러와줘",
        "import해줘",
        "파일 불러오기",
        "파일 추가해줘",
    ],
    "export": [
        "job 내보내줘",
        "파일 export해줘",
        "외부로 내보내줘",
        "압축해서 내보내",
        "내보내줘",
        "job 압축",
        "export해줘",
    ],
    "job_new": [
        "새 작업 만들어줘",
        "새 job 생성",
        "새 프로젝트 시작",
        "작업 폴더 새로 만들어줘",
        "작업 만들기",
        "새로운 job 만들어줘",
        "job 새로 만들어줘",
    ],
    "job_use": [
        "작업 전환",
        "다른 job으로 바꿔줘",
        "todolist job 써줘",
        "job 변경",
        "작업 바꿔줘",
        "job 선택해줘",
        "이 job으로 바꿔줘",
    ],
    "chat": [
        "안녕",
        "고마워",
        "잘 됐어",
        "ㅇㅋ",
        "알겠어",
        "그렇구나",
        "transformer가 뭐야?",
        "설명해줘",
        "왜 그래?",
        "어떻게 해?",
        "뭐가 문제야?",
        "좋아",
        "오늘 날씨 어때?",
        "LLM이 뭐야?",
    ],
}

# Minimum cosine similarity to trust the classifier.
# Below this threshold → fall through to agent.
CONFIDENCE_THRESHOLD = 0.75


# ============================================================
# Ollama embed API
# ============================================================

def _embed(texts: List[str], session: requests.Session, cfg: FridayConfig) -> List[List[float]]:
    """Call Ollama embedding API and return embedding vectors.

    Tries /api/embed (Ollama ≥ 0.1.26) first; falls back to the older
    /api/embeddings endpoint (one request per text) on 404.
    """
    # ── Try new batch endpoint (/api/embed) ──────────────────
    try:
        resp = session.post(
            cfg.ollama_embed_url,
            json={"model": cfg.embed_model, "input": texts},
            timeout=30,
        )
        if resp.status_code != 404:
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings")
            if embeddings and len(embeddings) == len(texts):
                return embeddings
            raise RuntimeError(f"임베딩 응답 비정상: {str(data)[:200]}")
    except requests.HTTPError:
        pass  # 404 → fall through to legacy endpoint

    # ── Fall back to legacy endpoint (/api/embeddings) ───────
    legacy_url = cfg.ollama_embed_url.replace("/api/embed", "/api/embeddings")
    results: List[List[float]] = []
    try:
        for text in texts:
            resp = session.post(
                legacy_url,
                json={"model": cfg.embed_model, "prompt": text},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            emb = data.get("embedding")
            if not emb:
                raise RuntimeError(f"legacy 임베딩 응답 비정상: {str(data)[:200]}")
            results.append(emb)
        return results
    except requests.RequestException as exc:
        raise RuntimeError(f"Embedding API 오류: {exc}") from exc


# ============================================================
# Cosine similarity (pure Python — no numpy dependency)
# ============================================================

def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b + 1e-9)


# ============================================================
# Parameter extraction  (regex / existing rule delegation)
# ============================================================

_FILE_RE = re.compile(r"([\w./-]+\.\w{1,6})")
_PATH_RE = re.compile(r"(~?/[\w./-]+|[\w.-]+\.\w{1,6})")
_NOISE_SEARCH = re.compile(
    r"(검색|찾아봐?|알아봐?|search|구글에서?|웹에서?|인터넷에서?)\s*", re.I
)

# Conditional / multi-step language — signals agent-level complexity
_COMPLEX_RE = re.compile(
    r"(만약|이라면|않다면|없다면|있다면|경우에?|조건|먼저|그리고\s*나서|그\s*다음|"
    r"최상단|최하단|맨\s*위|맨\s*아래|기존[에]?|이미\s*있|이전\s*내용|날짜가\s*없|날짜가\s*있)",
    re.I,
)
# Action words that strongly imply write/modify, not read
_WRITE_ACTION_RE = re.compile(
    r"(추가해?줘?|덧붙여?줘?|넣어줘?|써줘?|기록해?줘?|저장해?줘?|포함해?줘?)",
    re.I,
)


def _should_fallthrough_to_agent(intent: str, text: str) -> bool:
    """Return True when the request is too complex for Tier-1 rule extraction.

    Two cases:
    1. write/rewrite intent + conditional language (multi-step logic needed)
    2. "read" classification but text contains write-action words
       (classifier confused; agent should handle the real intent)
    """
    sentences = [s.strip() for s in re.split(r"[.。！？!?\n,，]", text) if s.strip()]

    if intent in ("write", "rewrite"):
        # Multi-clause with conditional logic → agent
        if _COMPLEX_RE.search(text):
            return True
        # Multiple sentences with different instructions → agent
        if len(sentences) >= 3:
            return True

    if intent == "read" and _WRITE_ACTION_RE.search(text):
        # Embed classifier confused a write request for read
        return True

    return False


def _extract_params(
    intent: str,
    text: str,
    cfg: FridayConfig,
) -> Optional[Dict[str, Any]]:
    """Extract structured parameters for a classified intent.

    Returns None if required params cannot be reliably extracted
    (caller should fall through to agent).
    """
    # Calendar intents: delegate to existing rule functions — they handle
    # date/time resolution which is too complex to reproduce here.
    if intent in ("cal_add", "cal_list", "cal_delete", "cal_export", "cal_import"):
        from friday.intent import (  # local import to avoid circular
            _rule_cal_add, _rule_cal_delete, _rule_cal_export,
            _rule_cal_import, _rule_cal_list,
        )
        _CAL_RULES = {
            "cal_add": _rule_cal_add,
            "cal_list": _rule_cal_list,
            "cal_delete": _rule_cal_delete,
            "cal_export": _rule_cal_export,
            "cal_import": _rule_cal_import,
        }
        result = _CAL_RULES[intent](text, cfg)
        return result["params"] if result else None

    if intent == "read":
        m = _FILE_RE.search(text)
        return {"relpath": m.group(1)} if m else None

    if intent == "write":
        m = _FILE_RE.search(text)
        if not m:
            return None
        relpath = m.group(1)
        content_m = re.search(r"[:：]\s*(.+)$", text, re.DOTALL)
        content = content_m.group(1).strip() if content_m else ""
        return {"relpath": relpath, "content": content}

    if intent == "rewrite":
        m = _FILE_RE.search(text)
        if not m:
            return None
        relpath = m.group(1)
        instruction = re.sub(re.escape(relpath), "", text).strip()
        return {"relpath": relpath, "instruction": instruction}

    if intent == "search":
        query = _NOISE_SEARCH.sub("", text).strip() or text
        return {"query": query}

    if intent == "files":
        return {}

    if intent == "trash":
        m = _FILE_RE.search(text)
        return {"relpath": m.group(1)} if m else None

    if intent == "import":
        m = _PATH_RE.search(text)
        return {"path": m.group(1)} if m else None

    if intent == "export":
        return {}

    if intent == "job_new":
        m = re.search(r"(?:이름|라벨|name|label)\s*[：:=]?\s*['\"]?(\S+)['\"]?", text, re.I)
        return {"label": m.group(1) if m else ""}

    if intent == "job_use":
        # Needs an exact job ID — too unreliable to guess from text
        return None

    if intent == "chat":
        return {}

    return None


_DESC_MAP: Dict[str, str] = {
    "read":       "'{relpath}' 파일 읽기",
    "write":      "'{relpath}' 파일 저장",
    "rewrite":    "'{relpath}' 파일 수정",
    "files":      "파일 목록 조회",
    "search":     "'{query}' 검색",
    "trash":      "'{relpath}' 삭제",
    "import":     "'{path}' 가져오기",
    "export":     "Job 내보내기",
    "job_new":    "새 Job 생성",
    "job_use":    "Job 전환",
    "cal_add":    "일정 추가",
    "cal_list":   "일정 조회",
    "cal_delete": "일정 삭제",
    "cal_export": "캘린더 내보내기",
    "cal_import": "캘린더 가져오기",
}


def _make_description(intent: str, params: Dict[str, Any]) -> str:
    tpl = _DESC_MAP.get(intent, intent)
    try:
        return tpl.format(**params)
    except KeyError:
        return tpl.split("'")[0].strip() or intent


# ============================================================
# Classifier
# ============================================================

class IntentEmbedClassifier:
    """Embedding-based intent classifier backed by Ollama /api/embed.

    Usage
    ─────
    classifier = IntentEmbedClassifier(session, cfg)
    classifier.init()   # call once at startup; fast if cache exists

    intent_dict = classifier.classify_and_extract(user_text, cfg)
    # Returns None  → fall through to agent
    # Returns dict  → {"intent": ..., "params": ..., "description": ...}
    """

    def __init__(self, session: requests.Session, cfg: FridayConfig):
        self._session = session
        self._cfg = cfg
        self._example_embeddings: Dict[str, List[List[float]]] = {}
        self.available = False  # True after successful init

    # ── Cache ────────────────────────────────────────────────

    def _cache_path(self) -> Path:
        return self._cfg.base_dir / ".embed_cache.json"

    @staticmethod
    def _examples_hash() -> str:
        flat = json.dumps(INTENT_EXAMPLES, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(flat.encode()).hexdigest()[:12]

    def _load_cache(self) -> bool:
        path = self._cache_path()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("hash") != self._examples_hash():
                return False
            if data.get("model") != self._cfg.embed_model:
                return False
            self._example_embeddings = data["embeddings"]
            return True
        except Exception:
            return False

    def _save_cache(self) -> None:
        try:
            data = {
                "hash": self._examples_hash(),
                "model": self._cfg.embed_model,
                "embeddings": self._example_embeddings,
            }
            self._cache_path().write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("EmbedClassifier: cache save failed: %s", exc)

    # ── Init ─────────────────────────────────────────────────

    def init(self) -> None:
        """Compute (or load cached) example embeddings.

        Silently sets self.available = False on any error so callers can
        gracefully fall back to rule-based routing.
        """
        if self.available:
            return

        if self._load_cache():
            logger.info(
                "EmbedClassifier: loaded cache  model=%s  intents=%d",
                self._cfg.embed_model, len(self._example_embeddings),
            )
            self.available = True
            return

        logger.info("EmbedClassifier: computing example embeddings (model=%s)…", self._cfg.embed_model)
        all_texts: List[str] = []
        all_labels: List[str] = []
        for intent_label, examples in INTENT_EXAMPLES.items():
            for ex in examples:
                all_texts.append(ex)
                all_labels.append(intent_label)

        try:
            embeddings = _embed(all_texts, self._session, self._cfg)
        except Exception as exc:
            hint = ""
            if "404" in str(exc) or "model" in str(exc).lower():
                hint = f"  →  'ollama pull {self._cfg.embed_model}' 으로 모델을 먼저 설치하세요."
            logger.warning("EmbedClassifier: init failed (%s)%s — rule-only routing으로 진행합니다.", exc, hint)
            return

        for label, emb in zip(all_labels, embeddings):
            self._example_embeddings.setdefault(label, []).append(emb)

        self._save_cache()
        self.available = True
        logger.info("EmbedClassifier: ready  (%d examples)", len(all_texts))

    # ── Classify ─────────────────────────────────────────────

    def classify(self, text: str) -> Tuple[str, float]:
        """Return (intent_label, max_cosine_similarity).

        Raises RuntimeError on embedding API failure.
        """
        query_emb = _embed([text], self._session, self._cfg)[0]

        best_label = "chat"
        best_score = 0.0
        for label, example_embs in self._example_embeddings.items():
            score = max(_cosine(query_emb, ex_emb) for ex_emb in example_embs)
            if score > best_score:
                best_score = score
                best_label = label

        return best_label, best_score

    # ── Classify + extract ───────────────────────────────────

    def classify_and_extract(
        self,
        text: str,
        cfg: FridayConfig,
    ) -> Optional[Dict[str, Any]]:
        """Classify intent and extract structured parameters.

        Returns a complete intent dict on success, or None when:
        - classifier is not available
        - confidence < CONFIDENCE_THRESHOLD
        - intent is "chat" (handled upstream by Tier-0)
        - required params cannot be extracted (→ agent handles it)
        """
        if not self.available:
            return None

        try:
            intent_label, confidence = self.classify(text)
        except Exception as exc:
            logger.warning("EmbedClassifier: classify failed: %s", exc)
            return None

        logger.debug(
            "EmbedClassifier: %s (%.3f) — %.60s", intent_label, confidence, text
        )

        if confidence < CONFIDENCE_THRESHOLD:
            logger.debug("EmbedClassifier: low confidence → agent")
            return None

        if intent_label == "chat":
            # Tier-0 already handles pure-chat; returning None here lets
            # the dispatcher re-classify as chat or fall to agent.
            return None

        if _should_fallthrough_to_agent(intent_label, text):
            logger.debug(
                "EmbedClassifier: %s intent but request is too complex → agent", intent_label
            )
            return None

        params = _extract_params(intent_label, text, cfg)
        if params is None:
            logger.debug("EmbedClassifier: param extraction failed for %s → agent", intent_label)
            return None

        return {
            "intent": intent_label,
            "params": params,
            "description": _make_description(intent_label, params),
            "confidence": round(confidence, 4),  # passed to permission policy
        }
