"""Paper library listing and compact context for build-up."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from buildup.config import BuildupConfig

MEMORY_CONTEXT_MAX_CHARS = 3_500


@dataclass(frozen=True)
class PaperLibraryEntry:
    paper_id: str
    path: Path
    title: str
    authors: str = ""
    venue: str = ""
    year: str = ""
    status: str = "unread"
    updated_at: float = 0.0
    has_pdf: bool = False
    has_review: bool = False
    has_evidence: bool = False

    def relpath(self, cfg: BuildupConfig) -> str:
        try:
            return str(self.path.relative_to(cfg.base_dir))
        except ValueError:
            return str(self.path)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _mtime(paths: Iterable[Path]) -> float:
    times: List[float] = []
    for path in paths:
        try:
            if path.exists():
                times.append(path.stat().st_mtime)
        except OSError:
            pass
    return max(times) if times else 0.0


def _entry_status(meta_status: str, has_review: bool, has_evidence: bool) -> str:
    if has_review:
        return "reviewed"
    if has_evidence:
        return "reading"
    if meta_status and meta_status not in {"reviewed", "done"}:
        return meta_status
    return "unread"


def list_paper_library(cfg: BuildupConfig) -> List[PaperLibraryEntry]:
    """Return paper folders sorted by most recently touched first."""
    root = cfg.paper_library_dir
    if not root.exists():
        return []

    entries: List[PaperLibraryEntry] = []
    for paper_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        metadata = _read_json(paper_dir / "metadata.json")
        has_pdf = (paper_dir / "paper.pdf").exists()
        has_review = (paper_dir / "review.md").exists()
        has_evidence = (paper_dir / "evidence.json").exists()
        updated = _mtime([
            paper_dir / "review.md",
            paper_dir / "evidence.json",
            paper_dir / "notes.md",
            paper_dir / "translation-ko.md",
            paper_dir / "summary.md",
            paper_dir / "metadata.json",
            paper_dir / "paper.pdf",
        ])
        title = str(metadata.get("title") or paper_dir.name)
        entries.append(PaperLibraryEntry(
            paper_id=paper_dir.name,
            path=paper_dir,
            title=title,
            authors=str(metadata.get("authors") or ""),
            venue=str(metadata.get("venue") or ""),
            year=str(metadata.get("year") or ""),
            status=_entry_status(str(metadata.get("status") or ""), has_review, has_evidence),
            updated_at=updated,
            has_pdf=has_pdf,
            has_review=has_review,
            has_evidence=has_evidence,
        ))

    entries.sort(key=lambda item: item.updated_at, reverse=True)
    return entries


def recent_paper_shelf(
    cfg: BuildupConfig,
    *,
    limit: int = 8,
    pending_first: bool = True,
) -> List[PaperLibraryEntry]:
    entries = list_paper_library(cfg)
    if not pending_first:
        return entries[:limit]
    pending = [entry for entry in entries if entry.status != "reviewed"]
    reviewed = [entry for entry in entries if entry.status == "reviewed"]
    return (pending + reviewed)[:limit]


def find_paper_entry(
    cfg: BuildupConfig,
    identifier: str,
    *,
    shelf_limit: int = 50,
) -> Optional[PaperLibraryEntry]:
    """Find a paper by shelf number, exact id, path name, or title substring."""
    needle = identifier.strip()
    if not needle:
        return None

    shelf = recent_paper_shelf(cfg, limit=shelf_limit, pending_first=True)
    if needle.isdigit():
        idx = int(needle)
        if 1 <= idx <= len(shelf):
            return shelf[idx - 1]

    low = needle.lower()
    entries = list_paper_library(cfg)
    for entry in entries:
        if low in {entry.paper_id.lower(), entry.path.name.lower()}:
            return entry
    for entry in entries:
        if low in entry.paper_id.lower() or low in entry.title.lower():
            return entry
    return None


def active_paper_context(cfg: BuildupConfig, paper_id: Optional[str]) -> str:
    """Compact context block for the selected paper."""
    if not paper_id:
        return "[build-up active paper]\nNo active paper selected."
    entry = find_paper_entry(cfg, paper_id)
    if entry is None:
        return f"[build-up active paper]\nSelected paper id not found: {paper_id}"
    lines = [
        "[build-up active paper]",
        f"id: {entry.paper_id}",
        f"title: {entry.title}",
        f"status: {entry.status}",
        f"path: {entry.relpath(cfg)}",
        f"paper_json: {entry.relpath(cfg)}/paper.paper.json",
        f"review: {entry.relpath(cfg)}/review.md",
        f"evidence: {entry.relpath(cfg)}/evidence.json",
        f"memory: {entry.relpath(cfg)}/memory.md",
        f"translation: {entry.relpath(cfg)}/translation-ko.md",
    ]
    memory_path = entry.path / "memory.md"
    if memory_path.exists() and memory_path.is_file() and not memory_path.is_symlink():
        try:
            memory = memory_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            memory = ""
        if memory:
            if len(memory) > MEMORY_CONTEXT_MAX_CHARS:
                memory = memory[:MEMORY_CONTEXT_MAX_CHARS].rstrip() + "\n...(compressed memory truncated)"
            lines.extend(["", "[build-up compressed paper memory]", memory])
    return "\n".join(lines)


def format_active_paper(cfg: BuildupConfig, paper_id: str) -> str:
    entry = find_paper_entry(cfg, paper_id)
    if entry is None:
        return f"선택된 논문을 찾지 못했어: {paper_id}"
    lines = [
        f"active paper: {entry.title}",
        f"id: {entry.paper_id}",
        f"status: {entry.status}",
        f"path: {entry.relpath(cfg)}",
    ]
    if entry.venue or entry.year:
        lines.append(f"venue: {' '.join(x for x in (entry.venue, entry.year) if x)}")
    return "\n".join(lines)


def format_paper_shelf(
    cfg: BuildupConfig,
    *,
    limit: int = 8,
    pending_first: bool = True,
) -> str:
    entries = recent_paper_shelf(cfg, limit=limit, pending_first=pending_first)
    if not entries:
        return "논문 라이브러리가 아직 비어 있어. arXiv 링크나 PDF를 주면 여기부터 쌓을게."

    lines = [
        "최근 논문 선반",
        "정렬: 미완료/읽는 중 먼저, 그 다음 최근 리뷰 순",
        "선택: `1번` 또는 `/paper use 1`",
        "",
    ]
    for idx, entry in enumerate(entries, start=1):
        stamp = "-"
        if entry.updated_at:
            stamp = datetime.fromtimestamp(entry.updated_at).strftime("%Y-%m-%d %H:%M")
        venue = " ".join(x for x in (entry.venue, entry.year) if x).strip()
        suffix = f" | {venue}" if venue else ""
        lines.append(f"{idx}. [{entry.status}] {entry.title}{suffix}")
        lines.append(f"   id: {entry.paper_id}")
        lines.append(f"   path: {entry.relpath(cfg)}")
        lines.append(f"   updated: {stamp}")
    return "\n".join(lines)


def build_paper_shelf_context(cfg: BuildupConfig, *, limit: int = 8) -> str:
    """Compact metadata-only context for build-up."""
    entries = recent_paper_shelf(cfg, limit=limit, pending_first=True)
    if not entries:
        return "[build-up paper shelf]\nNo papers in library yet."

    lines = [
        "[build-up paper shelf]",
        "Metadata-only list. Do not assume paper contents from this list; load review.md, evidence.json, or paper.paper.json when needed.",
    ]
    for entry in entries:
        lines.append(
            f"- id={entry.paper_id}; status={entry.status}; title={entry.title}; path={entry.relpath(cfg)}"
        )
    return "\n".join(lines)
