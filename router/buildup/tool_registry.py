"""Typed tool registry for Build-up agent.

Each tool is registered with:
  - name
  - description
  - risk_level: "low" | "medium" | "high"
  - required_capability: matches permission.py capability categories
  - executor: callable (action, params, ctx) -> (result_str, new_last_file)

The agent loop calls execute_tool() instead of a bare if-else chain.
Adding a new tool = one registry entry, zero changes elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from buildup.config import BuildupConfig


# ── Context passed to every executor ─────────────────────────

@dataclass
class ToolContext:
    cfg: BuildupConfig
    session: requests.Session
    logger: Any           # logging.Logger
    mode: str
    last_file: Optional[str]
    active_paper_id: Optional[str] = None
    approved_capabilities: frozenset[str] = field(default_factory=frozenset)


ExecutorFn = Callable[
    [Dict[str, Any], ToolContext],
    Tuple[str, Optional[str]],          # (result_text, new_last_file)
]


# ── Tool definition ───────────────────────────────────────────

@dataclass
class ToolDef:
    name: str
    description: str
    risk_level: str              # "low" | "medium" | "high"
    required_capability: str
    executor: ExecutorFn


# ── Registry ─────────────────────────────────────────────────

_REGISTRY: Dict[str, ToolDef] = {}


def register(tool: ToolDef) -> ToolDef:
    _REGISTRY[tool.name] = tool
    return tool


def get_tool(name: str) -> Optional[ToolDef]:
    return _REGISTRY.get(name)


def all_tools() -> List[ToolDef]:
    return list(_REGISTRY.values())


def execute_tool(
    action: str,
    params: Dict[str, Any],
    ctx: ToolContext,
) -> Tuple[str, Optional[str]]:
    """Look up tool and execute it.  Raises KeyError for unknown tools."""
    tool = _REGISTRY.get(action)
    if tool is None:
        return f"[알 수 없는 도구: {action}]", None
    if (
        tool.required_capability.startswith("confirm-")
        and tool.required_capability not in ctx.approved_capabilities
    ):
        return (
            f"[차단] {action}에는 사용자의 명시적 승인이 필요합니다 "
            f"({tool.required_capability}).",
            None,
        )
    return tool.executor(params, ctx)


# ── Executor helpers ──────────────────────────────────────────

def _relpath(params: Dict[str, Any], ctx: ToolContext, key: str = "relpath") -> str:
    return params.get(key) or ctx.last_file or ""

def _current_job(ctx: ToolContext) -> str:
    from buildup.state import get_current_job
    return get_current_job(ctx.cfg, required=True)


def _resolve_paper_json_path(hint: str, ctx: ToolContext, job_base) -> Optional[Any]:
    from buildup.paths import ensure_within
    from buildup.paper_library import find_paper_entry
    hint = (hint or "").strip()
    if not hint:
        if ctx.active_paper_id:
            entry = find_paper_entry(ctx.cfg, ctx.active_paper_id)
            if entry:
                candidate = entry.path / "paper.paper.json"
                if candidate.exists():
                    return ensure_within(candidate, ctx.cfg.paper_library_dir)
        return None
    entry = find_paper_entry(ctx.cfg, hint)
    if entry:
        candidate = entry.path / "paper.paper.json"
        if candidate.exists():
            return ensure_within(candidate, ctx.cfg.paper_library_dir)
    candidates = []
    if hint.endswith(".paper.json") or hint.endswith("paper.paper.json"):
        candidates.extend([
            job_base / hint,
            ctx.cfg.base_dir / hint,
            ctx.cfg.paper_library_dir / hint,
        ])
    for candidate in candidates:
        try:
            if candidate.exists():
                try:
                    return ensure_within(candidate, job_base)
                except Exception:
                    return ensure_within(candidate, ctx.cfg.paper_library_dir)
        except Exception:
            continue
    return None


def _resolve_paper_dir(params: Dict[str, Any], ctx: ToolContext):
    from buildup.paths import ensure_within
    from buildup.paper_library import find_paper_entry
    paper = str(params.get("paper") or ctx.active_paper_id or "").strip()
    entry = find_paper_entry(ctx.cfg, paper) if paper else None
    if entry is None:
        raise ValueError("선택된 논문이 없습니다. research에서 /paper list 후 /paper use 1처럼 선택하세요.")
    return ensure_within(entry.path, ctx.cfg.paper_library_dir), entry


def _extract_text_between_markers(text: str, start_pattern: str, end_patterns: List[str]) -> str:
    """Extract text between a start marker and the first following end marker."""
    import re

    start = re.search(start_pattern, text, re.IGNORECASE | re.MULTILINE)
    if not start:
        return ""
    tail = text[start.end():]
    end_positions = [
        match.start()
        for pattern in end_patterns
        if (match := re.search(pattern, tail, re.IGNORECASE | re.MULTILINE))
    ]
    end = min(end_positions) if end_positions else min(len(tail), 6000)
    extracted = tail[:end].strip()
    extracted = re.sub(r"\n{3,}", "\n\n", extracted)
    return extracted


def _fallback_extract_section_from_paper_json(data: Dict[str, Any], section: str) -> Tuple[str, str]:
    """Best-effort section extraction from full_text/pages when parser missed it."""
    full_text = str(data.get("full_text") or "")
    pages = data.get("pages") or []
    first_pages = "\n".join(str(page.get("content") or "") for page in pages[:3])
    text = first_pages or full_text
    if section == "abstract":
        content = _extract_text_between_markers(
            text,
            r"^\s*abstract\s*$",
            [
                r"^\s*(?:1\s*)?introduction\s*$",
                r"^\s*1\s+introduction\s*$",
                r"^\s*keywords?\s*[:\n]",
            ],
        )
        if content:
            return "Abstract (fallback from PDF text)", content
    return "", ""


# ── Tool executors ────────────────────────────────────────────

def _exec_calendar_list(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.calendar_mgr import cal_list, format_events
    days = int(params.get("days", 7))
    events = cal_list(ctx.cfg, upcoming_days=days)
    return (format_events(events) if events else "(일정 없음)"), None


def _exec_list_files(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.jobs import cmd_files
    jid = _current_job(ctx)
    files = cmd_files(jid, ctx.cfg)
    return ("\n".join(files) if files else "(비어 있음)"), None


def _exec_read_file(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.jobs import cmd_read
    relpath = _relpath(params, ctx)
    jid = _current_job(ctx)
    content = cmd_read(jid, relpath, ctx.cfg)
    return content[:6000], relpath


def _exec_list_paper_files(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    base, entry = _resolve_paper_dir(params, ctx)
    lines = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or path.name.startswith("."):
            continue
        rel = path.relative_to(base)
        marker = "/" if path.is_dir() else ""
        lines.append(str(rel) + marker)
    return (
        f"paper: {entry.paper_id}\n"
        + ("\n".join(lines) if lines else "(비어 있음)")
    ), str((base / "paper.paper.json").relative_to(ctx.cfg.base_dir))


def _exec_read_paper_file(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    import json
    from buildup.paths import ensure_within
    base, entry = _resolve_paper_dir(params, ctx)
    section = str(params.get("section") or "").strip().lower()
    section_aliases = {
        "초록": "abstract",
        "abstract": "abstract",
        "서론": "introduction",
        "intro": "introduction",
        "introduction": "introduction",
        "방법론": "method",
        "방법": "method",
        "method": "method",
        "실험": "experiments",
        "experiment": "experiments",
        "experiments": "experiments",
        "결과": "results",
        "result": "results",
        "results": "results",
        "결론": "conclusion",
        "conclusion": "conclusion",
    }
    if section:
        requested_section = section_aliases.get(section, section)
        paper_json = base / "paper.paper.json"
        if not paper_json.is_file():
            return "[오류] 논문 파싱 데이터가 없습니다: paper.paper.json", None
        data = json.loads(paper_json.read_text(encoding="utf-8"))
        sections = data.get("sections") or []
        matched = next(
            (
                item for item in sections
                if str(item.get("key") or "").lower() == requested_section
                or requested_section in str(item.get("title") or "").lower()
            ),
            None,
        )
        if matched:
            title = str(matched.get("title") or requested_section)
            content = str(matched.get("content") or "").strip()
            return (
                f"paper: {entry.title}\n"
                f"id: {entry.paper_id}\n"
                f"section: {requested_section}\n"
                f"title: {title}\n\n"
                f"{content[:12000]}",
                str(paper_json.relative_to(ctx.cfg.base_dir)),
            )

        fallback_title, fallback_content = _fallback_extract_section_from_paper_json(data, requested_section)
        if fallback_content:
            return (
                f"paper: {entry.title}\n"
                f"id: {entry.paper_id}\n"
                f"section: {requested_section}\n"
                f"title: {fallback_title}\n"
                "source: PDF/full_text fallback\n\n"
                f"{fallback_content[:12000]}",
                str(paper_json.relative_to(ctx.cfg.base_dir)),
            )

        available = [
            f"{item.get('key')}:{item.get('title')}"
            for item in sections
            if item.get("key") or item.get("title")
        ]
        fallback = next(
            (
                item for item in sections
                if str(item.get("key") or "").lower() in {"introduction", "other"}
            ),
            None,
        )
        fallback_text = ""
        if fallback:
            fallback_text = (
                "\n\n[nearest available section]\n"
                f"section: {fallback.get('key')}\n"
                f"title: {fallback.get('title')}\n\n"
                f"{str(fallback.get('content') or '').strip()[:5000]}"
            )
        return (
            f"paper: {entry.title}\n"
            f"id: {entry.paper_id}\n"
            f"requested section: {requested_section}\n"
            "status: requested section was not found in parsed paper data.\n"
            f"available sections: {', '.join(available[:20]) if available else '(none)'}"
            f"{fallback_text}",
            str(paper_json.relative_to(ctx.cfg.base_dir)),
        )

    preferred_files = (
        "review.md",
        "summary.md",
        "memory.md",
        "evidence.json",
        "paper.paper.json",
        "metadata.json",
        "notes.md",
    )

    requested = str(params.get("relpath") or params.get("path") or "").strip()
    relpath = requested or next((name for name in preferred_files if (base / name).is_file()), "review.md")
    target = ensure_within(base / relpath, base)
    if not target.exists() or not target.is_file():
        fallback = next((name for name in preferred_files if name != relpath and (base / name).is_file()), "")
        if not fallback:
            available = [
                str(path.relative_to(base))
                for path in sorted(base.rglob("*"))
                if path.is_file() and not path.is_symlink() and not path.name.startswith(".")
            ]
            return (
                f"[오류] 논문 파일을 찾지 못했습니다: {relpath}\n"
                f"available: {', '.join(available) if available else '(없음)'}"
            ), None
        relpath = fallback
        target = ensure_within(base / relpath, base)
    if target.suffix.lower() == ".pdf":
        return "[오류] PDF 원문은 직접 읽지 않고 paper.paper.json/review.md/evidence.json을 사용하세요.", None
    text = target.read_text(encoding="utf-8", errors="replace")
    notice = ""
    if requested and requested != relpath:
        notice = f"requested file '{requested}' was not available; using '{relpath}' instead.\n\n"
    return (
        f"paper: {entry.title}\n"
        f"id: {entry.paper_id}\n"
        f"source: {relpath}\n\n"
        f"{notice}{text[:12000]}",
        str(target.relative_to(ctx.cfg.base_dir)),
    )


def _exec_append_paper_note(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from datetime import datetime
    from buildup.sandbox import require_library_write

    base, entry = _resolve_paper_dir(params, ctx)
    content = str(params.get("content") or "").strip()
    if not content:
        return "[오류] append_paper_note: content가 비어 있습니다.", None

    heading = str(params.get("heading") or "").strip()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = heading or f"Note {stamp}"
    target = base / "notes.md"
    require_library_write(target, ctx.cfg, context="append_paper_note")
    previous = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    separator = "" if not previous else ("\n" if previous.endswith("\n") else "\n\n")
    block = f"{separator}## {title}\n\n{content}\n"
    target.write_text(previous + block, encoding="utf-8")
    return (
        f"paper: {entry.paper_id}\nnotes.md에 메모를 추가했습니다.",
        str(target.relative_to(ctx.cfg.base_dir)),
    )


def _exec_refresh_paper_memory(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.paper_memory import refresh_paper_memory

    _base, entry = _resolve_paper_dir(params, ctx)
    reason = str(params.get("reason") or "").strip()
    target = refresh_paper_memory(entry, ctx.cfg, ctx.session, ctx.logger, reason=reason)
    return (
        f"paper: {entry.paper_id}\n"
        f"compressed memory refreshed: {target.relative_to(ctx.cfg.base_dir)}",
        str(target.relative_to(ctx.cfg.base_dir)),
    )


def _exec_write_file(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.jobs import cmd_write
    relpath = _relpath(params, ctx)
    content = params.get("content", "")
    jid = _current_job(ctx)
    path = cmd_write(jid, relpath, content, ctx.cfg)
    return f"파일 저장 완료: {path.name} ({len(content)}자)", relpath


def _exec_edit_file(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.jobs import cmd_edit
    relpath = _relpath(params, ctx)
    old_string = params.get("old_string", "")
    new_string = params.get("new_string", "")
    if not old_string:
        return "[오류] edit_file: old_string이 비어 있습니다.", None
    jid = _current_job(ctx)
    diff = cmd_edit(jid, relpath, old_string, new_string, ctx.cfg)
    return f"수정 완료: {relpath}\n\ndiff:\n{diff[:800]}", relpath


def _exec_rewrite_file(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.ollama import rewrite_file
    relpath = _relpath(params, ctx)
    instruction = params.get("instruction", "")
    jid = _current_job(ctx)
    out_path, diff = rewrite_file(jid, relpath, instruction, None, ctx.mode, ctx.session, ctx.cfg, ctx.logger)
    result = f"수정본 생성: {out_path.name}"
    if diff:
        result += f"\n\ndiff:\n{diff[:600]}"
    return result, out_path.name


def _exec_glob_files(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.paths import glob_job
    from buildup.sandbox import job_read_roots
    pattern = params.get("pattern", "**/*")
    jid = _current_job(ctx)
    roots = job_read_roots(jid, ctx.cfg)
    results = list(glob_job(roots[0], pattern))
    for root in roots[1:]:
        results.extend(f"{root}/{name}" for name in glob_job(root, pattern))
    return ("\n".join(results[:100]) if results else "(매칭 없음)"), None


def _exec_grep_files(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.paths import grep_job
    from buildup.sandbox import job_read_roots
    pattern = params.get("pattern", "")
    if not pattern:
        return "[오류] grep_files: pattern이 비어 있습니다.", None
    path_glob = params.get("path_glob", "**/*")
    case_insensitive = bool(params.get("case_insensitive", False))
    jid = _current_job(ctx)
    roots = job_read_roots(jid, ctx.cfg)
    results: list[str] = []
    try:
        for index, root in enumerate(roots):
            hits = grep_job(root, pattern, path_glob=path_glob, case_insensitive=case_insensitive)
            results.extend(hits if index == 0 else [f"{root}/{hit}" for hit in hits])
    except ValueError as exc:
        return f"[오류] grep_files: {exc}", None
    return ("\n".join(results[:80]) if results else "(매칭 없음)"), None


def _exec_run_shell(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.command_runner import UnsafeCommandError, run_command
    from buildup.jobs import append_action_log
    from buildup.state import get_current_job, job_dir
    command = params.get("command", "")
    if not command:
        return "[오류] run_shell: command가 비어 있습니다.", None
    jid = get_current_job(ctx.cfg, required=False)
    cwd = job_dir(jid, ctx.cfg) if jid else ctx.cfg.base_dir
    try:
        completed = run_command(command, cwd, timeout=60)
        output = completed.output
        lines = output.splitlines()
        if len(lines) > 150:
            lines = [f"(출력 {len(lines)}줄 중 마지막 150줄)"] + lines[-150:]
            output = "\n".join(lines)
        if jid:
            append_action_log(
                jid, ctx.cfg, "shell",
                f"exit={completed.returncode} cmd={command[:80]}",
            )
        return f"[exit {completed.returncode}]\n{output or '(출력 없음)'}", None
    except UnsafeCommandError as exc:
        return f"[차단] {exc}", None
    except Exception as exc:
        return f"[오류] {exc}", None


def _exec_git_run(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.git_mgr import git_run as _git_run, RequiresConfirmation
    from buildup.jobs import append_action_log
    from buildup.state import get_current_job, job_dir
    args = params.get("args", "")
    if not args:
        return "[오류] git_run: args가 비어 있습니다.", None
    jid = get_current_job(ctx.cfg, required=False)
    cwd = job_dir(jid, ctx.cfg) if jid else ctx.cfg.base_dir
    try:
        output, code = _git_run(args, cwd, confirm_destructive=False)
        if jid:
            append_action_log(jid, ctx.cfg, "git", f"exit={code} args={args[:80]}")
        return f"[exit {code}]\n{output or '(출력 없음)'}", None
    except RequiresConfirmation as exc:
        return f"[차단] {exc}\n에이전트는 파괴적 git 명령을 실행할 수 없습니다. /git으로 직접 실행하세요.", None
    except Exception as exc:
        return f"[오류] git: {exc}", None


def _exec_search(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.search import format_search_results, web_search
    from buildup.prompts import system_search_synthesis, system_main
    from buildup.ollama import chat
    query = params.get("query", "")
    results, engine = web_search(query, ctx.cfg, ctx.session, max_results=ctx.cfg.web_search_max_results, bilingual=True)
    if not results:
        return "(검색 결과 없음)", None
    formatted = format_search_results(results, engine)
    synthesis = system_search_synthesis(query, formatted)
    summary = chat(ctx.session, ctx.cfg, ctx.cfg.main_model,
                   [{"role": "system", "content": system_main()},
                    {"role": "user", "content": synthesis}],
                   keep_alive="10m", logger=ctx.logger, display_thinking=True)
    return summary[:4000], None


def _exec_deep_search(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    import re as _re
    from datetime import datetime
    from buildup.search import format_search_results, web_search
    from buildup.prompts import system_search_synthesis, system_main, deep_search_file_prompt
    from buildup.ollama import chat
    from buildup.jobs import append_action_log
    from buildup.sandbox import require_write
    from buildup.state import get_current_job, job_dir
    query = params.get("query", "")
    save_to_file = bool(params.get("save_to_file", False))
    results, engine = web_search(query, ctx.cfg, ctx.session, max_results=8, bilingual=True)
    if not results:
        return "(검색 결과 없음)", None
    formatted = format_search_results(results, engine)
    synthesis = system_search_synthesis(query, formatted)
    analysis = chat(ctx.session, ctx.cfg, ctx.cfg.main_model,
                    [{"role": "system", "content": system_main()},
                     {"role": "user", "content": synthesis}],
                    keep_alive="10m", logger=ctx.logger, display_thinking=True)
    saved_file = None
    if save_to_file:
        try:
            jid = get_current_job(ctx.cfg, required=True)
            sources_text = "\n".join(f"- [{i+1}] {r['title']}: {r['href']}" for i, r in enumerate(results))
            file_prompt = deep_search_file_prompt(title=query, content=analysis, sources=sources_text)
            doc_content = chat(ctx.session, ctx.cfg, ctx.cfg.fast_model,
                               [{"role": "system", "content": "주어진 내용을 깔끔한 Markdown 문서로 정리하라. 문서 내용만 반환하라."},
                                {"role": "user", "content": file_prompt}],
                               keep_alive="2m", logger=ctx.logger)
            safe_q = _re.sub(r"[^\w\s-]", "", query)[:30].strip().replace(" ", "-")
            fname = f"research-{safe_q}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
            fpath = job_dir(jid, ctx.cfg) / fname
            require_write(fpath, jid, ctx.cfg, context="agent deep_search")
            fpath.write_text(doc_content, encoding="utf-8")
            append_action_log(jid, ctx.cfg, "deep_search", f"{query} → {fname}")
            saved_file = fname
            analysis += f"\n\n파일 저장 완료: {fname}"
        except Exception as exc:
            analysis += f"\n\n(파일 저장 실패: {exc})"
    return analysis[:5000], saved_file


def _exec_trash_file(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.jobs import append_action_log
    from buildup.paths import ensure_within, move_to_trash
    from buildup.sandbox import require_write
    from buildup.state import job_dir
    relpath = _relpath(params, ctx)
    jid = _current_job(ctx)
    base = job_dir(jid, ctx.cfg)
    target = ensure_within(base / relpath, base)
    require_write(target, jid, ctx.cfg, context=f"agent trash {relpath}")
    moved = move_to_trash(target, jid, ctx.cfg)
    append_action_log(jid, ctx.cfg, "trash", relpath)
    return f"휴지통으로 이동: {moved}", None


def _exec_create_job(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.jobs import cmd_job_new
    label = params.get("label") or None
    jid, created = cmd_job_new(label, ctx.cfg)
    result = f"새 job 생성: {jid}"
    if created:
        result += "\n" + "\n".join(created)
    return result, None


def _exec_load_paper(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.paper import load_paper_from_path, summarize_paper, save_paper_json, LONG_PAPER_THRESHOLD
    from buildup.paths import ensure_within
    from buildup.sandbox import require_read, require_write
    from buildup.jobs import append_action_log
    from buildup.state import job_dir
    relpath = _relpath(params, ctx)
    jid = _current_job(ctx)
    base = job_dir(jid, ctx.cfg)
    path = ensure_within(base / relpath, base)
    require_read(path, ctx.cfg, context=f"load_paper {relpath}")
    paper = load_paper_from_path(path, ctx.cfg, ctx.session, ctx.logger)
    json_path = save_paper_json(paper, base)
    summary = summarize_paper(paper, ctx.cfg, ctx.session, ctx.logger)
    stem = path.stem[:50]
    summary_path = base / f"{stem}.summary.md"
    require_write(summary_path, jid, ctx.cfg, context="load_paper summary")
    summary_path.write_text(summary, encoding="utf-8")
    append_action_log(jid, ctx.cfg, "load_paper", relpath)
    long_note = f"\n({'섹션별 처리' if paper.total_chars() > LONG_PAPER_THRESHOLD else '전체 컨텍스트'})"
    return (
        f"논문 로드 완료: {paper.title}\n저자: {paper.authors}\n"
        f"섹션 {len(paper.sections)}개{long_note}\n\n"
        f"요약: {summary_path.name}\n데이터: {json_path.name}\n\n{summary[:3000]}"
    ), json_path.name


def _exec_load_paper_source(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from pathlib import Path
    from buildup.jobs import append_action_log
    from buildup.paper import load_paper_from_path, run_deep_paper_review
    from buildup.paper_source import resolve_paper_source_to_library
    from buildup.state import get_current_job

    source = params.get("source") or params.get("url") or params.get("arxiv_id") or params.get("path") or ""
    if not str(source).strip():
        return "[오류] load_paper_source: source가 비어 있습니다.", None
    reviewer_mode = bool(params.get("reviewer_mode", False))

    current_job = get_current_job(ctx.cfg, required=False)
    resolved = resolve_paper_source_to_library(
        str(source), ctx.cfg, ctx.session, ctx.logger, current_job=current_job,
    )
    paper = load_paper_from_path(
        resolved.path, ctx.cfg, ctx.session, ctx.logger,
        source_metadata=resolved.metadata,
    )
    outputs = run_deep_paper_review(
        paper, resolved.paper_dir, ctx.cfg, ctx.session, ctx.logger,
        source_metadata=resolved.metadata,
        reviewer_mode=reviewer_mode,
    )
    if current_job:
        append_action_log(
            current_job, ctx.cfg, "load_paper_source",
            f"{source} -> {resolved.paper_dir.relative_to(ctx.cfg.base_dir)}",
        )
    paper_json_rel = str((resolved.paper_dir / "paper.paper.json").relative_to(ctx.cfg.base_dir))
    review_rel = str((resolved.paper_dir / "review.md").relative_to(ctx.cfg.base_dir))
    evidence_rel = str((resolved.paper_dir / "evidence.json").relative_to(ctx.cfg.base_dir))
    return (
        f"논문 라이브러리 저장 완료: {resolved.paper_id}\n"
        f"제목: {paper.title}\n"
        f"저자: {paper.authors or '논문에서 명시하지 않음'}\n"
        f"페이지: {len(paper.pages) or 'N/A'} / 섹션: {len(paper.sections)}\n\n"
        f"모델: research={ctx.cfg.research_model}"
        f"{' / reviewer=' + ctx.cfg.reviewer_model if reviewer_mode else ''}\n"
        f"리뷰: {review_rel}\n"
        f"근거 장부: {evidence_rel}\n"
        f"데이터: {paper_json_rel}\n\n"
        f"{Path(outputs['review']).read_text(encoding='utf-8')[:3000]}"
    ), paper_json_rel


def _exec_paper_qa(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.paper import load_paper_json, find_paper_json, answer_question
    from buildup.state import job_dir
    question = params.get("question", "")
    paper_hint = params.get("paper", "") or (ctx.last_file if ctx.last_file and ctx.last_file.endswith(".paper.json") else "")
    jid = _current_job(ctx)
    base = job_dir(jid, ctx.cfg)
    paper_path = _resolve_paper_json_path(paper_hint, ctx, base) or find_paper_json(base)
    if paper_path is None or not paper_path.exists():
        return "[오류] 로드된 논문이 없습니다. 먼저 load_paper를 실행하세요.", None
    paper = load_paper_json(paper_path)
    return answer_question(paper, question, ctx.cfg, ctx.session, ctx.logger), paper_path.name


def _exec_explain_concept(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.paper import load_paper_json, find_paper_json, explain_concept as _explain
    from buildup.state import job_dir
    concept = params.get("concept", "")
    paper_hint = params.get("paper", "") or (ctx.last_file if ctx.last_file and ctx.last_file.endswith(".paper.json") else "")
    jid = _current_job(ctx)
    base = job_dir(jid, ctx.cfg)
    paper_path = _resolve_paper_json_path(paper_hint, ctx, base) or find_paper_json(base)
    if paper_path is None or not paper_path.exists():
        return "[오류] 로드된 논문이 없습니다. 먼저 load_paper를 실행하세요.", None
    paper = load_paper_json(paper_path)
    return _explain(paper, concept, ctx.cfg, ctx.session, ctx.logger), paper_path.name


def _exec_compare_papers(params: Dict[str, Any], ctx: ToolContext) -> Tuple[str, Optional[str]]:
    from buildup.paper import load_paper_json, compare_papers as _compare
    from buildup.state import job_dir
    paper_a_hint = params.get("paper_a", "")
    paper_b_hint = params.get("paper_b", "")
    aspect = params.get("aspect", "방법론 및 성능")
    jid = _current_job(ctx)
    base = job_dir(jid, ctx.cfg)
    all_jsons = sorted(base.glob("*.paper.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if len(all_jsons) < 2 and not (paper_a_hint and paper_b_hint):
        return "[오류] 비교할 논문이 2개 필요합니다.", None
    def _find(hint: str, idx: int):
        if hint:
            for p in all_jsons:
                if hint.lower() in p.name.lower():
                    return p
        return all_jsons[idx]
    paper_a = load_paper_json(_find(paper_a_hint, 1))
    paper_b = load_paper_json(_find(paper_b_hint, 0))
    return _compare(paper_a, paper_b, aspect, ctx.cfg, ctx.session, ctx.logger), None


# ── Register all tools ────────────────────────────────────────

register(ToolDef("calendar_list",  "앞으로 N일간 일정 목록 반환",         "low",    "safe-readonly",       _exec_calendar_list))
register(ToolDef("list_files",     "현재 job 파일 목록 반환",              "low",    "safe-readonly",       _exec_list_files))
register(ToolDef("read_file",      "현재 job 파일 읽기",                   "low",    "safe-local-read",     _exec_read_file))
register(ToolDef("list_paper_files", "선택한 논문 라이브러리 디렉토리 파일 목록 반환", "low", "safe-readonly", _exec_list_paper_files))
register(ToolDef("read_paper_file", "선택한 논문 디렉토리 파일 또는 section=abstract 같은 섹션 읽기", "low", "safe-readonly", _exec_read_paper_file))
register(ToolDef("append_paper_note", "선택한 논문 디렉토리의 notes.md에 메모 추가", "medium", "safe-local-edit", _exec_append_paper_note))
register(ToolDef("refresh_paper_memory", "선택한 논문의 memory.md를 압축 갱신", "medium", "safe-local-edit", _exec_refresh_paper_memory))
register(ToolDef("write_file",     "현재 job 파일 생성/덮어쓰기",          "medium", "safe-local-create",   _exec_write_file))
register(ToolDef("edit_file",      "파일에서 old→new 정밀 교체",           "medium", "safe-local-edit",     _exec_edit_file))
register(ToolDef("rewrite_file",   "파일 전체 내용 수정 (새 파일 생성)",   "medium", "safe-local-rewrite",  _exec_rewrite_file))
register(ToolDef("glob_files",     "glob 패턴으로 파일 검색",              "low",    "safe-readonly",       _exec_glob_files))
register(ToolDef("grep_files",     "파일 내용 정규식 검색",                "low",    "safe-readonly",       _exec_grep_files))
register(ToolDef("run_shell",      "승인 후 단일 허용 명령 실행",          "high",   "confirm-shell",       _exec_run_shell))
register(ToolDef("git_run",        "git 명령 실행 (파괴적 명령 차단)",     "medium", "confirm-git",         _exec_git_run))
register(ToolDef("search",         "웹 검색 + AI 합성",                    "low",    "safe-search",         _exec_search))
register(ToolDef("deep_search",    "심층 웹 검색 + 분석 + 파일 저장",     "medium", "safe-search",         _exec_deep_search))
register(ToolDef("trash_file",     "파일을 휴지통으로 이동",               "medium", "confirm-always",      _exec_trash_file))
register(ToolDef("create_job",     "새 job 폴더 생성",                     "low",    "safe-local-create",   _exec_create_job))
register(ToolDef("load_paper_source", "arXiv/PDF/로컬 논문을 라이브러리에 저장하고 로컬 딥리뷰 생성", "medium", "safe-local-create", _exec_load_paper_source))
register(ToolDef("load_paper",     "논문 파일 로드 + 섹션 파싱 + 요약",   "medium", "safe-local-create",   _exec_load_paper))
register(ToolDef("paper_qa",       "논문 Q&A (본문 인용 기반)",            "low",    "safe-readonly",       _exec_paper_qa))
register(ToolDef("explain_concept","논문 개념 설명",                       "low",    "safe-readonly",       _exec_explain_concept))
register(ToolDef("compare_papers", "두 논문 비교 분석",                    "low",    "safe-readonly",       _exec_compare_papers))
