"""Compressed per-paper memory for build-up."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import requests

from buildup.config import BuildupConfig
from buildup.ollama import chat
from buildup.paper_library import PaperLibraryEntry
from buildup.sandbox import require_library_write

SOURCE_MAX_CHARS = 28_000


def _read_text(path: Path, *, max_chars: int) -> str:
    if not path.exists() or not path.is_file() or path.is_symlink():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...(truncated)"


def _read_json_excerpt(path: Path, *, max_chars: int) -> str:
    if not path.exists() or not path.is_file() or path.is_symlink():
        return ""
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _read_text(path, max_chars=max_chars)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...(truncated)"


def collect_memory_sources(entry: PaperLibraryEntry) -> str:
    """Collect bounded source material for refreshing memory.md."""
    sources: List[tuple[str, str]] = [
        ("metadata.json", _read_json_excerpt(entry.path / "metadata.json", max_chars=4_000)),
        ("summary.md", _read_text(entry.path / "summary.md", max_chars=7_000)),
        ("review.md", _read_text(entry.path / "review.md", max_chars=9_000)),
        ("evidence.json", _read_json_excerpt(entry.path / "evidence.json", max_chars=8_000)),
        ("notes.md", _read_text(entry.path / "notes.md", max_chars=7_000)),
        ("existing memory.md", _read_text(entry.path / "memory.md", max_chars=5_000)),
    ]
    blocks: List[str] = []
    total = 0
    for name, text in sources:
        if not text:
            continue
        block = f"\n\n## {name}\n\n{text}"
        if total + len(block) > SOURCE_MAX_CHARS:
            remain = SOURCE_MAX_CHARS - total
            if remain > 500:
                blocks.append(block[:remain].rstrip() + "\n...(source budget exhausted)")
            break
        blocks.append(block)
        total += len(block)
    return "".join(blocks).strip()


def refresh_paper_memory(
    entry: PaperLibraryEntry,
    cfg: BuildupConfig,
    session: requests.Session,
    logger,
    *,
    reason: str = "",
) -> Path:
    """Rewrite memory.md as a compact working memory for the selected paper."""
    sources = collect_memory_sources(entry)
    if not sources:
        raise ValueError("압축할 논문 자료가 없습니다. 먼저 논문을 읽거나 review/evidence/notes를 생성하세요.")

    prompt = f"""\
You are build-up, a careful senior research assistant.

Create or refresh a compact Korean working memory for this paper.
This file will be injected into future context windows, so keep it dense,
factual, and useful. Do not invent facts absent from the sources.

Length target: 1200-2200 Korean characters, hard maximum 3000 characters.

Required structure:
# Paper Memory
- Title / id
- Current reading status

## One-line Verdict

## Core Problem

## Main Contributions

## Method Reconstruction

## Evidence And Numbers

## Limitations / Doubts

## User Notes Integrated

## Next Questions

Refresh reason: {reason or "regular compression"}

Sources:
{sources}
"""
    memory = chat(
        session, cfg, cfg.research_model,
        [
            {
                "role": "system",
                "content": "Return only the memory.md Markdown. Be concise, skeptical, and citation-aware.",
            },
            {"role": "user", "content": prompt},
        ],
        keep_alive="10m",
        logger=logger,
        think=False,
        display_thinking=True,
    ).strip()
    if not memory:
        raise ValueError("메모리 압축 결과가 비어 있습니다.")

    target = entry.path / "memory.md"
    require_library_write(target, cfg, context="refresh_paper_memory")
    target.write_text(memory + "\n", encoding="utf-8")
    return target
