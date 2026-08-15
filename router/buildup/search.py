"""Public search facade backed by Build-up's normalized provider broker."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

import requests

from buildup.config import BuildupConfig
from buildup.research.providers import SearchBroker, SearchProviderError
from buildup.research.reader import SafeWebReader, is_public_web_url


def _is_korean(text: str) -> bool:
    return bool(re.search(r"[\uac00-\ud7af]", text))


def _generate_english_query(korean_query: str) -> str:
    """Build a conservative English keyword variant without an LLM call."""
    mappings = {
        "대규모 언어 모델": "large language model LLM",
        "언어 모델": "language model",
        "인공지능": "artificial intelligence AI",
        "에이전트": "agent",
        "하네스": "harness",
        "딥 리서치": "deep research",
        "최신": "latest recent",
        "최근": "recent",
        "연구": "research",
        "논문": "paper",
        "동향": "trends survey",
        "비교": "comparison",
        "분석": "analysis",
        "성능": "performance benchmark",
        "평가": "evaluation",
        "설계": "design architecture",
        "방법론": "methodology",
        "구현": "implementation",
        "오픈소스": "open source",
        "검색": "search",
        "출처": "sources",
        "근거": "evidence",
        "한계": "limitations",
        "관련": "",
        "정리해줘": "",
        "알려줘": "",
        "찾아줘": "",
        "해줘": "",
    }
    remaining = korean_query
    parts: List[str] = []
    for source, target in sorted(mappings.items(), key=lambda item: -len(item[0])):
        if source in remaining:
            remaining = remaining.replace(source, " ")
            if target:
                parts.append(target)
    parts.extend(word for word in remaining.split() if not _is_korean(word))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _deduplicate_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    output: List[Dict[str, Any]] = []
    for result in results:
        url = str(result.get("href") or result.get("url") or "").rstrip("/").lower()
        if not url or url in seen:
            continue
        seen.add(url)
        output.append(result)
    return output


def _interleave(first: List[Dict[str, Any]], second: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for index in range(max(len(first), len(second))):
        if index < len(first):
            merged.append(first[index])
        if index < len(second):
            merged.append(second[index])
    return merged


def _page_excerpt(
    url: str,
    session: requests.Session,
    *,
    max_chars: int = 24_000,
    cfg: Optional[BuildupConfig] = None,
) -> str:
    """Read a public page through the same safety policy as deep research."""
    if not is_public_web_url(url):
        return ""
    selected = replace(cfg or BuildupConfig(), research_source_chars=max_chars)
    try:
        return SafeWebReader(selected, session=session).read(url).content[:max_chars]
    except Exception:
        return ""


def web_search(
    query: str,
    cfg: BuildupConfig,
    session: requests.Session,
    max_results: int = 5,
    bilingual: bool = True,
) -> Tuple[List[Dict[str, Any]], str]:
    """Search configured providers and return the legacy result shape."""
    logger = logging.getLogger("buildup.search")
    max_results = max(1, min(20, int(max_results)))
    broker = SearchBroker(cfg, session=session)
    try:
        primary = broker.search(query, max_results=max_results)
    except SearchProviderError as exc:
        logger.warning("Search failed: %s", exc)
        return [], "none"
    primary_rows = [item.legacy_dict() for item in primary.results]
    providers = [primary.provider]
    if not bilingual or not _is_korean(query):
        return primary_rows, primary.provider

    english = _generate_english_query(query)
    if len(english.split()) < 2 or english.lower() == query.lower():
        return primary_rows, primary.provider
    try:
        secondary = broker.search(english, max_results=max(3, max_results // 2 + 1))
        secondary_rows = [item.legacy_dict() for item in secondary.results]
        if secondary.provider not in providers:
            providers.append(secondary.provider)
        merged = _deduplicate_results(_interleave(primary_rows, secondary_rows))[:max_results]
        return merged, ", ".join(providers)
    except SearchProviderError as exc:
        logger.warning("English search variant failed: %s", exc)
        return primary_rows, primary.provider


def research_search(
    query: str,
    cfg: BuildupConfig,
    session: requests.Session,
    max_results: int = 10,
) -> Tuple[List[Dict[str, Any]], str]:
    """Search with raw-content retrieval and safe public-page hydration."""
    research_cfg = cfg
    if cfg.search_provider in {"auto", "tavily"} and cfg.tavily_api_key:
        research_cfg = replace(cfg, tavily_search_depth="advanced")
    results, provider = web_search(
        query,
        research_cfg,
        session,
        max_results=max_results,
        bilingual=True,
    )
    hydrated: List[Dict[str, Any]] = []
    for result in results:
        item = dict(result)
        if not str(item.get("raw_content") or "").strip():
            item["raw_content"] = _page_excerpt(
                str(item.get("href") or ""),
                session,
                max_chars=cfg.research_source_chars,
                cfg=cfg,
            )
        hydrated.append(item)
    return hydrated, provider


def format_search_results(results: List[Dict[str, Any]], engine: str = "") -> str:
    if not results:
        return "(검색 결과 없음)"
    header = f"[{engine}]  " if engine else ""
    parts: List[str] = []
    for index, result in enumerate(results, 1):
        parts.append(
            f"[{index}] {result.get('title', '')}\n"
            f"    {result.get('href', '')}\n"
            f"    {result.get('body', '')}"
        )
    return f"{header}{len(results)}건\n\n" + "\n\n".join(parts)


__all__ = [
    "format_search_results",
    "is_public_web_url",
    "research_search",
    "web_search",
]
