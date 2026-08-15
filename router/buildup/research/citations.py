"""Deterministic evidence validation and citation integrity checks."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import unquote

from .models import Claim, Evidence, Source


EVIDENCE_CITATION_RE = re.compile(r"\[(E[A-F0-9]{8,20})\]")
MARKDOWN_URL_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)", re.I)
BARE_URL_RE = re.compile(r"(?<!\()\bhttps?://[^\s)>]+", re.I)


@dataclass(frozen=True)
class PassageMatch:
    valid: bool
    normalized_passage: str
    normalized_offset: int = -1
    reason: str = ""


def normalize_text(value: str) -> str:
    """Normalize text without performing semantic or fuzzy matching."""
    clean = html.unescape(value or "").replace("\x00", " ")
    clean = unicodedata.normalize("NFKC", clean)
    return re.sub(r"\s+", " ", clean).strip()


def verify_exact_passage(source_content: str, passage: str, *, minimum_chars: int = 16) -> PassageMatch:
    """Accept a quote only when its normalized text is a literal source substring."""
    needle = normalize_text(passage)
    haystack = normalize_text(source_content)
    if len(needle) < minimum_chars:
        return PassageMatch(False, needle, reason=f"passage too short ({len(needle)} < {minimum_chars})")
    if not haystack:
        return PassageMatch(False, needle, reason="source content is empty")
    offset = haystack.find(needle)
    if offset < 0:
        return PassageMatch(False, needle, reason="passage is not an exact normalized source substring")
    return PassageMatch(True, needle, normalized_offset=offset)


def passage_sha256(passage: str) -> str:
    return hashlib.sha256(normalize_text(passage).encode("utf-8")).hexdigest()


def audit_report(
    report: str,
    evidence: Sequence[Evidence],
    sources: Sequence[Source],
    claims: Sequence[Claim],
    *,
    used_claim_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    evidence_by_id = {item.id: item for item in evidence}
    source_by_id = {item.id: item for item in sources}
    claim_by_id = {item.id: item for item in claims}
    cited = EVIDENCE_CITATION_RE.findall(report or "")
    cited_unique = _unique(cited)
    unknown = [item for item in cited_unique if item not in evidence_by_id]
    invalid = [
        item for item in cited_unique
        if item in evidence_by_id and not evidence_by_id[item].verified_exact
    ]

    cited_source_ids = _unique(
        evidence_by_id[item].source_id
        for item in cited_unique
        if item in evidence_by_id and evidence_by_id[item].source_id in source_by_id
    )
    paragraphs = _factual_paragraphs(report)
    cited_paragraphs = sum(1 for paragraph in paragraphs if EVIDENCE_CITATION_RE.search(paragraph))
    placement = round(cited_paragraphs / len(paragraphs), 3) if paragraphs else 1.0

    allowed_urls = {
        _canonical_report_url(url)
        for item in sources
        for url in (item.url, item.normalized_url)
        if url
    }
    report_urls = _unique(MARKDOWN_URL_RE.findall(report or "") + BARE_URL_RE.findall(report or ""))
    unsafe_urls = [url for url in report_urls if _canonical_report_url(url) not in allowed_urls]

    used_claims = _unique(used_claim_ids)
    unknown_claim_ids = [item for item in used_claims if item not in claim_by_id]
    unsupported_claim_ids = [
        item for item in used_claims
        if item in claim_by_id and not _claim_has_valid_support(claim_by_id[item], evidence_by_id)
    ]
    uncited_claim_ids = [
        item
        for item in used_claims
        if item in claim_by_id
        and not set(claim_by_id[item].evidence_ids).intersection(cited_unique)
    ]
    critical_claims = [item for item in claims if item.critical]
    unsupported_critical = [
        item.id for item in critical_claims if not _claim_has_valid_support(item, evidence_by_id)
    ]
    omitted_critical = [item.id for item in critical_claims if item.id not in used_claims]
    uncited_evidence = [item.id for item in evidence if item.id not in cited_unique]
    issues: List[str] = []
    if not cited_unique:
        issues.append("report contains no evidence citations")
    if unknown:
        issues.append("report cites unknown evidence IDs")
    if invalid:
        issues.append("report cites evidence that did not pass exact-passage validation")
    if unsafe_urls:
        issues.append("report contains URLs outside the source registry")
    if unknown_claim_ids:
        issues.append("writer declared unknown claim IDs")
    if unsupported_claim_ids:
        issues.append("writer used claims without valid supporting evidence")
    if uncited_claim_ids:
        issues.append("writer used claims without citing their supporting evidence")
    if unsupported_critical:
        issues.append("critical claims lack valid supporting evidence")
    if omitted_critical:
        issues.append("writer omitted critical claims from the report")
    if placement < 0.6:
        issues.append("too many factual paragraphs have no nearby evidence citation")

    passed = not issues
    return {
        "passed": passed,
        "issues": issues,
        "citation_count": len(cited),
        "cited_evidence_ids": cited_unique,
        "unknown_evidence_ids": unknown,
        "invalid_evidence_ids": invalid,
        "uncited_evidence_ids": uncited_evidence,
        "cited_source_ids": cited_source_ids,
        "uncited_source_ids": [item.id for item in sources if item.id not in cited_source_ids],
        "unsafe_urls": unsafe_urls,
        "factual_paragraphs": len(paragraphs),
        "cited_factual_paragraphs": cited_paragraphs,
        "citation_placement_coverage": placement,
        "used_claim_ids": used_claims,
        "unknown_claim_ids": unknown_claim_ids,
        "unsupported_claim_ids": unsupported_claim_ids,
        "uncited_claim_ids": uncited_claim_ids,
        "unsupported_critical_claim_ids": unsupported_critical,
        "omitted_critical_claim_ids": omitted_critical,
        "source_utilization": round(len(cited_source_ids) / len(sources), 3) if sources else 0.0,
    }


def append_used_sources(
    report: str,
    audit: Dict[str, Any],
    evidence: Sequence[Evidence],
    sources: Sequence[Source],
) -> str:
    """Append an authoritative registry-derived bibliography, never model URLs."""
    evidence_by_id = {item.id: item for item in evidence}
    sources_by_id = {item.id: item for item in sources}
    source_evidence: Dict[str, List[str]] = {}
    for evidence_id in audit.get("cited_evidence_ids", []):
        item = evidence_by_id.get(str(evidence_id))
        if item:
            source_evidence.setdefault(item.source_id, []).append(item.id)

    lines = [report.rstrip(), "", "## Sources", ""]
    for source_id in audit.get("cited_source_ids", []):
        source = sources_by_id.get(str(source_id))
        if not source:
            continue
        title = source.title.replace("[", "\\[").replace("]", "\\]").replace("\n", " ")
        evidence_ids = ", ".join(f"[{item}]" for item in source_evidence.get(source.id, []))
        details = f" — {evidence_ids}" if evidence_ids else ""
        lines.append(f"- [{source.id}] [{title}]({_markdown_url(source.url)}){details}")
    return "\n".join(lines).rstrip() + "\n"


def evidence_packet(
    evidence: Sequence[Evidence],
    sources: Sequence[Source],
    *,
    max_chars: int,
) -> str:
    """Render a bounded, XML-delimited evidence packet for a model."""
    source_by_id = {item.id: item for item in sources}
    blocks: List[str] = []
    used = 0
    for item in evidence:
        source = source_by_id.get(item.source_id)
        if not source:
            continue
        safe_title = _xml_safe(source.title)
        safe_passage = _xml_safe(item.passage)
        block = (
            f'<evidence id="{item.id}" source_id="{source.id}" stance="{item.stance}" '
            f'relevance="{item.relevance:.2f}" credibility="{item.credibility:.2f}" '
            f'source_type="{_xml_safe(source.source_type)}">\n'
            f"Title: {safe_title}\n"
            f"Author: {_xml_safe(source.author)}\n"
            f"Published: {_xml_safe(source.published_at)}\n"
            f"Locator: {_xml_safe(item.locator)}\n"
            f"Claim hint: {_xml_safe(item.claim_hint)}\n"
            f"Exact passage: {safe_passage}\n"
            "</evidence>"
        )
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def _claim_has_valid_support(claim: Claim, evidence_by_id: Dict[str, Evidence]) -> bool:
    return any(
        evidence_id in evidence_by_id
        and evidence_by_id[evidence_id].verified_exact
        and evidence_by_id[evidence_id].stance == "supports"
        for evidence_id in claim.evidence_ids
    )


def _factual_paragraphs(report: str) -> List[str]:
    body = re.split(r"^##\s+Sources\s*$", report or "", maxsplit=1, flags=re.M | re.I)[0]
    paragraphs: List[str] = []
    in_code = False
    current: List[str] = []
    for raw_line in body.splitlines() + [""]:
        line = raw_line.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.startswith("#"):
            continue
        if not line:
            paragraph = " ".join(current).strip()
            current = []
            plain = EVIDENCE_CITATION_RE.sub("", paragraph).strip()
            if len(plain) >= 40 and re.search(r"[.!?。]|다\.?$", plain):
                paragraphs.append(paragraph)
            continue
        current.append(line)
    return paragraphs


def _unique(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _xml_safe(value: str) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _canonical_report_url(value: str) -> str:
    return unquote((value or "").strip().rstrip(".,;:").rstrip("/"))


def _markdown_url(value: str) -> str:
    return (value or "").replace(" ", "%20").replace("(", "%28").replace(")", "%29")
