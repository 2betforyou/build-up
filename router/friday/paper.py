"""ML/NLP/CV/FL paper reading, parsing, and analysis.

Accuracy-first design:
- All answers cite actual paper text with section labels.
- Numbers, model names, dataset names are quoted verbatim.
- Anything not found in the paper is explicitly flagged as absent.
- Long papers (>80k chars) are processed section-by-section.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────

@dataclass
class PaperSection:
    key: str        # canonical key: "abstract", "method", "results", …
    title: str      # raw section title from the paper
    content: str    # full section text
    start_page: Optional[int] = None
    end_page: Optional[int] = None

    def __len__(self) -> int:
        return len(self.content)


@dataclass
class PaperPage:
    page_number: int
    content: str


@dataclass
class Paper:
    title: str
    authors: str
    venue: str
    year: str
    source_file: str
    sections: List[PaperSection] = field(default_factory=list)
    full_text: str = ""
    pages: List[PaperPage] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ── helpers ──────────────────────────────

    def get_section(self, key: str) -> Optional[PaperSection]:
        for s in self.sections:
            if s.key == key:
                return s
        return None

    def abstract(self) -> str:
        s = self.get_section("abstract")
        return s.content if s else ""

    def total_chars(self) -> int:
        return len(self.full_text)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Paper":
        data = dict(d)
        sections = [PaperSection(**s) for s in data.pop("sections", [])]
        pages = [PaperPage(**p) for p in data.pop("pages", [])]
        allowed = {
            "title", "authors", "venue", "year", "source_file",
            "full_text", "metadata",
        }
        p = cls(**{k: v for k, v in data.items() if k in allowed})
        p.sections = sections
        p.pages = pages
        return p


# ─────────────────────────────────────────────
# Section parser
# ─────────────────────────────────────────────

# Canonical section keys and their trigger words (order matters)
_SECTION_MAP: List[Tuple[str, List[str]]] = [
    ("abstract",      ["abstract"]),
    ("introduction",  ["introduction", "intro"]),
    ("related_work",  ["related work", "related works", "prior work",
                       "background", "literature review"]),
    ("method",        ["method", "methodology", "approach", "proposed",
                       "framework", "architecture", "model", "formulation",
                       "problem formulation", "preliminary", "overview"]),
    ("experiments",   ["experiment", "experimental setup", "setup",
                       "implementation", "training detail"]),
    ("results",       ["result", "evaluation", "performance", "analysis",
                       "ablation", "comparison", "benchmark", "main result"]),
    ("discussion",    ["discussion", "analysis", "limitation",
                       "future work", "broader impact", "societal"]),
    ("conclusion",    ["conclusion", "conclud", "summary and conclusion"]),
    ("appendix",      ["appendix", "supplementary", "supplemental"]),
]

# Header patterns: numbered, all-caps, markdown
_HEADER_RE = re.compile(
    r"(?m)^(?:"
    r"(?P<numbered>\d+(?:\.\d+)?\s+[A-Z][^\n]{2,60})"        # "3. Method", "3.1 Encoder"
    r"|(?P<allcaps>[A-Z][A-Z\s\-]{4,50})"                      # "EXPERIMENTS"
    r"|(?P<markdown>#{1,3}\s+.{2,60})"                          # "## 3. Method"
    r")\s*$",
    re.UNICODE,
)

_PAGE_MARKER_RE = re.compile(r"\[Page\s+(\d+)\]")


def _canonicalize(title: str) -> str:
    return title.lower().strip("#0123456789. \t")


def _detect_key(raw_title: str) -> str:
    clean = _canonicalize(raw_title)
    for key, triggers in _SECTION_MAP:
        if any(t in clean for t in triggers):
            return key
    return "other"


def _page_at(text: str, pos: int) -> Optional[int]:
    page = None
    for match in _PAGE_MARKER_RE.finditer(text, 0, max(0, pos)):
        try:
            page = int(match.group(1))
        except ValueError:
            pass
    return page


def parse_sections(text: str) -> List[PaperSection]:
    """Split paper text into labelled sections.

    Strategy:
      1. Try regex header detection (numbered / all-caps / markdown).
      2. If <3 headers found, fall back to paragraph-based splitting using
         keyword matching on the first sentence of each paragraph.
    """
    splits: List[Tuple[int, str]] = []   # (start_pos, raw_header_title)

    for m in _HEADER_RE.finditer(text):
        raw = (m.group("numbered") or m.group("allcaps") or m.group("markdown") or "").strip()
        splits.append((m.start(), raw))

    if len(splits) < 3:
        # Fallback: paragraph-level keyword detection
        splits = []
        for para_m in re.finditer(r"(?m)^(.{10,80})\n", text):
            first_line = para_m.group(1).strip()
            key = _detect_key(first_line)
            if key != "other":
                splits.append((para_m.start(), first_line))

    # Build sections from split positions
    sections: List[PaperSection] = []
    for i, (start, raw_title) in enumerate(splits):
        end = splits[i + 1][0] if i + 1 < len(splits) else len(text)
        content = text[start:end].strip()
        # Remove the header line itself from content
        content_lines = content.split("\n")
        body = "\n".join(content_lines[1:]).strip() if len(content_lines) > 1 else content
        key = _detect_key(raw_title)
        sections.append(PaperSection(
            key=key,
            title=raw_title.strip(),
            content=body,
            start_page=_page_at(text, start),
            end_page=_page_at(text, end),
        ))

    return sections


def extract_pdf_pages(path: Path, cfg) -> List[PaperPage]:
    """Extract PDF text page-by-page.

    This is the backbone for build-up: later reviews can point back to
    page numbers instead of only vague section names.
    """
    try:
        from pdfminer.high_level import extract_pages  # type: ignore
        from pdfminer.layout import LTTextContainer  # type: ignore
    except ImportError:
        raise ImportError(
            "PDF 읽기에는 pdfminer.six가 필요합니다.  pip install pdfminer.six"
        )

    size = path.stat().st_size
    if size > cfg.max_pdf_bytes:
        raise ValueError(f"PDF가 너무 큽니다 ({size:,} bytes).")

    pages: List[PaperPage] = []
    for idx, layout in enumerate(extract_pages(str(path)), start=1):
        parts: List[str] = []
        for element in layout:
            if isinstance(element, LTTextContainer):
                text = element.get_text().strip()
                if text:
                    parts.append(text)
        pages.append(PaperPage(page_number=idx, content="\n".join(parts).strip()))

    if not pages or not any(p.content.strip() for p in pages):
        raise ValueError("PDF에서 텍스트를 추출할 수 없습니다 (스캔 이미지일 수 있습니다).")
    return pages


def page_marked_text(pages: List[PaperPage]) -> str:
    return "\n\n".join(
        f"[Page {page.page_number}]\n{page.content.strip()}"
        for page in pages
        if page.content.strip()
    )


# ─────────────────────────────────────────────
# Context selector (for long papers)
# ─────────────────────────────────────────────

LONG_PAPER_THRESHOLD = 80_000   # chars ≈ 20k tokens

# Which sections are relevant for each question type
_QUESTION_SECTION_HINTS: List[Tuple[re.Pattern, List[str]]] = [
    (re.compile(r"기여|contribution|novel|제안|introduce|핵심|main result", re.I),
     ["abstract", "introduction", "conclusion"]),
    (re.compile(r"방법|method|어떻게|how|알고리즘|algorithm|모델|model|구조|architecture", re.I),
     ["method", "introduction"]),
    (re.compile(r"실험|experiment|결과|result|성능|performance|benchmark|데이터|dataset|비교", re.I),
     ["experiments", "results", "method"]),
    (re.compile(r"한계|limitation|약점|weakness|단점|future|향후", re.I),
     ["discussion", "conclusion"]),
    (re.compile(r"관련|related|prior|기존|이전", re.I),
     ["related_work", "introduction"]),
    (re.compile(r"배경|background|preliminary|전제|개념", re.I),
     ["related_work", "method", "introduction"]),
]

_ALWAYS_INCLUDE = {"abstract", "introduction"}


def select_context(paper: Paper, query: str, max_chars: int = 14_000) -> str:
    """Return the most relevant paper sections for a query, within max_chars.

    For short papers, returns the full text.
    For long papers, prioritises sections based on query keywords.
    """
    if paper.total_chars() <= LONG_PAPER_THRESHOLD:
        return paper.full_text[:max_chars * 2]   # use full text for short papers

    # Determine priority sections for this query
    priority_keys: List[str] = list(_ALWAYS_INCLUDE)
    for pattern, keys in _QUESTION_SECTION_HINTS:
        if pattern.search(query):
            for k in keys:
                if k not in priority_keys:
                    priority_keys.append(k)

    # Build context: priority sections first, then fill remaining budget
    used: List[str] = []
    budget = max_chars

    for key in priority_keys:
        sec = paper.get_section(key)
        if sec and budget > 500:
            chunk = sec.content[:min(budget, 5000)]
            used.append(f"[{sec.title}]\n{chunk}")
            budget -= len(chunk)

    # Fill remaining budget with other sections
    for sec in paper.sections:
        if sec.key in priority_keys or budget < 500:
            continue
        chunk = sec.content[:min(budget, 3000)]
        used.append(f"[{sec.title}]\n{chunk}")
        budget -= len(chunk)

    return "\n\n---\n\n".join(used)


# ─────────────────────────────────────────────
# Prompts (accuracy-first)
# ─────────────────────────────────────────────

PAPER_SUMMARY_SYSTEM = """\
당신은 ML/NLP/LLM/CV/Federated Learning 논문 분석 전문가다.
주어진 논문 본문만을 근거로 구조화된 요약을 작성한다.

정확성 규칙 (절대 준수):
1. 모든 주장은 논문 본문에서 확인된 내용만 포함하라.
2. 수치, 수식, 모델명, 데이터셋명, 벤치마크명은 논문에 나온 그대로 인용하라.
3. 논문에 없는 내용을 추가하지 마라.
4. 불확실하거나 명시되지 않은 항목은 "논문에서 명시하지 않음"으로 표시하라.
5. 저자의 주장(claim)과 실험으로 입증된 사실을 구분하라.
6. 한국어를 기본 본문으로 쓰고, 마지막에 짧은 English Brief를 추가하라.
7. technical terms, model names, datasets, metrics, equations는 원문 표기를 보존하라.
"""

PAPER_SUMMARY_PROMPT = """\
아래 논문을 분석하여 다음 형식으로 구조화된 요약을 작성하라.
본문에서 확인된 내용만 작성하고, 없는 항목은 "논문에서 명시하지 않음"이라고 쓴다.

## 기본 정보
- 제목: (논문 제목 그대로)
- 저자: (저자 목록)
- 발표: (학회/저널/arXiv, 연도)

## 한 줄 요약
(이 논문이 무엇을 제안하는지 한 문장으로)

## 핵심 기여 (논문에서 인용)
(저자가 논문에서 명시한 contribution 그대로. 일반화하지 말 것)

## 문제 정의
(어떤 문제를 다루는지, 기존 방법의 한계는 무엇인지)

## 방법론
### 핵심 아이디어
### 모델 구조 / 알고리즘
(수식, 모듈 이름 그대로 포함)
### 학습 방법 / Objective

## 실험 결과
### 데이터셋 및 벤치마크
(사용된 데이터셋, 비교 대상 baseline 목록)
### 주요 성능 수치
(수치를 그대로. 예: "BLEU 38.4 on WMT14 En-De, +1.2 over baseline")
### Ablation Study 핵심 결과
(있는 경우만)

## 한계 및 향후 연구
(저자가 명시한 limitation / future work만)

## 이 논문을 이해하기 위한 배경 지식
(논문에서 전제하는 선행 개념 목록. 일반 지식이므로 [배경 지식] 표시)

## English Brief
(3-6 bullets. Main verdict, contribution, evidence quality, and caveats only.)

---
논문 본문:
{paper_text}
"""

PAPER_QA_SYSTEM = """\
당신은 ML/NLP/LLM/CV/Federated Learning 논문 전문 분석가다.
주어진 논문 본문만을 근거로 질문에 답한다.

정확성 규칙 (절대 준수):
1. 모든 사실 주장에 출처 섹션을 표시하라. 형식: [섹션명]
2. 수치, 수식, 모델명, 데이터셋명, 메트릭은 논문에 나온 그대로 인용하라.
3. 논문 본문에서 찾을 수 없는 정보는 "이 논문에서 명시하지 않습니다"라고 답하라.
4. 불확실하면 "~로 보입니다" / "~로 해석됩니다"처럼 불확실성을 명시하라.
5. 일반 배경 지식이 필요할 때는 반드시 [배경 지식]으로 구분하라.
6. 저자의 주장과 실험으로 검증된 사실을 구분하라.
7. 논문 외부 지식으로 답을 채우거나 가정하지 마라.
8. 한국어 답변 뒤에 짧은 English Brief를 붙인다.
"""

PAPER_QA_PROMPT = """\
논문 제목: {title}
저자: {authors}

[관련 논문 본문]
{context}

---
질문: {question}

위 논문 본문을 근거로 답하라. 본문에 없는 내용은 지어내지 마라.
한국어로 먼저 답하고, 마지막에 2-4문장의 English Brief를 덧붙여라.
"""

EXPLAIN_SYSTEM = """\
당신은 ML/NLP/LLM/CV/Federated Learning 개념 설명 전문가다.

설명 원칙:
1. 논문에서 해당 개념을 설명하는 부분을 먼저 직접 인용하라. [인용]
2. 그 다음 쉬운 언어로 풀어 설명하라. [설명]
3. 논문이 이 개념을 어떻게 활용/변형했는지 설명하라. [논문에서의 적용]
4. 이해를 위한 직관적 비유를 제공하라 (있을 때). [직관]
5. 논문에 없는 내용을 추가할 때는 [배경 지식]으로 명확히 표시하라.
6. 한국어 설명 뒤에 짧은 English Brief를 붙여라.
"""

EXPLAIN_PROMPT = """\
논문 제목: {title}

[논문 본문 (관련 부분)]
{context}

---
설명 요청: "{concept}"

이 개념을 위 논문 본문을 중심으로 설명하라.
논문의 정의·수식·사용 방식을 먼저 인용하고, 이해하기 쉽게 풀어 설명하라.
"""

COMPARE_SYSTEM = """\
당신은 ML 논문 비교 분석 전문가다.

비교 원칙:
1. 각 논문에서 실제로 주장하는 내용만 비교하라.
2. 수치 비교는 반드시 동일 벤치마크/설정에서의 수치만 비교하라.
3. 직접 비교가 불가능한 경우 "직접 비교 불가 (다른 설정)"이라고 명시하라.
4. 논문 간 우열을 판단할 때 해당 주장의 근거가 있는지 확인하라.
5. 한국어 비교 뒤에 concise English Brief를 붙여라.
"""


def _night_model(cfg) -> str:
    return getattr(cfg, "night_model", getattr(cfg, "main_model", ""))


def _reviewer_model(cfg) -> str:
    return getattr(cfg, "reviewer_model", _night_model(cfg))

PAPER_PASS_SYSTEM = """\
당신은 외부 모델이나 웹 지식 없이 논문 본문만 읽는 로컬 연구 보조자다.
답변은 반드시 주어진 본문 근거 안에서만 작성한다.
모든 핵심 항목에는 가능한 경우 [섹션명] 또는 [Page N] 근거를 붙인다.
저자 주장, 실험 근거, 해석, 배경지식을 구분한다.
technical terms, model names, datasets, metrics는 원문 표기를 보존한다.
"""

SENIOR_REVIEW_SYSTEM = """\
당신은 엄격하지만 실용적인 시니어 연구자다.
아래의 섹션별 독해 노트와 원문 후보 근거만 사용해서 최종 리뷰를 작성한다.

규칙:
1. 논문에 없는 내용을 단정하지 않는다.
2. claim / evidence / interpretation / background를 구분한다.
3. 실험 설계, baseline, metric, ablation, limitation을 비판적으로 확인한다.
4. 근거가 약하면 "근거 약함"이라고 표시한다.
5. 한국어로 본문을 작성하되 모델명, 데이터셋명, 수치는 원문 표기를 유지한다.
6. 마지막에 concise English Brief를 추가한다.
"""

# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def load_paper(
    text: str,
    source_file: str,
    cfg,
    session,
    logger,
    pages: Optional[List[PaperPage]] = None,
    source_metadata: Optional[Dict[str, Any]] = None,
) -> Paper:
    """Parse raw text into a Paper object with sections extracted."""
    from friday.ollama import chat

    source_metadata = dict(source_metadata or {})
    sections = parse_sections(text)

    # Extract metadata (title, authors, venue) from first ~2000 chars
    meta_prompt = (
        "아래 논문 서두에서 제목, 저자, 발표 학회/저널/arXiv, 연도를 추출하라.\n"
        "반드시 JSON만 반환하라:\n"
        '{"title": "...", "authors": "...", "venue": "...", "year": "..."}\n\n'
        f"{text[:2000]}"
    )
    try:
        raw = chat(
            session, cfg, cfg.fast_model,
            [{"role": "user", "content": meta_prompt}],
            keep_alive="2m", logger=logger,
        )
        # Extract JSON
        m = re.search(r"\{[^{}]+\}", raw, re.DOTALL)
        meta = json.loads(m.group()) if m else {}
    except Exception:
        meta = {}

    if source_metadata.get("title") and not meta.get("title"):
        meta["title"] = source_metadata.get("title")
    if source_metadata.get("authors") and not meta.get("authors"):
        authors = source_metadata.get("authors")
        meta["authors"] = ", ".join(authors) if isinstance(authors, list) else str(authors)
    if source_metadata.get("journal_ref") and not meta.get("venue"):
        meta["venue"] = source_metadata.get("journal_ref")
    if source_metadata.get("arxiv_id") and not meta.get("venue"):
        meta["venue"] = "arXiv"
    if source_metadata.get("published") and not meta.get("year"):
        meta["year"] = str(source_metadata.get("published", ""))[:4]

    paper = Paper(
        title=meta.get("title", source_file),
        authors=meta.get("authors", ""),
        venue=meta.get("venue", ""),
        year=meta.get("year", ""),
        source_file=source_file,
        sections=sections,
        full_text=text,
        pages=pages or [],
        metadata={**source_metadata, **meta},
    )
    return paper


def load_paper_from_path(
    path: Path,
    cfg,
    session,
    logger,
    source_metadata: Optional[Dict[str, Any]] = None,
) -> Paper:
    """Load a local PDF/text paper with the richest extraction available."""
    from friday.paths import read_text_file, is_pdf_file, is_text_file

    if is_pdf_file(path):
        pages = extract_pdf_pages(path, cfg)
        text = page_marked_text(pages)
        return load_paper(
            text, path.name, cfg, session, logger,
            pages=pages, source_metadata=source_metadata,
        )
    if is_text_file(path, cfg):
        text = read_text_file(path, cfg)
        return load_paper(text, path.name, cfg, session, logger, source_metadata=source_metadata)
    raise ValueError(f"지원하지 않는 파일 형식: {path.suffix}")


def summarize_paper(paper: Paper, cfg, session, logger) -> str:
    """Generate a structured summary of the paper."""
    from friday.ollama import chat

    # For long papers, use section-by-section context
    if paper.total_chars() > LONG_PAPER_THRESHOLD:
        # Build a condensed view: abstract + intro + method + results + conclusion
        priority = ["abstract", "introduction", "method", "experiments",
                    "results", "discussion", "conclusion"]
        parts = []
        budget = 60_000
        for key in priority:
            sec = paper.get_section(key)
            if sec and budget > 0:
                chunk = sec.content[:min(budget, 10_000)]
                parts.append(f"[{sec.title}]\n{chunk}")
                budget -= len(chunk)
        paper_text = "\n\n---\n\n".join(parts)
    else:
        paper_text = paper.full_text

    prompt = PAPER_SUMMARY_PROMPT.format(paper_text=paper_text)

    summary = chat(
        session, cfg, _night_model(cfg),
        [{"role": "system", "content": PAPER_SUMMARY_SYSTEM},
         {"role": "user", "content": prompt}],
        keep_alive="10m", logger=logger, display_thinking=True,
    )
    return summary


def answer_question(
    paper: Paper,
    question: str,
    cfg,
    session,
    logger,
) -> str:
    """Answer a question about the paper with mandatory citations."""
    from friday.ollama import chat

    context = select_context(paper, question)

    prompt = PAPER_QA_PROMPT.format(
        title=paper.title,
        authors=paper.authors,
        context=context,
        question=question,
    )

    answer = chat(
        session, cfg, _night_model(cfg),
        [{"role": "system", "content": PAPER_QA_SYSTEM},
         {"role": "user", "content": prompt}],
        keep_alive="10m", logger=logger, display_thinking=True,
    )
    return answer


def explain_concept(
    paper: Paper,
    concept: str,
    cfg,
    session,
    logger,
) -> str:
    """Explain a concept as used in the paper, with citations."""
    from friday.ollama import chat

    context = select_context(paper, concept)

    prompt = EXPLAIN_PROMPT.format(
        title=paper.title,
        context=context,
        concept=concept,
    )

    explanation = chat(
        session, cfg, _night_model(cfg),
        [{"role": "system", "content": EXPLAIN_SYSTEM},
         {"role": "user", "content": prompt}],
        keep_alive="10m", logger=logger, display_thinking=True,
    )
    return explanation


def compare_papers(
    paper_a: Paper,
    paper_b: Paper,
    aspect: str,
    cfg,
    session,
    logger,
) -> str:
    """Compare two papers on a given aspect."""
    from friday.ollama import chat

    ctx_a = select_context(paper_a, aspect, max_chars=8000)
    ctx_b = select_context(paper_b, aspect, max_chars=8000)

    prompt = (
        f"두 논문을 '{aspect}' 관점에서 비교하라.\n\n"
        f"[논문 A: {paper_a.title}]\n{ctx_a}\n\n"
        f"[논문 B: {paper_b.title}]\n{ctx_b}\n\n"
        "각 논문에서 실제로 주장하는 내용만 비교하라. "
        "동일 벤치마크 수치가 없으면 직접 수치 비교를 피하라."
    )
    return chat(
        session, cfg, _night_model(cfg),
        [{"role": "system", "content": COMPARE_SYSTEM},
         {"role": "user", "content": prompt}],
        keep_alive="10m", logger=logger, display_thinking=True,
    )


# ─────────────────────────────────────────────
# Local-only senior review pipeline
# ─────────────────────────────────────────────

def _compact(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _sections_by_keys(paper: Paper, keys: List[str], max_chars: int = 18_000) -> str:
    chunks: List[str] = []
    remaining = max_chars
    for key in keys:
        for sec in paper.sections:
            if sec.key != key or remaining <= 500:
                continue
            page = ""
            if sec.start_page:
                page = f" [Page {sec.start_page}" + (f"-{sec.end_page}" if sec.end_page and sec.end_page != sec.start_page else "") + "]"
            chunk = f"[{sec.title or sec.key}]{page}\n{sec.content.strip()}"
            chunk = _compact(chunk, min(remaining, 6000))
            chunks.append(chunk)
            remaining -= len(chunk)
    if chunks:
        return "\n\n---\n\n".join(chunks)
    return _compact(paper.full_text, max_chars)


def extract_number_candidates(paper: Paper, max_items: int = 80) -> List[Dict[str, Any]]:
    """Heuristically collect lines containing experimental-looking numbers."""
    patterns = (
        re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|x|ms|s|sec|GB|MB|B|M|K)\b", re.I),
        re.compile(r"\b(?:accuracy|acc|f1|bleu|rouge|mmlu|pass@|auc|wer|perplexity|ppl)\b", re.I),
        re.compile(r"\b\d+(?:\.\d+)?\s*(?:±|\+/-|\+)\s*\d+(?:\.\d+)?\b"),
    )
    items: List[Dict[str, Any]] = []
    for page in paper.pages or [PaperPage(0, paper.full_text)]:
        for raw in page.content.splitlines():
            line = " ".join(raw.split())
            if len(line) < 20 or len(line) > 260:
                continue
            if any(p.search(line) for p in patterns):
                items.append({"page": page.page_number or None, "text": line})
                if len(items) >= max_items:
                    return items
    return items


def extract_caption_candidates(paper: Paper, max_items: int = 60) -> List[Dict[str, Any]]:
    caption_re = re.compile(r"^\s*(figure|fig\.|table|algorithm)\s+\d+", re.I)
    items: List[Dict[str, Any]] = []
    for page in paper.pages or [PaperPage(0, paper.full_text)]:
        lines = page.content.splitlines()
        for idx, raw in enumerate(lines):
            line = " ".join(raw.split())
            if not caption_re.search(line):
                continue
            extra = " ".join(" ".join(x.split()) for x in lines[idx + 1:idx + 3])
            text = _compact((line + " " + extra).strip(), 500)
            items.append({"page": page.page_number or None, "text": text})
            if len(items) >= max_items:
                return items
    return items


def _run_reading_pass(
    paper: Paper,
    name: str,
    focus: str,
    context: str,
    cfg,
    session,
    logger,
) -> Dict[str, Any]:
    from friday.ollama import chat

    prompt = f"""\
논문: {paper.title}
독해 pass: {name}
초점: {focus}

아래 본문만 근거로 독해 노트를 작성하라.
형식:
- confirmed_findings: 본문 근거가 있는 사실
- author_claims: 저자가 주장하는 기여/효과
- evidence: 실험/수치/수식/표/그림 근거
- concerns: 근거가 약하거나 확인할 점
- missing_info: 본문에서 찾지 못한 중요 정보

[본문]
{context}
"""
    notes = chat(
        session, cfg, _night_model(cfg),
        [{"role": "system", "content": PAPER_PASS_SYSTEM},
         {"role": "user", "content": prompt}],
        keep_alive="10m", logger=logger, display_thinking=True,
    )
    return {
        "name": name,
        "focus": focus,
        "notes": notes,
    }


def build_evidence_ledger(paper: Paper, cfg, session, logger) -> Dict[str, Any]:
    """Run local multi-pass reading and store the raw evidence ledger."""
    pass_specs = [
        (
            "skim",
            "초록/서론/결론에서 문제정의, 핵심 주장, 저자 contribution 추출",
            ["abstract", "introduction", "conclusion"],
        ),
        (
            "method",
            "방법론, 모델 구조, 알고리즘, objective, 핵심 assumption 복원",
            ["method", "introduction"],
        ),
        (
            "experiments",
            "데이터셋, baseline, metric, main result, ablation, 수치 근거 추출",
            ["experiments", "results"],
        ),
        (
            "critical",
            "한계, 약한 근거, 빠진 baseline/ablation, generalization 문제 점검",
            ["discussion", "conclusion", "results", "experiments"],
        ),
    ]
    passes: List[Dict[str, Any]] = []
    for name, focus, keys in pass_specs:
        context = _sections_by_keys(paper, keys, max_chars=18_000)
        passes.append(_run_reading_pass(paper, name, focus, context, cfg, session, logger))

    return {
        "version": 1,
        "title": paper.title,
        "source_file": paper.source_file,
        "pages": len(paper.pages),
        "sections": [
            {
                "key": sec.key,
                "title": sec.title,
                "start_page": sec.start_page,
                "end_page": sec.end_page,
                "chars": len(sec.content),
            }
            for sec in paper.sections
        ],
        "number_candidates": extract_number_candidates(paper),
        "caption_candidates": extract_caption_candidates(paper),
        "passes": passes,
        "checklist": {
            "claim_evidence_separation": True,
            "dataset_baseline_metric_check": True,
            "ablation_check": True,
            "limitation_check": True,
            "local_only": True,
        },
    }


def generate_senior_review(
    paper: Paper,
    evidence: Dict[str, Any],
    cfg,
    session,
    logger,
    reviewer_mode: bool = False,
) -> str:
    from friday.ollama import chat

    pass_notes = "\n\n".join(
        f"## {item.get('name')}\n{item.get('notes')}"
        for item in evidence.get("passes", [])
    )
    numbers = "\n".join(
        f"- p.{item.get('page')}: {item.get('text')}"
        for item in evidence.get("number_candidates", [])[:40]
    )
    captions = "\n".join(
        f"- p.{item.get('page')}: {item.get('text')}"
        for item in evidence.get("caption_candidates", [])[:30]
    )
    prompt = f"""\
논문: {paper.title}
저자: {paper.authors or '논문에서 명시하지 않음'}
발표: {paper.venue or '논문에서 명시하지 않음'} {paper.year or ''}

[독해 노트]
{_compact(pass_notes, 30_000)}

[수치 후보]
{numbers or '(없음)'}

[표/그림/알고리즘 후보]
{captions or '(없음)'}

위 정보만 사용해서 senior_review.md를 작성하라.
반드시 다음 섹션을 포함하라:

# Senior Paper Review
## 1. 한 문장 결론
## 2. 이 논문이 푸는 문제
## 3. 핵심 기여: 저자 주장 vs 근거
## 4. 방법론 재구성
## 5. 실험 설계 검토
## 6. 중요한 수치와 표/그림 해석
## 7. 한계: 명시된 한계와 추론된 약점
## 8. 내가 시니어 연구자라면 다음에 확인할 것
## 9. 구현/재현 체크리스트
## 10. 읽을 가치 판단
## 11. English Brief
"""
    return chat(
        session, cfg, _reviewer_model(cfg) if reviewer_mode else _night_model(cfg),
        [{"role": "system", "content": SENIOR_REVIEW_SYSTEM},
         {"role": "user", "content": prompt}],
        keep_alive="10m", logger=logger, display_thinking=True,
    )


def write_paper_metadata(
    paper: Paper,
    paper_dir: Path,
    source_metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    metadata = {
        "title": paper.title,
        "authors": paper.authors,
        "venue": paper.venue,
        "year": paper.year,
        "source_file": paper.source_file,
        "pages": len(paper.pages),
        "section_count": len(paper.sections),
        "fields": [],
        "tags": [],
        "status": "reviewed",
        "source_metadata": source_metadata or paper.metadata or {},
    }
    path = paper_dir / "metadata.json"
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update_paper_indexes(paper: Paper, paper_dir: Path, cfg, source_metadata: Optional[Dict[str, Any]] = None) -> List[Path]:
    """Update lightweight markdown index views for the paper library."""
    from friday.paths import slugify
    from friday.sandbox import require_library_index_write

    source_metadata = source_metadata or paper.metadata or {}
    title = paper.title or paper_dir.name
    rel = paper_dir.relative_to(cfg.base_dir)
    entry = f"- [{title}]({rel}/review.md) — `{rel}`"
    if paper.authors:
        entry += f" — {paper.authors}"

    views: List[tuple[str, str]] = []
    venue = paper.venue or source_metadata.get("journal_ref") or "unknown-venue"
    year = paper.year or str(source_metadata.get("published", ""))[:4] or "unknown-year"
    views.append(("by-venue", f"{slugify(str(venue))}-{slugify(str(year))}.md"))

    primary_category = source_metadata.get("primary_category") or "uncategorized"
    views.append(("by-field", f"{slugify(str(primary_category))}.md"))
    views.append(("by-status", "reviewed.md"))

    written: List[Path] = []
    for folder, filename in views:
        path = cfg.paper_index_dir / folder / filename
        require_library_index_write(path, cfg, context="paper index")
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else f"# {filename.removesuffix('.md')}\n\n"
        if f"`{rel}`" not in existing:
            existing = existing.rstrip() + "\n" + entry + "\n"
            path.write_text(existing, encoding="utf-8")
        written.append(path)
    return written


def run_deep_paper_review(
    paper: Paper,
    paper_dir: Path,
    cfg,
    session,
    logger,
    source_metadata: Optional[Dict[str, Any]] = None,
    reviewer_mode: bool = False,
) -> Dict[str, str]:
    """Persist a local-only build-up review bundle."""
    paper_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = write_paper_metadata(paper, paper_dir, source_metadata)
    update_paper_indexes(paper, paper_dir, cfg, source_metadata)
    paper_json = save_paper_json(paper, paper_dir, filename="paper.paper.json")
    summary = summarize_paper(paper, cfg, session, logger)
    summary_path = paper_dir / "summary.md"
    summary_path.write_text(summary, encoding="utf-8")
    evidence = build_evidence_ledger(paper, cfg, session, logger)
    evidence_path = paper_dir / "evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    review = generate_senior_review(paper, evidence, cfg, session, logger, reviewer_mode=reviewer_mode)
    review_path = paper_dir / "review.md"
    review_path.write_text(review, encoding="utf-8")
    notes_path = paper_dir / "notes.md"
    notes_path.write_text(
        "\n\n".join(
            f"# {item.get('name')}\n\n{item.get('notes')}"
            for item in evidence.get("passes", [])
        ),
        encoding="utf-8",
    )
    return {
        "metadata": str(metadata_path),
        "paper_json": str(paper_json),
        "summary": str(summary_path),
        "evidence": str(evidence_path),
        "review": str(review_path),
        "notes": str(notes_path),
    }

# ─────────────────────────────────────────────
# Persistence helpers
# ─────────────────────────────────────────────

def save_paper_json(paper: Paper, job_base: Path, filename: Optional[str] = None) -> Path:
    """Save Paper object as JSON."""
    stem = re.sub(r"[^\w\-]", "_", Path(paper.source_file).stem)[:50]
    path = job_base / (filename or f"{stem}.paper.json")
    path.write_text(json.dumps(paper.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_paper_json(path: Path) -> Paper:
    """Load Paper object from .paper.json file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return Paper.from_dict(data)


def find_paper_json(job_base: Path, hint: Optional[str] = None) -> Optional[Path]:
    """Find the most recently modified .paper.json in the job directory."""
    candidates = sorted(job_base.glob("*.paper.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    if hint:
        for p in candidates:
            if hint.lower() in p.name.lower():
                return p
    return candidates[0]
