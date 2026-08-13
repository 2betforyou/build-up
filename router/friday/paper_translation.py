"""Faithful Korean translation of research PDFs with separated explanations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from friday.config import FridayConfig
from friday.paths import ensure_within, reject_symlink


TRANSLATION_RULES = """번역 규칙 (절대 준수):
1. Machine learning 및 LLM domain terminology는 영어로 유지한다.
2. 원문의 의미를 추가하거나 생략하지 않는다.
3. citation, equation, variable name은 그대로 유지한다.
4. 직역을 우선하되 한국어 문장 구조가 심각하게 어색할 때만 최소한으로 재구성한다.
5. 번역 후 별도로 핵심 의미를 설명한다.
6. 번역과 설명을 명확히 구분한다.
"""

TRANSLATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "translation": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["translation", "explanation"],
    "additionalProperties": False,
}

_PROTECTED_RE = re.compile(
    r"\\begin\{(?:equation\*?|align\*?|gather\*?)\}.*?\\end\{(?:equation\*?|align\*?|gather\*?)\}"
    r"|\\\[.*?\\\]"
    r"|\\\(.*?\\\)"
    r"|\$\$.*?\$\$"
    r"|(?<!\$)\$(?!\$).*?(?<!\$)\$(?!\$)"
    r"|\[[^\]\n]{1,160}\]"
    r"|(?<!\w)[A-Za-z](?:_[A-Za-z0-9{}]+|\^[A-Za-z0-9{}]+)+(?!\w)",
    re.DOTALL,
)


@dataclass(frozen=True)
class TranslationTarget:
    source_path: Path
    output_path: Path
    job_id: Optional[str]
    library: bool


@dataclass(frozen=True)
class TranslationChunk:
    index: int
    start_page: int
    end_page: int
    text: str


@dataclass(frozen=True)
class PaperTranslationResult:
    source_path: Path
    output_path: Path
    page_count: int
    chunk_count: int
    model_calls: int
    model: str


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_translation_target(
    source: str,
    cfg: FridayConfig,
    *,
    current_job: Optional[str] = None,
    active_paper_id: Optional[str] = None,
) -> TranslationTarget:
    """Resolve a paper ID, active paper, or current-job PDF to a safe target."""
    from friday.paper_library import find_paper_entry
    from friday.state import job_dir

    hint = (source or "").strip().strip('"\'')
    entry = None
    if not hint and active_paper_id:
        entry = find_paper_entry(cfg, active_paper_id)
    elif hint and not hint.lower().endswith(".pdf") and "/" not in hint and "\\" not in hint:
        entry = find_paper_entry(cfg, hint)

    if entry:
        pdf = ensure_within(entry.path / "paper.pdf", cfg.paper_library_dir)
        if not pdf.exists():
            raise ValueError(f"선택한 논문에 paper.pdf가 없습니다: {entry.paper_id}")
        return TranslationTarget(pdf, entry.path / "translation-ko.md", None, True)

    if not hint:
        raise ValueError("번역할 PDF를 지정하거나 build-up에서 active paper를 선택해 주세요.")

    raw_path = Path(hint).expanduser()
    candidates: List[Tuple[Path, Optional[str]]] = []
    if raw_path.is_absolute():
        candidates.append((raw_path, current_job))
    else:
        if current_job:
            candidates.append((job_dir(current_job, cfg) / raw_path, current_job))
        candidates.append((cfg.base_dir / raw_path, current_job))

    for candidate, job_id in candidates:
        if not candidate.exists():
            continue
        resolved = candidate.resolve()
        reject_symlink(candidate)
        if _is_within(resolved, cfg.paper_library_dir):
            return TranslationTarget(resolved, resolved.parent / "translation-ko.md", None, True)
        if current_job:
            base = job_dir(current_job, cfg)
            try:
                safe = ensure_within(resolved, base)
            except ValueError:
                continue
            return TranslationTarget(
                safe,
                safe.with_name(f"{safe.stem}.ko.md"),
                job_id,
                False,
            )

    raise ValueError(
        "PDF를 찾지 못했거나 허용 범위 밖입니다. 외부 PDF는 먼저 /import로 가져와 주세요."
    )


def _split_text(text: str, limit: int) -> List[str]:
    parts: List[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _make_chunks(pages: List[Any], chunk_chars: int) -> List[TranslationChunk]:
    grouped: List[Tuple[int, int, str]] = []
    current: List[str] = []
    start_page = 0
    end_page = 0
    size = 0

    def flush() -> None:
        nonlocal current, start_page, end_page, size
        if current:
            grouped.append((start_page, end_page, "\n\n".join(current)))
        current = []
        start_page = 0
        end_page = 0
        size = 0

    for page in pages:
        page_no = int(page.page_number)
        block = f"[Page {page_no}]\n{page.content.strip()}"
        blocks = _split_text(block, chunk_chars) if len(block) > chunk_chars else [block]
        for sub_block in blocks:
            if current and size + len(sub_block) + 2 > chunk_chars:
                flush()
            if not current:
                start_page = page_no
            current.append(sub_block)
            end_page = page_no
            size += len(sub_block) + 2
    flush()
    return [
        TranslationChunk(i, start, end, text)
        for i, (start, end, text) in enumerate(grouped, 1)
    ]


def _protect_literals(text: str) -> Tuple[str, Dict[str, str]]:
    protected: Dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        token = f"[[[NIGHT_KEEP_{len(protected):04d}]]]"
        protected[token] = match.group(0)
        return token

    return _PROTECTED_RE.sub(replace, text), protected


def _restore_literals(text: str, protected: Dict[str, str]) -> str:
    restored = text
    for token, original in protected.items():
        restored = restored.replace(token, original)
    return restored


def _translation_payload(text: str) -> Dict[str, str]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0].strip()
    start = raw.find("{")
    if start < 0:
        raise ValueError("번역 모델 응답에서 JSON을 찾지 못했습니다.")
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"번역 JSON 형식이 올바르지 않습니다: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("번역 응답은 JSON 객체여야 합니다.")
    translation = value.get("translation")
    explanation = value.get("explanation")
    if not isinstance(translation, str) or not translation.strip():
        raise ValueError("번역 응답의 translation 필드가 비어 있습니다.")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ValueError("번역 응답의 explanation 필드가 비어 있습니다.")
    return {"translation": translation, "explanation": explanation}


def _authorise_output(target: TranslationTarget, cfg: FridayConfig) -> None:
    if not target.source_path.exists() or not target.source_path.is_file():
        raise ValueError(f"PDF 파일을 찾지 못했습니다: {target.source_path}")
    reject_symlink(target.source_path)
    if target.library:
        from friday.sandbox import require_library_write

        ensure_within(target.source_path, cfg.paper_library_dir)
        require_library_write(target.output_path, cfg, context="paper PDF translation")
        return
    if not target.job_id:
        raise ValueError("번역 결과를 저장할 job이 없습니다.")
    from friday.sandbox import require_write
    from friday.state import job_dir

    ensure_within(target.source_path, job_dir(target.job_id, cfg))
    require_write(target.output_path, target.job_id, cfg, context="paper PDF translation")


def translate_paper_pdf(
    target: TranslationTarget,
    cfg: FridayConfig,
    session: Any,
    logger: Any,
    *,
    chat_fn: Optional[Callable[..., str]] = None,
    chunk_chars: int = 28_000,
    status: Optional[Callable[[str], None]] = None,
) -> PaperTranslationResult:
    """Translate a PDF, using one inference when it fits and isolated chunks otherwise."""
    from friday.ollama import chat
    from friday.paper import extract_pdf_pages

    if target.source_path.suffix.lower() != ".pdf":
        raise ValueError("PDF 파일만 번역할 수 있습니다.")
    if chunk_chars < 4_000:
        raise ValueError("chunk_chars는 4,000 이상이어야 합니다.")
    _authorise_output(target, cfg)
    chat_fn = chat_fn or chat
    status = status or (lambda _: None)

    status("PDF 텍스트를 페이지별로 추출하는 중...")
    pages = extract_pdf_pages(target.source_path, cfg)
    chunks = _make_chunks(pages, chunk_chars)
    translated: List[Tuple[TranslationChunk, str, str]] = []

    for chunk in chunks:
        status(f"번역 중... {chunk.index}/{len(chunks)} (p.{chunk.start_page}-{chunk.end_page})")
        protected_text, literals = _protect_literals(chunk.text)
        prompt = (
            f"{TRANSLATION_RULES}\n"
            "보호 토큰 [[[NIGHT_KEEP_XXXX]]]은 철자·순서·개수를 바꾸지 말고 translation에 모두 유지하라.\n"
            "원문 전체를 빠짐없이 번역하라. 설명은 번역문에 섞지 말고, explanation에도 외부 지식을 추가하지 마라.\n"
            '반드시 JSON만 반환하라: {"translation":"...", "explanation":"..."}\n\n'
            f"[원문 페이지 {chunk.start_page}-{chunk.end_page}]\n{protected_text}"
        )
        raw = chat_fn(
            session,
            cfg,
            cfg.night_model,
            [
                {
                    "role": "system",
                    "content": "당신은 ML/LLM 학술 논문 전문 영한 번역가다. 제공된 원문 밖의 내용을 만들지 않는다.",
                },
                {"role": "user", "content": prompt},
            ],
            keep_alive="15m",
            logger=logger,
            think=False,
            json_mode=True,
            json_schema=TRANSLATION_SCHEMA,
            sanitize_thinking=False,
        )
        payload = _translation_payload(raw)
        found_tokens = re.findall(r"\[\[\[NIGHT_KEEP_\d{4}\]\]\]", payload["translation"])
        expected_tokens = set(literals)
        missing = sorted(expected_tokens - set(found_tokens))
        extras = sorted(set(found_tokens) - expected_tokens)
        duplicated = sorted(token for token in expected_tokens if found_tokens.count(token) != 1)
        if missing or extras or duplicated:
            raise ValueError(
                f"p.{chunk.start_page}-{chunk.end_page} 번역에서 citation/equation 보호 토큰 "
                f"검증에 실패했습니다 (누락 {len(missing)}, 추가 {len(extras)}, 중복 {len(duplicated)}). "
                "결과 파일은 저장하지 않았습니다."
            )
        translated.append((
            chunk,
            _restore_literals(payload["translation"], literals).strip(),
            _restore_literals(payload["explanation"], literals).strip(),
        ))

    lines = [
        f"# {target.source_path.name} 한국어 번역",
        "",
        f"> 번역 모델: `{cfg.night_model}` · 원문 {len(pages)}페이지 · {len(chunks)}개 묶음",
        "> Machine learning/LLM terminology, citation, equation, variable name은 원문 표기를 유지합니다.",
        "",
    ]
    for chunk, translation, explanation in translated:
        lines.extend([
            f"## Pages {chunk.start_page}-{chunk.end_page}",
            "",
            "### 번역",
            "",
            translation,
            "",
            "### 핵심 의미",
            "",
            explanation,
            "",
        ])

    target.output_path.parent.mkdir(parents=True, exist_ok=True)
    target.output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    if target.job_id:
        from friday.jobs import append_action_log

        append_action_log(
            target.job_id,
            cfg,
            "paper_translate",
            f"{target.source_path.name} → {target.output_path.name}",
        )
    return PaperTranslationResult(
        source_path=target.source_path,
        output_path=target.output_path,
        page_count=len(pages),
        chunk_count=len(chunks),
        model_calls=len(chunks),
        model=cfg.night_model,
    )
