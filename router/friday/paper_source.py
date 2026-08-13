"""Resolve paper sources such as arXiv IDs, arXiv URLs, and PDF URLs."""

from __future__ import annotations

import ipaddress
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote, urlparse

import requests

from friday.config import FridayConfig
from friday.paths import slugify
from friday.sandbox import require_library_write, require_read, require_write


ARXIV_API_URL = "https://export.arxiv.org/api/query"
_NEW_ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b", re.I)
_OLD_ARXIV_ID_RE = re.compile(r"\b([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?\b", re.I)
_ARXIV_PREFIX_RE = re.compile(r"^\s*arxiv\s*:\s*", re.I)


@dataclass(frozen=True)
class PaperSource:
    """A normalized source that can be downloaded or loaded."""

    source: str
    kind: str                       # arxiv | pdf_url | local_relpath
    arxiv_id: str = ""
    url: str = ""


@dataclass(frozen=True)
class PaperSourceResult:
    """A resolved paper source inside the current job."""

    relpath: str
    path: Path
    source: PaperSource
    metadata: Dict[str, Any] = field(default_factory=dict)
    downloaded: bool = False
    paper_id: str = ""
    paper_dir: Optional[Path] = None


def normalize_arxiv_id(arxiv_id: str) -> str:
    value = _ARXIV_PREFIX_RE.sub("", arxiv_id.strip())
    value = value.removesuffix(".pdf")
    value = value.strip("/")
    match = _NEW_ARXIV_ID_RE.search(value) or _OLD_ARXIV_ID_RE.search(value)
    if not match:
        raise ValueError(f"arXiv ID를 인식하지 못했습니다: {arxiv_id}")
    suffix = match.group(2) or ""
    return f"{match.group(1)}{suffix}"


def detect_paper_source(source: str) -> PaperSource:
    """Classify user-provided paper source.

    Supports:
    - https://arxiv.org/abs/2501.12345
    - https://arxiv.org/pdf/2501.12345.pdf
    - arXiv:2501.12345
    - 2501.12345
    - https://example.edu/paper.pdf
    - existing local job-relative path
    """
    raw = source.strip().strip("<>")
    if not raw:
        raise ValueError("paper source가 비어 있습니다.")

    prefixed = _ARXIV_PREFIX_RE.sub("", raw)
    parsed = urlparse(raw)

    if parsed.scheme in {"http", "https"}:
        host = parsed.netloc.lower().split("@")[-1].split(":")[0]
        path = unquote(parsed.path)
        if host.endswith("arxiv.org") or host.endswith("ar5iv.labs.arxiv.org"):
            arxiv_id = normalize_arxiv_id(path)
            return PaperSource(raw, "arxiv", arxiv_id=arxiv_id, url=arxiv_pdf_url(arxiv_id))
        if path.lower().endswith(".pdf"):
            return PaperSource(raw, "pdf_url", url=raw)
        raise ValueError("지원하는 논문 URL은 arXiv URL 또는 .pdf URL입니다.")

    if _NEW_ARXIV_ID_RE.search(prefixed) or _OLD_ARXIV_ID_RE.search(prefixed):
        arxiv_id = normalize_arxiv_id(prefixed)
        return PaperSource(raw, "arxiv", arxiv_id=arxiv_id, url=arxiv_pdf_url(arxiv_id))

    return PaperSource(raw, "local_relpath")


def arxiv_pdf_url(arxiv_id: str) -> str:
    normalized = normalize_arxiv_id(arxiv_id)
    return "https://arxiv.org/pdf/" + quote(normalized, safe="/") + ".pdf"


def _safe_url_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("PDF URL은 http/https만 지원합니다.")
    host = parsed.hostname or ""
    if host.lower() in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("localhost URL은 다운로드하지 않습니다.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        raise ValueError("private/internal IP PDF URL은 다운로드하지 않습니다.")


def fetch_arxiv_metadata(
    arxiv_id: str,
    session: requests.Session,
    cfg: FridayConfig,
) -> Dict[str, Any]:
    """Fetch arXiv metadata via the official Atom API."""
    normalized = normalize_arxiv_id(arxiv_id)
    response = session.get(
        ARXIV_API_URL,
        params={"id_list": normalized, "max_results": 1},
        timeout=cfg.ollama_connect_timeout + cfg.http_max_retries * 10,
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    entry = root.find("atom:entry", ns)
    if entry is None:
        return {"arxiv_id": normalized}

    def text(path: str) -> str:
        elem = entry.find(path, ns)
        return " ".join((elem.text or "").split()) if elem is not None else ""

    authors = [
        " ".join((author.findtext("atom:name", default="", namespaces=ns) or "").split())
        for author in entry.findall("atom:author", ns)
    ]
    links = []
    for link in entry.findall("atom:link", ns):
        href = link.attrib.get("href", "")
        if href:
            links.append({
                "href": href,
                "rel": link.attrib.get("rel", ""),
                "type": link.attrib.get("type", ""),
                "title": link.attrib.get("title", ""),
            })
    primary = entry.find("arxiv:primary_category", ns)
    return {
        "arxiv_id": normalized,
        "title": text("atom:title"),
        "authors": [a for a in authors if a],
        "summary": text("atom:summary"),
        "published": text("atom:published"),
        "updated": text("atom:updated"),
        "doi": text("arxiv:doi"),
        "journal_ref": text("arxiv:journal_ref"),
        "comment": text("arxiv:comment"),
        "primary_category": primary.attrib.get("term", "") if primary is not None else "",
        "links": links,
    }


def _filename_for_source(source: PaperSource, metadata: Dict[str, Any]) -> str:
    if source.kind == "arxiv":
        arxiv_part = source.arxiv_id.replace("/", "-")
        title = slugify(str(metadata.get("title") or "paper"))[:60]
        return f"arxiv-{arxiv_part}-{title}.pdf"

    parsed = urlparse(source.url)
    name = Path(unquote(parsed.path)).name
    if name.lower().endswith(".pdf"):
        stem = slugify(Path(name).stem)[:80]
        return f"{stem}.pdf"
    return "paper.pdf"


def _paper_id_for_source(source: PaperSource, metadata: Dict[str, Any], fallback_name: str = "paper") -> str:
    if source.kind == "arxiv" and source.arxiv_id:
        title = slugify(str(metadata.get("title") or "paper"))[:60]
        return f"arxiv-{source.arxiv_id.replace('/', '-')}-{title}"
    title = metadata.get("title") or Path(fallback_name).stem or "paper"
    return slugify(str(title))[:90] or "paper"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for idx in range(2, 100):
        candidate = parent / f"{stem}-{idx}{suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError(f"사용 가능한 파일명을 찾지 못했습니다: {path.name}")


def _unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    parent = path.parent
    stem = path.name
    for idx in range(2, 100):
        candidate = parent / f"{stem}-{idx}"
        if not candidate.exists():
            return candidate
    raise ValueError(f"사용 가능한 폴더명을 찾지 못했습니다: {path.name}")


def _download_pdf(
    url: str,
    dest: Path,
    session: requests.Session,
    cfg: FridayConfig,
    logger,
) -> Path:
    _safe_url_host(url)
    max_bytes = cfg.max_pdf_bytes
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    with session.get(url, stream=True, timeout=cfg.ollama_timeout) as response:
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError(f"PDF가 너무 큽니다: {int(content_length):,} bytes")
        content_type = response.headers.get("content-type", "")
        if "pdf" not in content_type.lower() and not urlparse(url).path.lower().endswith(".pdf"):
            logger.warning("PDF content-type is unusual: %s", content_type)

        written = 0
        try:
            with tmp.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(f"PDF가 너무 큽니다: {written:,} bytes")
                    fh.write(chunk)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    with tmp.open("rb") as fh:
        header = fh.read(5)
    if header != b"%PDF-":
        tmp.unlink(missing_ok=True)
        raise ValueError("다운로드한 파일이 PDF로 보이지 않습니다.")

    tmp.replace(dest)
    return dest


def _resolve_local_source_path(source: str, cfg: FridayConfig, current_job: Optional[str]) -> Path:
    from friday.paths import ensure_within, resolve_path

    raw = source.strip()
    path = Path(raw).expanduser()
    if path.is_absolute():
        return require_read(path, cfg, context="paper source local")
    if current_job:
        job_base = cfg.workspace_dir / current_job
        candidate = ensure_within(job_base / raw, job_base)
        return require_read(candidate, cfg, context="paper source job file")
    candidate = resolve_path(raw)
    return require_read(candidate, cfg, context="paper source local")


def resolve_paper_source_to_library(
    source_text: str,
    cfg: FridayConfig,
    session: requests.Session,
    logger,
    current_job: Optional[str] = None,
) -> PaperSourceResult:
    """Resolve source into FridayLocal/library/papers/<paper-id>/.

    The paper library is the durable build-up store. Individual jobs may still
    reference papers, but the canonical copy and review artifacts live here.
    """
    source = detect_paper_source(source_text)
    metadata: Dict[str, Any] = {}

    if source.kind == "local_relpath":
        src = _resolve_local_source_path(source.source, cfg, current_job)
        paper_id = _paper_id_for_source(source, metadata, fallback_name=src.name)
        paper_dir = _unique_dir(cfg.paper_library_dir / paper_id)
        suffix = src.suffix.lower() or ".txt"
        filename = "paper.pdf" if suffix == ".pdf" else f"paper{suffix}"
        dest = paper_dir / filename
        require_library_write(dest, cfg, context="paper library local copy")
        paper_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest, follow_symlinks=False)
        return PaperSourceResult(
            relpath=str(dest.relative_to(cfg.base_dir)),
            path=dest,
            source=source,
            metadata=metadata,
            downloaded=False,
            paper_id=paper_dir.name,
            paper_dir=paper_dir,
        )

    if source.kind == "arxiv":
        try:
            metadata = fetch_arxiv_metadata(source.arxiv_id, session, cfg)
        except Exception as exc:
            logger.warning("arXiv metadata fetch failed for %s: %s", source.arxiv_id, exc)
            metadata = {"arxiv_id": source.arxiv_id}

    fallback = _filename_for_source(source, metadata)
    paper_id = _paper_id_for_source(source, metadata, fallback_name=fallback)
    paper_dir = cfg.paper_library_dir / paper_id
    if paper_dir.exists() and (paper_dir / "paper.pdf").exists():
        return PaperSourceResult(
            relpath=str((paper_dir / "paper.pdf").relative_to(cfg.base_dir)),
            path=paper_dir / "paper.pdf",
            source=source,
            metadata=metadata,
            downloaded=False,
            paper_id=paper_dir.name,
            paper_dir=paper_dir,
        )

    paper_dir = _unique_dir(paper_dir)
    dest = paper_dir / "paper.pdf"
    require_library_write(dest, cfg, context="paper library download")
    paper_dir.mkdir(parents=True, exist_ok=True)
    downloaded = _download_pdf(source.url, dest, session, cfg, logger)
    return PaperSourceResult(
        relpath=str(downloaded.relative_to(cfg.base_dir)),
        path=downloaded,
        source=source,
        metadata=metadata,
        downloaded=True,
        paper_id=paper_dir.name,
        paper_dir=paper_dir,
    )


def resolve_paper_source_to_job(
    source_text: str,
    job_id: str,
    cfg: FridayConfig,
    session: requests.Session,
    logger,
) -> PaperSourceResult:
    """Resolve source to a PDF/text file inside the current job."""
    from friday.paths import ensure_within

    source = detect_paper_source(source_text)
    job_base = cfg.workspace_dir / job_id

    if source.kind == "local_relpath":
        path = ensure_within(job_base / source.source, job_base)
        return PaperSourceResult(
            relpath=str(path.relative_to(job_base)),
            path=path,
            source=source,
            metadata={},
            downloaded=False,
        )

    metadata: Dict[str, Any] = {}
    if source.kind == "arxiv":
        try:
            metadata = fetch_arxiv_metadata(source.arxiv_id, session, cfg)
        except Exception as exc:
            logger.warning("arXiv metadata fetch failed for %s: %s", source.arxiv_id, exc)
            metadata = {"arxiv_id": source.arxiv_id}

    filename = _filename_for_source(source, metadata)
    dest = _unique_path(job_base / filename)
    require_write(dest, job_id, cfg, context="paper source download")
    downloaded = _download_pdf(source.url, dest, session, cfg, logger)
    return PaperSourceResult(
        relpath=str(downloaded.relative_to(job_base)),
        path=downloaded,
        source=source,
        metadata=metadata,
        downloaded=True,
    )
