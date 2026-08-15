"""Normalized search providers with deterministic failover and local caching."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import requests

from buildup.config import BuildupConfig

from .reader import is_public_web_url, normalize_url


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    provider: str
    query: str
    raw_content: str = ""
    score: Optional[float] = None
    published_at: str = ""
    author: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def legacy_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "href": self.url,
            "body": self.snippet,
            "raw_content": self.raw_content,
            "score": self.score,
            "published_at": self.published_at,
            "author": self.author,
            "provider": self.provider,
            "query": self.query,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class SearchResponse:
    results: List[SearchResult]
    provider: str
    cache_hit: bool = False
    failures: List[Dict[str, str]] = field(default_factory=list)


class SearchProviderError(RuntimeError):
    pass


class NoSearchProviderConfigured(SearchProviderError):
    pass


class SearchProvider:
    name = "base"

    def __init__(self, cfg: BuildupConfig, session: requests.Session):
        self.cfg = cfg
        self.session = session

    def available(self) -> bool:
        return False

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        raise NotImplementedError

    def _get_json(self, url: str, **kwargs: Any) -> Dict[str, Any]:
        kwargs.setdefault("timeout", (10, self.cfg.search_timeout))
        try:
            response = self.session.get(url, **kwargs)
            response.raise_for_status()
            value = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            raise SearchProviderError(f"{self.name} request failed: {detail}") from exc
        if not isinstance(value, dict):
            raise SearchProviderError(f"{self.name} returned a non-object JSON response")
        return value

    def _post_json(self, url: str, **kwargs: Any) -> Dict[str, Any]:
        kwargs.setdefault("timeout", (10, self.cfg.search_timeout))
        try:
            response = self.session.post(url, **kwargs)
            response.raise_for_status()
            value = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            raise SearchProviderError(f"{self.name} request failed: {detail}") from exc
        if not isinstance(value, dict):
            raise SearchProviderError(f"{self.name} returned a non-object JSON response")
        return value


class TavilyProvider(SearchProvider):
    name = "tavily"

    def available(self) -> bool:
        return bool(self.cfg.tavily_api_key)

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        payload: Dict[str, Any] = {
            "query": query,
            "search_depth": self.cfg.tavily_search_depth,
            "max_results": min(max_results, 20),
            "topic": "general",
            "include_answer": False,
            "include_raw_content": "markdown",
            "include_images": False,
        }
        if self.cfg.tavily_search_depth == "advanced":
            payload["chunks_per_source"] = 3
        data = self._post_json(
            "https://api.tavily.com/search",
            json=payload,
            headers={
                "Authorization": f"Bearer {self.cfg.tavily_api_key}",
                "Content-Type": "application/json",
            },
        )
        return _normalize_rows(self.name, query, data.get("results", []), {
            "url": "url", "title": "title", "snippet": "content", "raw_content": "raw_content",
            "score": "score", "published_at": "published_date",
        })


class BraveProvider(SearchProvider):
    name = "brave"

    def available(self) -> bool:
        return bool(self.cfg.brave_search_api_key)

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        params: Dict[str, Any] = {"q": query, "count": min(max_results, 20), "safesearch": "moderate"}
        if self.cfg.web_search_lang in {"ko", "en"}:
            params["search_lang"] = self.cfg.web_search_lang
        data = self._get_json(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.cfg.brave_search_api_key,
            },
        )
        web = data.get("web") if isinstance(data.get("web"), dict) else {}
        return _normalize_rows(self.name, query, web.get("results", []), {
            "url": "url", "title": "title", "snippet": "description", "published_at": "age",
        })


class ExaProvider(SearchProvider):
    name = "exa"

    def available(self) -> bool:
        return bool(self.cfg.exa_api_key)

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        data = self._post_json(
            "https://api.exa.ai/search",
            json={
                "query": query,
                "type": "auto",
                "numResults": min(max_results, 20),
                "contents": {
                    "text": {
                        "maxCharacters": min(self.cfg.research_source_chars, 10_000),
                        "includeHtmlTags": False,
                    }
                },
            },
            headers={"x-api-key": self.cfg.exa_api_key, "Content-Type": "application/json"},
        )
        return _normalize_rows(self.name, query, data.get("results", []), {
            "url": "url", "title": "title", "snippet": "text", "raw_content": "text",
            "score": "score", "published_at": "publishedDate", "author": "author",
        })


class OpenAlexProvider(SearchProvider):
    """Scholarly discovery through the official OpenAlex Works API.

    OpenAlex metadata is discovery context, not evidence text. ``raw_content``
    deliberately remains empty so the research reader must fetch and hash the
    original landing page or open-access PDF before a claim can cite it.
    """

    name = "openalex"

    def available(self) -> bool:
        # OpenAlex currently grants a small free daily budget to registered API
        # keys. We fail closed instead of silently relying on the tiny anonymous
        # allowance or making an operator think the API is unlimited/free.
        return bool(self.cfg.openalex_api_key)

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        data = self._get_json(
            "https://api.openalex.org/works",
            params={
                "search": query,
                "per_page": min(max_results, 100),
                "api_key": self.cfg.openalex_api_key,
                "select": ",".join((
                    "id", "doi", "display_name", "publication_year",
                    "publication_date", "type", "cited_by_count",
                    "is_retracted", "authorships", "primary_location",
                    "best_oa_location", "open_access",
                    "abstract_inverted_index", "relevance_score",
                )),
            },
            headers={"Accept": "application/json"},
        )
        rows = data.get("results")
        if not isinstance(rows, list):
            return []
        results: List[SearchResult] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            target = _openalex_target_url(row)
            if not target:
                continue
            try:
                target = normalize_url(target)
            except ValueError:
                continue
            authorships = row.get("authorships")
            author_names: List[str] = []
            if isinstance(authorships, list):
                for authorship in authorships[:12]:
                    if not isinstance(authorship, dict):
                        continue
                    author = authorship.get("author")
                    if isinstance(author, dict):
                        name = str(author.get("display_name") or "").strip()
                        if name:
                            author_names.append(name)
            try:
                score = float(row["relevance_score"])
                if not math.isfinite(score):
                    score = None
            except (KeyError, TypeError, ValueError):
                score = None
            open_access = row.get("open_access")
            metadata = {
                "openalex_id": str(row.get("id") or ""),
                "doi": str(row.get("doi") or ""),
                "publication_year": row.get("publication_year"),
                "work_type": str(row.get("type") or ""),
                "cited_by_count": row.get("cited_by_count"),
                "is_retracted": bool(row.get("is_retracted")),
                "is_oa": bool(open_access.get("is_oa"))
                if isinstance(open_access, dict) else False,
            }
            results.append(SearchResult(
                title=str(row.get("display_name") or "Untitled").strip()[:500],
                url=target,
                snippet=_openalex_abstract(row.get("abstract_inverted_index")),
                raw_content="",
                score=score,
                published_at=str(row.get("publication_date") or ""),
                author=", ".join(author_names)[:1000],
                provider=self.name,
                query=query,
                metadata=metadata,
            ))
        return _deduplicate(results)[:max_results]


class SearXNGProvider(SearchProvider):
    name = "searxng"

    def available(self) -> bool:
        return bool(self.cfg.searxng_url)

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        endpoint = urljoin(self.cfg.searxng_url.rstrip("/") + "/", "search")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SearchProviderError("BUILDUP_SEARXNG_URL must be an HTTP(S) endpoint")
        params: Dict[str, Any] = {"q": query, "format": "json", "safesearch": 1}
        if self.cfg.web_search_lang in {"ko", "en"}:
            params["language"] = self.cfg.web_search_lang
        data = self._get_json(endpoint, params=params, headers={"Accept": "application/json"})
        rows = _normalize_rows(self.name, query, data.get("results", []), {
            "url": "url", "title": "title", "snippet": "content", "score": "score",
            "published_at": "publishedDate",
        })
        return rows[:max_results]


class GoogleCSEProvider(SearchProvider):
    """Legacy provider for existing customers until Google's 2027 shutdown."""

    name = "google-cse"

    def available(self) -> bool:
        return bool(self.cfg.google_cse_api_key and self.cfg.google_cse_cx)

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        params: Dict[str, Any] = {
            "key": self.cfg.google_cse_api_key,
            "cx": self.cfg.google_cse_cx,
            "q": query,
            "num": min(max_results, 10),
        }
        if self.cfg.web_search_lang in {"ko", "en"}:
            params["lr"] = f"lang_{self.cfg.web_search_lang}"
        data = self._get_json("https://customsearch.googleapis.com/customsearch/v1", params=params)
        return _normalize_rows(self.name, query, data.get("items", []), {
            "url": "link", "title": "title", "snippet": "snippet",
        })


class BrowserProvider(SearchProvider):
    """Opt-in, keyless discovery through a normal local Chromium session."""

    name = "browser"

    def available(self) -> bool:
        return bool(self.cfg.browser_search_enabled)

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SearchProviderError(
                "브라우저 검색에는 `pip install -e '.[browser]'`와 "
                "`playwright install chromium`이 필요합니다."
            ) from exc

        engine = self.cfg.browser_search_engine.lower()
        targets = {
            "google": (f"https://www.google.com/search?q={quote_plus(query)}", "a"),
            "brave": (f"https://search.brave.com/search?q={quote_plus(query)}", "a"),
            "bing": (f"https://www.bing.com/search?q={quote_plus(query)}", "li.b_algo h2 a"),
        }
        if engine not in targets:
            raise SearchProviderError(f"지원하지 않는 browser search engine입니다: {engine}")
        target_url, selector = targets[engine]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.cfg.browser_headless)
            try:
                context = browser.new_context(user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/125 Safari/537.36"
                ))
                page = context.new_page()
                page.goto(target_url, wait_until="domcontentloaded", timeout=self.cfg.search_timeout * 1000)
                visible = (page.title() + " " + page.locator("body").inner_text(timeout=5000)[:5000]).lower()
                if any(marker in visible for marker in (
                    "captcha", "unusual traffic", "verify you are human", "자동화된 쿼리",
                    "before you continue to google", "동의하기 전에",
                )):
                    raise SearchProviderError(
                        "검색 엔진이 CAPTCHA 또는 동의 화면을 표시했습니다. Build-up은 이를 우회하지 않습니다."
                    )
                rows: List[SearchResult] = []
                for element in page.locator(selector).all()[: max_results * 8]:
                    href = str(element.get_attribute("href") or "").strip()
                    title = str(element.inner_text() or "").strip()
                    href = _unwrap_search_url(href, target_url)
                    if not title or len(title) < 3 or not is_public_web_url(href):
                        continue
                    if urlparse(href).hostname in {
                        "www.google.com", "google.com", "www.bing.com", "search.brave.com",
                    }:
                        continue
                    rows.append(SearchResult(title, normalize_url(href), "", self.name, query))
                    if len(rows) >= max_results:
                        break
                return _deduplicate(rows)
            finally:
                browser.close()


PROVIDER_TYPES = {
    "tavily": TavilyProvider,
    "brave": BraveProvider,
    "exa": ExaProvider,
    "openalex": OpenAlexProvider,
    "searxng": SearXNGProvider,
    "google": GoogleCSEProvider,
    "google-cse": GoogleCSEProvider,
    "browser": BrowserProvider,
}


class SearchBroker:
    def __init__(self, cfg: BuildupConfig, *, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        clean_query = re.sub(r"\s+", " ", query).strip()
        if not clean_query:
            raise ValueError("검색어가 비어 있습니다.")
        max_results = max(1, min(20, int(max_results)))
        providers = self._providers(clean_query)
        if not providers:
            raise NoSearchProviderConfigured(
                "사용 가능한 검색 공급자가 없습니다. Tavily/Brave/Exa/OpenAlex 키, "
                "BUILDUP_SEARXNG_URL, 또는 BUILDUP_BROWSER_SEARCH_ENABLED=true 중 하나를 설정하세요."
            )

        # Check every configured provider's cache before touching the network.
        # This prevents a cached fallback result from repeatedly waiting on a
        # higher-priority provider that is currently unavailable.
        for provider in providers:
            cached = self._load_cache(provider.name, clean_query, max_results)
            if cached is not None:
                return SearchResponse(cached, provider.name, cache_hit=True)

        failures: List[Dict[str, str]] = []
        for provider in providers:
            try:
                results = _deduplicate(provider.search(clean_query, max_results))[:max_results]
                if not results:
                    failures.append({"provider": provider.name, "error": "no results"})
                    continue
                self._save_cache(provider.name, clean_query, max_results, results)
                return SearchResponse(results, provider.name, cache_hit=False, failures=failures)
            except Exception as exc:
                failures.append({"provider": provider.name, "error": f"{type(exc).__name__}: {exc}"})
        message = " | ".join(f"{item['provider']}: {item['error']}" for item in failures)
        raise SearchProviderError(f"모든 검색 공급자가 실패했습니다. {message}")

    def _providers(self, query: str = "") -> List[SearchProvider]:
        selected = self.cfg.search_provider.strip().lower()
        if selected != "auto":
            provider_type = PROVIDER_TYPES.get(selected)
            if not provider_type:
                raise NoSearchProviderConfigured(f"알 수 없는 검색 공급자입니다: {selected}")
            provider = provider_type(self.cfg, self.session)
            if not provider.available():
                raise NoSearchProviderConfigured(f"검색 공급자 {selected!r}의 설정이 완료되지 않았습니다.")
            return [provider]
        scholarly = bool(re.search(
            r"\b(?:paper|papers|study|studies|journal|doi|arxiv|preprint|"
            r"systematic review|meta-analysis|dataset)\b|"
            r"논문|학술|저널|연구\s*결과|체계적\s*문헌|메타\s*분석|데이터셋",
            query,
            re.I,
        ))
        order = (
            (
                OpenAlexProvider, TavilyProvider, BraveProvider, ExaProvider,
                SearXNGProvider, GoogleCSEProvider, BrowserProvider,
            )
            if scholarly else
            (
                TavilyProvider, BraveProvider, ExaProvider, SearXNGProvider,
                GoogleCSEProvider, BrowserProvider, OpenAlexProvider,
            )
        )
        return [provider for provider_type in order if (provider := provider_type(self.cfg, self.session)).available()]

    def _cache_path(self, provider: str, query: str, max_results: int) -> Path:
        key = json.dumps({
            "provider": provider,
            "query": query,
            "max_results": max_results,
            "language": self.cfg.web_search_lang,
            "v": 1,
        }, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cfg.search_cache_dir / "queries" / f"{digest}.json"

    def _load_cache(self, provider: str, query: str, max_results: int) -> Optional[List[SearchResult]]:
        if self.cfg.search_cache_ttl_hours == 0:
            return None
        path = self._cache_path(provider, query, max_results)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stored = datetime.fromisoformat(str(payload["stored_at"]))
            if datetime.now(timezone.utc) - stored.astimezone(timezone.utc) > timedelta(
                hours=self.cfg.search_cache_ttl_hours
            ):
                return None
            cached_rows = payload["results"]
            if not isinstance(cached_rows, list):
                return None
            serialized = json.dumps(
                cached_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if payload.get("results_sha256") != hashlib.sha256(
                serialized.encode("utf-8")
            ).hexdigest():
                return None
            results: List[SearchResult] = []
            for item in cached_rows:
                if not isinstance(item, dict):
                    continue
                result = SearchResult(**item)
                if is_public_web_url(result.url):
                    results.append(result)
            return results
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save_cache(
        self, provider: str, query: str, max_results: int, results: Sequence[SearchResult]
    ) -> None:
        if self.cfg.search_cache_ttl_hours == 0:
            return
        path = self._cache_path(provider, query, max_results)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            cached_rows = [asdict(item) for item in results]
            serialized = json.dumps(
                cached_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            payload = {
                "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "results": cached_rows,
                "results_sha256": hashlib.sha256(
                    serialized.encode("utf-8")
                ).hexdigest(),
            }
            fd, temporary = tempfile.mkstemp(
                dir=str(path.parent), prefix=".query-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                if "temporary" in locals():
                    os.unlink(temporary)
            except (OSError, UnboundLocalError):
                pass


def _normalize_rows(
    provider: str,
    query: str,
    rows: Any,
    fields: Dict[str, str],
) -> List[SearchResult]:
    results: List[SearchResult] = []
    if not isinstance(rows, list):
        return results
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get(fields["url"]) or "").strip()
        if not is_public_web_url(url):
            continue
        score_value = row.get(fields.get("score", ""))
        try:
            score = float(score_value) if score_value is not None else None
            if score is not None and not math.isfinite(score):
                score = None
        except (TypeError, ValueError):
            score = None
        try:
            normalized_url = normalize_url(url)
        except ValueError:
            continue
        results.append(SearchResult(
            title=str(row.get(fields["title"]) or "Untitled").strip()[:500],
            url=normalized_url,
            snippet=str(row.get(fields["snippet"]) or "").strip(),
            raw_content=str(row.get(fields.get("raw_content", "")) or "").strip(),
            score=score,
            published_at=str(row.get(fields.get("published_at", "")) or "").strip(),
            author=str(row.get(fields.get("author", "")) or "").strip(),
            provider=provider,
            query=query,
        ))
    return results


def _openalex_target_url(row: Dict[str, Any]) -> str:
    """Choose an original, readable target while retaining OpenAlex metadata."""
    candidates: List[Any] = []
    for key in ("best_oa_location", "primary_location"):
        location = row.get(key)
        if isinstance(location, dict):
            candidates.extend((location.get("pdf_url"), location.get("landing_page_url")))
    candidates.extend((row.get("doi"), row.get("id")))
    for value in candidates:
        candidate = str(value or "").strip()
        if is_public_web_url(candidate):
            return candidate
    return ""


def _openalex_abstract(value: Any) -> str:
    """Rebuild OpenAlex's inverted abstract deterministically and defensively."""
    if not isinstance(value, dict):
        return ""
    positioned: List[tuple[int, str]] = []
    for token, positions in value.items():
        if not isinstance(token, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and 0 <= position <= 100_000:
                positioned.append((position, token))
    positioned.sort(key=lambda item: item[0])
    return " ".join(token for _, token in positioned)[:5_000]


def _deduplicate(rows: Iterable[SearchResult]) -> List[SearchResult]:
    seen: set[str] = set()
    result: List[SearchResult] = []
    for row in rows:
        try:
            key = normalize_url(row.url)
        except ValueError:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _unwrap_search_url(href: str, base_url: str) -> str:
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.path == "/url":
        query = parse_qs(parsed.query)
        return str((query.get("q") or query.get("url") or [absolute])[0])
    return absolute
