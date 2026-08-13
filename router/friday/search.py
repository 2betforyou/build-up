"""Web search: Tavily (optional) + Google Custom Search API (legacy).

Enhancements over v1:
  - Bilingual search: Korean queries automatically spawn an English query too.
  - Deduplication across language variants.
  - Increased default depth for Tavily ("advanced").
  - Result merging with interleaved ranking.
"""

from __future__ import annotations

import logging
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from friday.config import FridayConfig


class _ReadableHTML(HTMLParser):
    """Small dependency-free extractor for research page text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "nav"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "nav"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def is_public_web_url(url: str, *, resolve_dns: bool = False) -> bool:
    """Reject local/private targets before fetching a search result."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        addresses: List[str] = []
        try:
            addresses.append(str(ipaddress.ip_address(host)))
        except ValueError:
            if resolve_dns:
                addresses.extend(
                    item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                )
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                return False
        return True
    except (OSError, ValueError):
        return False


def _duckduckgo_search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Keyless fallback; prefer the renamed ddgs package when installed."""
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError as exc:
            raise RuntimeError("DuckDuckGo 검색에는 `pip install ddgs`가 필요합니다.") from exc

    rows = DDGS().text(query, region="wt-wt", safesearch="moderate", max_results=max_results)
    results: List[Dict[str, str]] = []
    for row in rows or []:
        url = str(row.get("href") or row.get("url") or "").strip()
        if not is_public_web_url(url):
            continue
        results.append({
            "title": str(row.get("title") or ""),
            "href": url,
            "body": str(row.get("body") or row.get("description") or ""),
        })
    return results


def _page_excerpt(url: str, session: requests.Session, *, max_chars: int = 24_000) -> str:
    """Fetch readable public HTML for grounding; return empty on any failure."""
    try:
        current_url = url
        response = None
        for _ in range(4):
            if not is_public_web_url(current_url, resolve_dns=True):
                return ""
            response = session.get(
                current_url,
                headers={"User-Agent": "build-up-research/0.1 (+local research tool)"},
                timeout=15,
                allow_redirects=False,
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = str(response.headers.get("location") or "").strip()
            if not location:
                return ""
            current_url = urljoin(current_url, location)
        if response is None or response.status_code in {301, 302, 303, 307, 308}:
            return ""
        response.raise_for_status()
        content_type = str(response.headers.get("content-type") or "").lower()
        if "html" not in content_type and "text/plain" not in content_type:
            return ""
        raw = response.text[:750_000]
        if "html" in content_type:
            parser = _ReadableHTML()
            parser.feed(raw)
            return parser.text()[:max_chars]
        return re.sub(r"\s+", " ", raw).strip()[:max_chars]
    except Exception:
        return ""


def _hydrate_research_results(
    results: List[Dict[str, Any]],
    session: requests.Session,
) -> List[Dict[str, Any]]:
    hydrated: List[Dict[str, Any]] = []
    for result in results:
        item = dict(result)
        if not item.get("raw_content"):
            item["raw_content"] = _page_excerpt(str(item.get("href") or ""), session)
        hydrated.append(item)
    return hydrated


def _tavily_search(
    query: str,
    cfg: FridayConfig,
    session: requests.Session,
    max_results: int = 5,
    search_depth: Optional[str] = None,
    include_raw_content: bool = False,
) -> List[Dict[str, Any]]:
    """Search via Tavily Search API."""
    if not getattr(cfg, "tavily_api_key", None):
        return []

    depth = search_depth or getattr(cfg, "tavily_search_depth", "advanced")

    url = "https://api.tavily.com/search"
    payload: Dict[str, Any] = {
        "query": query,
        "search_depth": depth,
        "max_results": max_results,
        "topic": "general",
        "include_answer": False,
        "include_raw_content": "markdown" if include_raw_content else False,
        "include_images": False,
    }
    if include_raw_content and depth == "advanced":
        payload["chunks_per_source"] = 3
    headers = {
        "Authorization": f"Bearer {cfg.tavily_api_key}",
        "Content-Type": "application/json",
    }

    resp = session.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    results: List[Dict[str, Any]] = []
    for item in data.get("results", []):
        results.append({
            "title": item.get("title", ""),
            "href": item.get("url", ""),
            "body": item.get("content", ""),
            "raw_content": item.get("raw_content", "") or "",
            "score": item.get("score"),
        })
    return results


def _google_cse_search(
    query: str,
    cfg: FridayConfig,
    session: requests.Session,
    max_results: int = 5,
) -> List[Dict[str, str]]:
    """Search via Google Custom Search JSON API."""
    if not cfg.google_cse_api_key or not cfg.google_cse_cx:
        return []

    url = "https://www.googleapis.com/customsearch/v1"
    params: Dict[str, Any] = {
        "key": cfg.google_cse_api_key,
        "cx": cfg.google_cse_cx,
        "q": query,
        "num": min(max_results, 10),
    }

    if cfg.web_search_lang == "en":
        params["lr"] = "lang_en"
        params["gl"] = "us"
    elif cfg.web_search_lang == "ko":
        params["lr"] = "lang_ko"
        params["gl"] = "kr"

    resp = session.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    results: List[Dict[str, str]] = []
    for item in data.get("items", []):
        results.append({
            "title": item.get("title", ""),
            "href": item.get("link", ""),
            "body": item.get("snippet", ""),
        })
    return results


def _is_korean(text: str) -> bool:
    """True if text contains Korean characters."""
    return bool(re.search(r"[\uac00-\ud7af]", text))


def _generate_english_query(korean_query: str) -> str:
    """Heuristic Korean→English query translation for search.

    This is a keyword-level translation, NOT full translation.
    For common research terms, this gets us 70-80% coverage without LLM.
    Uncommon terms pass through as-is (which often works fine for academic search).
    """
    # Common Korean→English keyword mappings for research/tech domains
    _KO_TO_EN: Dict[str, str] = {
        # Research terms
        "최신": "latest recent 2024 2025",
        "최근": "recent latest",
        "연구": "research",
        "논문": "paper",
        "동향": "trends survey",
        "리뷰": "review survey",
        "서베이": "survey",
        "분석": "analysis",
        "비교": "comparison",
        "성능": "performance benchmark",
        "평가": "evaluation",
        "실험": "experiment",
        "결과": "results",
        "방법론": "methodology",
        "기법": "technique method",
        "모델": "model",
        "학습": "training learning",
        "추론": "inference reasoning",
        "데이터셋": "dataset",
        "벤치마크": "benchmark",
        "프레임워크": "framework",

        # AI/ML terms
        "대규모 언어 모델": "large language model LLM",
        "언어 모델": "language model",
        "강화학습": "reinforcement learning",
        "미세조정": "fine-tuning",
        "프롬프트": "prompt",
        "임베딩": "embedding",
        "트랜스포머": "transformer",
        "어텐션": "attention",
        "생성형": "generative",
        "멀티모달": "multimodal",
        "에이전트": "agent",
        "정렬": "alignment",
        "안전성": "safety",
        "보안": "security",
        "탈옥": "jailbreak",
        "레드팀": "red teaming",
        "가드레일": "guardrails",
        "환각": "hallucination",
        "편향": "bias",

        # General tech
        "자동화": "automation",
        "최적화": "optimization",
        "아키텍처": "architecture",
        "배포": "deployment",
        "확장성": "scalability",
        "오픈소스": "open source",

        # Action words (drop these in English query)
        "관련해서": "",
        "관련": "",
        "정리해줘": "",
        "정리": "",
        "알려줘": "",
        "보여줘": "",
        "찾아봐": "",
        "검색해줘": "",
        "해줘": "",
    }

    query = korean_query
    en_parts: List[str] = []

    # Sort by length descending so longer phrases match first
    for ko, en in sorted(_KO_TO_EN.items(), key=lambda x: -len(x[0])):
        if ko in query:
            query = query.replace(ko, "")
            if en:
                en_parts.append(en)

    # Remaining text: pass through non-Korean words and drop Korean
    remaining = query.strip()
    for word in remaining.split():
        if not _is_korean(word):
            en_parts.append(word)

    return " ".join(en_parts).strip()


def _deduplicate_results(
    results: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Remove duplicate URLs, keeping the first occurrence."""
    seen_urls: set = set()
    deduped: List[Dict[str, str]] = []
    for r in results:
        url = r.get("href", "").rstrip("/").lower()
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(r)
    return deduped


def _interleave(
    list_a: List[Dict[str, str]],
    list_b: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Interleave two result lists: a1, b1, a2, b2, ..."""
    merged: List[Dict[str, str]] = []
    i, j = 0, 0
    while i < len(list_a) or j < len(list_b):
        if i < len(list_a):
            merged.append(list_a[i])
            i += 1
        if j < len(list_b):
            merged.append(list_b[j])
            j += 1
    return merged


def web_search(
    query: str,
    cfg: FridayConfig,
    session: requests.Session,
    max_results: int = 5,
    bilingual: bool = True,
) -> Tuple[List[Dict[str, str]], str]:
    """Search the web. Returns (results, engine_used).

    If bilingual=True and the query is Korean, also searches an
    auto-translated English variant and merges results.
    """
    logger = logging.getLogger("friday")
    provider = getattr(cfg, "search_provider", "auto").lower()

    providers: List[Tuple[str, Any]] = []
    if provider == "tavily":
        providers = [("Tavily", _tavily_search)]
    elif provider == "google":
        providers = [("Google", _google_cse_search)]
    elif provider in {"duckduckgo", "ddg"}:
        providers = [("DuckDuckGo", _duckduckgo_search)]
    else:
        providers = [
            ("Tavily", _tavily_search),
            ("Google", _google_cse_search),
            ("DuckDuckGo", _duckduckgo_search),
        ]

    # Determine if we should do bilingual search
    do_bilingual = bilingual and _is_korean(query)
    en_query = ""
    if do_bilingual:
        en_query = _generate_english_query(query)
        if len(en_query.split()) < 2:
            do_bilingual = False  # Too few English keywords, skip

    errors: List[str] = []

    for engine_name, fn in providers:
        try:
            # Primary search (original query)
            per_lang = max_results if not do_bilingual else max(3, max_results // 2 + 1)
            if fn is _duckduckgo_search:
                ko_results = fn(query, per_lang)
            else:
                ko_results = fn(query, cfg, session, per_lang)

            if do_bilingual and en_query:
                # Secondary search (English query)
                logger.info("Bilingual search: EN query = %r", en_query)
                try:
                    if fn is _duckduckgo_search:
                        en_results = fn(en_query, per_lang)
                    else:
                        en_results = fn(en_query, cfg, session, per_lang)
                except Exception as exc:
                    logger.warning("English search failed: %s", exc)
                    en_results = []

                # Interleave and deduplicate
                merged = _interleave(ko_results, en_results)
                results = _deduplicate_results(merged)[:max_results + 3]  # allow a few extra
            else:
                results = ko_results

            if results:
                logger.info(
                    "%s: %d results for %r%s",
                    engine_name, len(results), query,
                    f" (+ EN: {en_query})" if do_bilingual else "",
                )
                return results, engine_name
        except Exception as exc:
            errors.append(f"{engine_name}: {exc}")
            logger.warning("%s search failed: %s", engine_name, exc)

    if errors:
        logger.warning("All search providers failed: %s", " | ".join(errors))
    return [], "none"


def research_search(
    query: str,
    cfg: FridayConfig,
    session: requests.Session,
    max_results: int = 10,
) -> Tuple[List[Dict[str, Any]], str]:
    """Collect deep-research sources without making an LLM call.

    Tavily's advanced search returns page content for grounding.  When Tavily
    is unavailable, fall back to the existing search path and its snippets.
    """
    provider = getattr(cfg, "search_provider", "auto").lower()
    if provider != "google" and getattr(cfg, "tavily_api_key", ""):
        queries = [query]
        if _is_korean(query):
            english = _generate_english_query(query)
            if len(english.split()) >= 2 and english.lower() != query.lower():
                queries.append(english)

        collected: List[Dict[str, Any]] = []
        per_query = max(4, (max_results + len(queries) - 1) // len(queries))
        try:
            for search_query in queries:
                items = _tavily_search(
                    search_query,
                    cfg,
                    session,
                    max_results=per_query,
                    search_depth="advanced",
                    include_raw_content=True,
                )
                for item in items:
                    item["query"] = search_query
                collected.extend(items)
            deduped = _deduplicate_results(collected)[:max_results]
            if deduped:
                return deduped, "Tavily advanced"
        except Exception as exc:
            logging.getLogger("friday").warning("Research search failed: %s", exc)

    results, engine = web_search(
        query,
        cfg,
        session,
        max_results=max_results,
        bilingual=True,
    )
    return _hydrate_research_results(list(results), session), engine


def format_search_results(results: List[Dict[str, str]], engine: str = "") -> str:
    """Format search results into a context string for the model."""
    if not results:
        return "(검색 결과 없음)"
    header = f"[{engine}]  " if engine else ""
    parts: List[str] = []
    for i, r in enumerate(results, 1):
        parts.append(f"[{i}] {r['title']}\n    {r['href']}\n    {r['body']}")
    return f"{header}{len(results)}건\n\n" + "\n\n".join(parts)
