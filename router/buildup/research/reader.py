"""Safe public-page reader with redirect, robots, size, and cache controls."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests

from buildup.config import BuildupConfig


USER_AGENT = "Build-upResearch/0.2 (+local evidence research; respects robots.txt)"
REDIRECT_CODES = {301, 302, 303, 307, 308}
TRACKING_PARAMS = {
    "dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid", "ref_src",
}


@dataclass(frozen=True)
class ReaderResult:
    url: str
    final_url: str
    title: str
    content: str
    content_type: str
    retrieved_at: str
    content_sha256: str
    locator_kind: str
    metadata: Dict[str, Any]
    raw_sha256: str = ""
    raw_bytes: bytes = field(default=b"", repr=False)


class ReaderError(RuntimeError):
    pass


class RobotsDenied(ReaderError):
    pass


class ResponseTooLarge(ReaderError):
    pass


def is_public_web_url(url: str, *, resolve_dns: bool = False) -> bool:
    """Return False for local, private, link-local, and non-HTTP targets."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        addresses: List[str] = []
        try:
            addresses.append(str(ipaddress.ip_address(host)))
        except ValueError:
            if resolve_dns:
                addresses.extend(
                    item[4][0]
                    for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
                )
        return all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    return urlunparse((scheme, netloc, path, "", urlencode(sorted(query)), ""))


class SafeWebReader:
    def __init__(self, cfg: BuildupConfig, *, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self._robots: Dict[str, Tuple[bool, str]] = {}

    def read(self, url: str, *, force_refresh: bool = False) -> ReaderResult:
        normalized = normalize_url(url)
        if not is_public_web_url(normalized, resolve_dns=True):
            raise ReaderError(f"공개 HTTP(S) URL이 아닙니다: {url}")
        if not force_refresh:
            cached = self._load_cache(normalized)
            if cached:
                return cached
        if self.cfg.respect_robots_txt:
            allowed, reason = self._robots_allowed(normalized)
            if not allowed:
                raise RobotsDenied(f"robots.txt가 읽기를 허용하지 않습니다: {normalized} ({reason})")

        response, final_url = self._get_with_safe_redirects(normalized)
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].lower()
        max_bytes = self.cfg.max_pdf_bytes if content_type == "application/pdf" else 10 * 1024 * 1024
        try:
            raw = self._bounded_body(response, max_bytes=max_bytes)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        is_pdf = content_type == "application/pdf" or final_url.lower().endswith(".pdf")
        if is_pdf:
            content = self._extract_pdf(raw)
            title = Path(urlparse(final_url).path).name or "PDF document"
            locator_kind = "page"
        elif (
            content_type.startswith("text/")
            or "html" in content_type
            or content_type in {
                "application/json",
                "application/xml",
                "application/rss+xml",
                "application/atom+xml",
            }
            or content_type.endswith(("+json", "+xml"))
            or not content_type
        ):
            decoded = self._decode(response, raw)
            if "html" in content_type or "<html" in decoded[:1000].lower():
                parser = _ReadableHTML()
                parser.feed(decoded)
                content = parser.text()
                title = parser.title or urlparse(final_url).hostname or final_url
                locator_kind = "section"
            else:
                content = re.sub(r"\s+", " ", decoded).strip()
                title = Path(urlparse(final_url).path).name or urlparse(final_url).hostname or final_url
                locator_kind = "line"
        else:
            raise ReaderError(f"지원하지 않는 content type입니다: {content_type or 'unknown'}")

        content = content[: self.cfg.research_source_chars]
        if not content:
            raise ReaderError(f"읽을 수 있는 본문이 없습니다: {final_url}")
        result = ReaderResult(
            url=normalized,
            final_url=normalize_url(final_url),
            title=title.strip()[:500],
            content=content,
            content_type=content_type or "text/html",
            retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            locator_kind=locator_kind,
            metadata={
                "http_status": int(response.status_code),
                "content_length": len(raw),
                "cache": "miss",
                "robots_respected": self.cfg.respect_robots_txt,
            },
            raw_sha256=hashlib.sha256(raw).hexdigest() if is_pdf else "",
            raw_bytes=raw if is_pdf else b"",
        )
        self._save_cache(normalized, result)
        return result

    def _get_with_safe_redirects(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        raise_for_status: bool = True,
    ) -> Tuple[Any, str]:
        current = url
        response = None
        request_headers = headers or {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,text/plain,application/pdf;q=0.9,*/*;q=0.1",
        }
        for _ in range(6):
            if not is_public_web_url(current, resolve_dns=True):
                raise ReaderError(f"리디렉션 대상이 공개 URL이 아닙니다: {current}")
            response = self.session.get(
                current,
                headers=request_headers,
                timeout=(10, self.cfg.search_timeout),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code not in REDIRECT_CODES:
                if raise_for_status:
                    response.raise_for_status()
                return response, current
            location = str(response.headers.get("location") or "").strip()
            if not location:
                raise ReaderError("Location 없는 HTTP redirect를 받았습니다.")
            next_url = urljoin(current, location)
            close = getattr(response, "close", None)
            if callable(close):
                close()
            current = next_url
        raise ReaderError("HTTP redirect 한도(5회)를 초과했습니다.")

    def _robots_allowed(self, url: str) -> Tuple[bool, str]:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._robots.get(origin)
        if cached:
            return cached
        robots_url = f"{origin}/robots.txt"
        try:
            if not is_public_web_url(robots_url, resolve_dns=True):
                result = (False, "robots endpoint is not public")
            else:
                response, final_robots_url = self._get_with_safe_redirects(
                    robots_url,
                    headers={"User-Agent": USER_AGENT},
                    raise_for_status=False,
                )
                if response.status_code in {401, 403}:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                    result = (False, f"robots HTTP {response.status_code}")
                elif response.status_code >= 400:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                    result = (True, f"robots unavailable: HTTP {response.status_code}")
                else:
                    try:
                        raw = self._bounded_body(response, max_bytes=1024 * 1024)
                    finally:
                        close = getattr(response, "close", None)
                        if callable(close):
                            close()
                    parser = RobotFileParser()
                    parser.set_url(final_robots_url)
                    parser.parse(self._decode(response, raw).splitlines())
                    result = (parser.can_fetch(USER_AGENT, url), "robots policy")
        except requests.RequestException as exc:
            # An unavailable robots endpoint is not evidence that crawling is forbidden.
            result = (True, f"robots unavailable: {type(exc).__name__}")
        self._robots[origin] = result
        return result

    @staticmethod
    def _bounded_body(response: Any, *, max_bytes: int) -> bytes:
        header = str(response.headers.get("content-length") or "")
        if header.isdigit() and int(header) > max_bytes:
            raise ResponseTooLarge(f"응답 크기가 제한을 초과합니다: {header} > {max_bytes}")
        chunks: List[bytes] = []
        size = 0
        if hasattr(response, "iter_content"):
            iterator = response.iter_content(chunk_size=64 * 1024)
        else:
            iterator = [bytes(response.content)]
        for chunk in iterator:
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLarge(f"응답 크기가 제한을 초과합니다: > {max_bytes}")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode(response: Any, raw: bytes) -> str:
        encoding = getattr(response, "encoding", None) or "utf-8"
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _extract_pdf(raw: bytes) -> str:
        try:
            from pdfminer.high_level import extract_text

            return extract_text(BytesIO(raw)).strip()
        except Exception as exc:
            raise ReaderError(f"PDF 본문 추출에 실패했습니다: {exc}") from exc

    def _cache_path(self, normalized_url: str) -> Path:
        digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        return self.cfg.search_cache_dir / "pages" / f"{digest}.json"

    def _load_cache(self, normalized_url: str) -> Optional[ReaderResult]:
        if self.cfg.search_cache_ttl_hours == 0:
            return None
        path = self._cache_path(normalized_url)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stored = datetime.fromisoformat(str(payload["stored_at"]))
            if datetime.now(timezone.utc) - stored.astimezone(timezone.utc) > timedelta(
                hours=self.cfg.search_cache_ttl_hours
            ):
                return None
            value = payload["result"]
            original_url = str(value["url"])
            final_url = str(value["final_url"])
            content = str(value["content"])
            content_sha256 = str(value["content_sha256"])
            content_type = str(value["content_type"])
            if content_type == "application/pdf" or final_url.lower().endswith(".pdf"):
                return None
            if (
                normalize_url(original_url) != normalized_url
                or not is_public_web_url(original_url, resolve_dns=True)
                or not is_public_web_url(final_url, resolve_dns=True)
                or hashlib.sha256(content.encode("utf-8")).hexdigest()
                != content_sha256
            ):
                return None
            metadata = dict(value.get("metadata") or {})
            metadata["cache"] = "hit"
            return ReaderResult(
                url=original_url,
                final_url=final_url,
                title=str(value["title"]),
                content=content,
                content_type=content_type,
                retrieved_at=str(value["retrieved_at"]),
                content_sha256=content_sha256,
                locator_kind=str(value["locator_kind"]),
                metadata=metadata,
                raw_sha256="",
                raw_bytes=b"",
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save_cache(self, normalized_url: str, result: ReaderResult) -> None:
        # A cached extracted PDF is not an immutable original. Re-fetch PDFs so
        # every research run can archive and seal the exact bytes it cited.
        if self.cfg.search_cache_ttl_hours == 0 or result.raw_bytes:
            return
        path = self._cache_path(normalized_url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            serialized_result = asdict(result)
            serialized_result.pop("raw_bytes", None)
            payload = {
                "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "result": serialized_result,
            }
            fd, temporary = tempfile.mkstemp(
                dir=str(path.parent), prefix=".page-", suffix=".tmp"
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


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: List[str] = []
        self._in_title = False
        self._title_parts: List[str] = []

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._title_parts)).strip()

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "form"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "form"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        clean = html.unescape(data).strip()
        if not clean:
            return
        if self._in_title:
            self._title_parts.append(clean)
        if not self._skip_depth:
            self._parts.append(clean)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()
