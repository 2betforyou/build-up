"""Agent dispatcher for Build-up Tier-2.

Dispatch order:
  1. PlanStateMachine (State Machine + Constrained Planner) — default
  2. run_agent_legacy (original ReAct loop) — fallback on PlanningError
     or when BUILDUP_USE_STATE_MACHINE=0

Toggle:  export BUILDUP_USE_STATE_MACHINE=0   to force legacy mode.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from buildup.config import BuildupConfig
from buildup.prompts import bilingual_clause, system_main

AGENT_MAX_STEPS = 8

# ============================================================
# Tool registry
# ============================================================

TOOL_DESCRIPTIONS = """\
사용 가능한 도구:

calendar_list(days=7)
  앞으로 N일간 일정 목록을 반환한다.

read_file(relpath)
  현재 job의 파일을 읽어 내용을 반환한다.

list_paper_files(paper="")
  research에서 선택한 active paper 디렉토리의 파일 목록을 반환한다.
  paper를 지정하면 해당 paper id 또는 선반 번호를 사용한다.

read_paper_file(relpath="review.md", section="", paper="")
  research에서 선택한 active paper 디렉토리의 파일을 읽는다.
  review.md, evidence.json, notes.md, summary.md, paper.paper.json 확인에 사용한다.
  abstract/초록/서론/방법론/실험/결과/결론 같은 섹션 요청은 section을 사용한다.
  예: read_paper_file(section="abstract")

append_paper_note(content, heading="", paper="")
  research에서 선택한 active paper 디렉토리의 notes.md에 메모를 추가한다.
  사용자가 "이 논문 노트에 남겨줘", "읽은 내용 정리해둬"처럼 말하면 사용한다.

refresh_paper_memory(paper="", reason="")
  선택한 active paper의 review/evidence/notes/기존 memory를 압축해 memory.md를 갱신한다.
  사용자가 "메모리 압축", "이 논문 상태 압축", "다음에도 이어서 보게 정리"를 요청하면 사용한다.

write_file(relpath, content)
  현재 job에 파일을 생성하거나 덮어쓴다. content는 실제 파일에 저장될 텍스트다.

rewrite_file(relpath, instruction)
  현재 job의 기존 파일을 instruction에 따라 수정한다. 수정본은 .buildup.md로 저장된다.

list_files()
  현재 job의 파일 목록을 반환한다.

search(query)
  웹 검색을 수행하고 결과 요약을 반환한다.

deep_search(query, save_to_file=false)
  웹 검색 후 메인 모델로 심층 분석한다. save_to_file=true이면 결과를 파일로 저장한다.

trash_file(relpath)
  파일을 휴지통으로 이동한다.

create_job(label="")
  새 job 폴더를 만들고 현재 job으로 전환한다.

load_paper_source(source)
  arXiv URL/ID, 직접 PDF URL, 또는 로컬 PDF/text 경로를 받아 build-up 논문 라이브러리에 저장한다.
  저장 위치는 library/papers/<paper-id>/ 이며 paper.pdf, metadata.json, paper.paper.json,
  summary.md, evidence.json, notes.md, review.md를 생성한다.
  research에서 arXiv 링크나 PDF 링크를 받으면 이 도구를 우선 사용한다.

load_paper(relpath)
  PDF 또는 텍스트 논문 파일을 읽어 섹션을 분석하고 구조화된 요약을 생성한다.
  요약은 <name>.summary.md로, 파싱 데이터는 <name>.paper.json으로 저장된다.
  이후 paper_qa / explain_concept / compare_papers에서 이 논문을 참조한다.

paper_qa(question, paper="")
  로드된 논문에 대해 질문하고 본문 인용 기반으로 답변을 받는다.
  paper를 지정하면 해당 .paper.json을 우선 사용한다.
  정확성 최우선: 논문에 없는 내용은 절대 답하지 않는다.

explain_concept(concept, paper="")
  논문에서 특정 개념·기법·수식을 인용하고 쉽게 설명한다.
  논문 본문 인용 → 쉬운 설명 → 직관적 비유 순서로 제공한다.

compare_papers(paper_a, paper_b, aspect)
  두 .paper.json 파일을 특정 관점(방법론, 성능, 학습법 등)으로 비교한다.

edit_file(relpath, old_string, new_string)
  파일에서 old_string을 찾아 new_string으로 교체한다.
  old_string은 파일 내에서 유일해야 한다. 공백·줄바꿈 포함 정확히 일치해야 한다.
  전체 파일을 다시 쓰지 않고 최소 변경만 수행한다.
  결과로 diff를 반환한다.

glob_files(pattern)
  현재 job 폴더에서 glob 패턴으로 파일을 검색한다.
  예: "**/*.py", "src/**/*.ts", "*.md"
  매칭된 파일 경로 목록을 반환한다.

grep_files(pattern, path_glob="**/*", case_insensitive=false)
  현재 job 폴더의 파일 내용을 정규식으로 검색한다.
  "파일명:줄번호: 내용" 형식으로 반환한다.

run_shell(command)
  사용자 승인 후 허용된 단일 프로세스를 현재 job 폴더에서 실행한다.
  파이프, 리다이렉션, 명령 치환, 여러 명령 연결은 지원하지 않는다.
  Git 작업은 이 도구가 아니라 git_run을 사용한다.

git_run(args)
  사용자 승인 후 현재 job 폴더에서 제한된 git 명령을 실행한다.
  예: "status", "diff HEAD", "add README.md", "commit -m '메시지'", "log --oneline -10"
  alias/config 우회와 외부 helper는 차단되며, 파괴적 명령은 에이전트가 실행할 수 없다.

answer(content)
  사용자에게 최종 답변을 전달하고 에이전트 루프를 종료한다.
  반드시 모든 작업이 끝난 후 마지막으로 호출해야 한다.
"""

# ============================================================
# System prompt
# ============================================================

AGENT_SYSTEM_PROMPT = """\
당신은 로컬 파일, 일정, 논문 작업을 안전하게 처리하는 개인 비서 에이전트 research다.
너의 임무는 사용자 요청을 한 번에 하나의 도구 호출로 실행하고, 필요한 근거를 수집한 뒤 최종 답변으로 보고하는 것이다.

{date_ctx}
현재 job: {current_job}
현재 job 파일: {file_list}
다가오는 일정 (7일): {upcoming}
{job_prefs_block}{exemplar_context}{tool_descriptions}

매 응답은 반드시 아래 JSON 형식만 출력하라. 다른 텍스트는 절대 출력하지 마라.
{{
  "thought": "현재 상황 분석과 다음 행동의 이유를 한국어로 작성",
  "action": "도구_이름",
  "params": {{...}}
}}

행동 원칙:
1. 파일에 내용을 쓰기 전에 필요한 데이터(일정, 검색 결과, 논문 본문, 기존 파일 내용 등)를 먼저 수집하라.
2. "그 파일", "거기에", "방금 만든 파일" 등의 표현은 last_file 컨텍스트의 파일을 가리킨다.
2-1. research에서 active paper가 선택되어 있으면 논문 관련 후속 질문은 그 paper를 기본 대상으로 삼는다.
     논문 디렉토리 파일을 확인할 때는 list_paper_files/read_paper_file을 사용하고,
     abstract/초록 같은 섹션 요청은 read_paper_file(section="abstract")처럼 처리하며,
     read_paper_file 다음에 같은 자료를 read_file로 다시 읽지 않는다.
     사용자가 논문 노트에 남기길 원하면 append_paper_note를 사용한다.
     사용자가 메모리 압축/이어보기/상태 저장을 요청하면 refresh_paper_memory를 사용한다.
3. 사용자가 일정을 파일에 저장하길 원하면 calendar_list로 데이터를 먼저 가져온 후 write_file하라.
4. write_file의 content는 실제 파일에 저장될 텍스트여야 한다 — 요청이나 지시가 아니다.
5. 작업이 완료되면 반드시 answer로 결과를 보고하라.{bilingual_rule}
6. 확실하지 않은 내용을 지어내지 마라.
7. 논문 URL/arXiv/새 PDF 요청: load_paper_source를 우선 사용하라. 이미 job 안의 파일이면 load_paper도 가능하다.
8. 논문 질문 시 논문에 없는 내용을 지어내지 마라. "이 논문에서 명시하지 않습니다"라고 답하라.
9. 파일의 일부를 수정할 때는 rewrite_file 대신 edit_file을 사용하라. 더 빠르고 정확하다.
10. 코드 실행이나 테스트가 필요하면 run_shell을 사용하라.
11. 파일을 찾을 때는 glob_files, 내용을 검색할 때는 grep_files를 사용하라.
12. git 작업(커밋, 상태 확인 등)은 git_run을 사용하라.
"""

# ============================================================
# Context builder
# ============================================================

def _build_context(cfg: BuildupConfig) -> Dict[str, str]:
    from buildup.context import build_context
    ctx = build_context(cfg)
    ctx["tool_descriptions"] = TOOL_DESCRIPTIONS
    job_prefs = ctx.get("job_prefs", "")
    ctx["job_prefs_block"] = (
        f"\n[프로젝트 컨텍스트 (.buildup.md)]\n{job_prefs}\n"
        if job_prefs else ""
    )
    return ctx

# ============================================================
# JSON action parser
# ============================================================

def _parse_action(raw: str) -> Optional[Dict[str, Any]]:
    """Extract the agent's JSON action from LLM output."""
    text = raw.strip()

    # Strip markdown fences
    text = re.sub(r"^```\w*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find all outermost { ... } candidates and try each.
    # This handles models that wrap their JSON in <thought> or prose blocks.
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            return None
        depth = 0
        end = -1
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return None
        candidate = text[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "action" in parsed:
                return parsed
        except json.JSONDecodeError:
            # Fix trailing commas and retry
            fixed = re.sub(r",\s*}", "}", candidate)
            fixed = re.sub(r",\s*]", "]", fixed)
            try:
                parsed = json.loads(fixed)
                if isinstance(parsed, dict) and "action" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass
        pos = end + 1
    return None

# ============================================================
# Streaming helper for agent steps
# ============================================================

def _find_json_string_end(text: str) -> int:
    """Return index of first unescaped closing quote in a JSON string fragment, or -1."""
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return -1


def _stream_agent_step(
    session: "requests.Session",
    cfg: "BuildupConfig",
    messages: list,
    logger: "logging.Logger",
) -> str:
    """Stream one agent LLM step and render any model think block separately."""
    from buildup.ollama import chat_stream, render_thinking_block

    buf = ""
    for chunk in chat_stream(session, cfg, cfg.main_model, messages, "10m", logger):
        buf += chunk
    render_thinking_block(buf)
    return buf


# ============================================================
# Tool executor  (delegates to tool_registry)
# ============================================================

def _execute_tool(
    action: str,
    params: Dict[str, Any],
    cfg: BuildupConfig,
    session: requests.Session,
    logger: logging.Logger,
    mode: str,
    last_file: Optional[str],
    active_paper_id: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Execute a named tool via the typed tool registry."""
    from buildup.tool_registry import execute_tool, ToolContext
    ctx = ToolContext(
        cfg=cfg, session=session, logger=logger, mode=mode,
        last_file=last_file, active_paper_id=active_paper_id,
    )
    return execute_tool(action, params, ctx)


def _execute_tool_legacy(
    action: str,
    params: Dict[str, Any],
    cfg: BuildupConfig,
    session: requests.Session,
    logger: logging.Logger,
    mode: str,
    last_file: Optional[str],
) -> Tuple[str, Optional[str]]:
    """Legacy executor kept for reference — no longer called."""
    from buildup.calendar_mgr import cal_list, format_events
    from buildup.jobs import cmd_files, cmd_job_new, cmd_read, cmd_write, append_action_log
    from buildup.ollama import chat, rewrite_file
    from buildup.paths import ensure_within, move_to_trash
    from buildup.sandbox import require_write
    from buildup.search import format_search_results, web_search
    from buildup.state import get_current_job, job_dir

    def _current_job() -> str:
        return get_current_job(cfg, required=True)

    def _relpath(key: str = "relpath") -> str:
        return params.get(key) or last_file or ""

    if action == "calendar_list":
        days = int(params.get("days", 7))
        events = cal_list(cfg, upcoming_days=days)
        if not events:
            return "(일정 없음)", None
        return format_events(events), None

    if action == "list_files":
        jid = _current_job()
        files = cmd_files(jid, cfg)
        return "\n".join(files) if files else "(비어 있음)", None

    if action == "read_file":
        relpath = _relpath()
        jid = _current_job()
        content = cmd_read(jid, relpath, cfg)
        return content[:6000], relpath

    if action == "write_file":
        relpath = _relpath()
        content = params.get("content", "")
        jid = _current_job()
        path = cmd_write(jid, relpath, content, cfg)
        return f"파일 저장 완료: {path.name} ({len(content)}자)", relpath

    if action == "rewrite_file":
        relpath = _relpath()
        instruction = params.get("instruction", "")
        jid = _current_job()
        out_path, diff = rewrite_file(
            jid, relpath, instruction, None, mode, session, cfg, logger
        )
        result = f"수정본 생성: {out_path.name}"
        if diff:
            result += f"\n\ndiff:\n{diff[:600]}"
        return result, out_path.name

    if action == "search":
        query = params.get("query", "")
        results, engine = web_search(
            query, cfg, session, max_results=cfg.web_search_max_results, bilingual=True
        )
        if not results:
            return "(검색 결과 없음)", None
        formatted = format_search_results(results, engine)
        # Synthesize with main model
        from buildup.prompts import system_search_synthesis
        synthesis = system_search_synthesis(query, formatted)
        summary = chat(
            session, cfg, cfg.main_model,
            [{"role": "system", "content": system_main()},
             {"role": "user", "content": synthesis}],
            keep_alive="10m", logger=logger, display_thinking=True,
        )
        return summary[:4000], None

    if action == "deep_search":
        query = params.get("query", "")
        save_to_file = bool(params.get("save_to_file", False))
        results, engine = web_search(query, cfg, session, max_results=8, bilingual=True)
        if not results:
            return "(검색 결과 없음)", None
        formatted = format_search_results(results, engine)
        from buildup.prompts import system_search_synthesis, deep_search_file_prompt
        synthesis = system_search_synthesis(query, formatted)
        analysis = chat(
            session, cfg, cfg.main_model,
            [{"role": "system", "content": system_main()},
             {"role": "user", "content": synthesis}],
            keep_alive="10m", logger=logger, display_thinking=True,
        )
        saved_file = None
        if save_to_file:
            try:
                jid = get_current_job(cfg, required=True)
                sources_text = "\n".join(
                    f"- [{i+1}] {r['title']}: {r['href']}" for i, r in enumerate(results)
                )
                file_prompt = deep_search_file_prompt(
                    title=query, content=analysis, sources=sources_text
                )
                doc_content = chat(
                    session, cfg, cfg.fast_model,
                    [{"role": "system", "content": "주어진 내용을 깔끔한 Markdown 문서로 정리하라. 문서 내용만 반환하라."},
                     {"role": "user", "content": file_prompt}],
                    keep_alive="2m", logger=logger,
                )
                safe_q = re.sub(r"[^\w\s-]", "", query)[:30].strip().replace(" ", "-")
                fname = f"research-{safe_q}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
                fpath = job_dir(jid, cfg) / fname
                require_write(fpath, jid, cfg, context="agent deep_search")
                fpath.write_text(doc_content, encoding="utf-8")
                append_action_log(jid, cfg, "deep_search", f"{query} → {fname}")
                saved_file = fname
                analysis += f"\n\n파일 저장 완료: {fname}"
            except Exception as exc:
                analysis += f"\n\n(파일 저장 실패: {exc})"
        return analysis[:5000], saved_file

    if action == "trash_file":
        relpath = _relpath()
        jid = _current_job()
        base = job_dir(jid, cfg)
        target = ensure_within(base / relpath, base)
        require_write(target, jid, cfg, context=f"agent trash {relpath}")
        moved = move_to_trash(target, jid, cfg)
        append_action_log(jid, cfg, "trash", relpath)
        return f"휴지통으로 이동: {moved}", None

    if action == "create_job":
        label = params.get("label") or None
        jid, created = cmd_job_new(label, cfg)
        result = f"새 job 생성: {jid}"
        if created:
            result += "\n" + "\n".join(created)
        return result, None

    # ── Paper tools ──────────────────────────────────────────────────

    if action == "load_paper_source":
        from pathlib import Path
        from buildup.jobs import append_action_log
        from buildup.paper import load_paper_from_path, run_deep_paper_review
        from buildup.paper_source import resolve_paper_source_to_library

        source = params.get("source") or params.get("url") or params.get("arxiv_id") or params.get("path") or ""
        if not str(source).strip():
            return "[오류] load_paper_source: source가 비어 있습니다.", None
        reviewer_mode = bool(params.get("reviewer_mode", False))

        jid = get_current_job(cfg, required=False)
        resolved = resolve_paper_source_to_library(
            str(source), cfg, session, logger, current_job=jid,
        )
        paper = load_paper_from_path(
            resolved.path, cfg, session, logger,
            source_metadata=resolved.metadata,
        )
        outputs = run_deep_paper_review(
            paper, resolved.paper_dir, cfg, session, logger,
            source_metadata=resolved.metadata,
            reviewer_mode=reviewer_mode,
        )
        if jid:
            append_action_log(
                jid, cfg, "load_paper_source",
                f"{source} -> {resolved.paper_dir.relative_to(cfg.base_dir)}",
            )
        paper_json_rel = str((resolved.paper_dir / "paper.paper.json").relative_to(cfg.base_dir))
        review_rel = str((resolved.paper_dir / "review.md").relative_to(cfg.base_dir))
        evidence_rel = str((resolved.paper_dir / "evidence.json").relative_to(cfg.base_dir))
        return (
            f"논문 라이브러리 저장 완료: {resolved.paper_id}\n"
            f"제목: {paper.title}\n"
            f"저자: {paper.authors or '논문에서 명시하지 않음'}\n"
            f"페이지: {len(paper.pages) or 'N/A'} / 섹션: {len(paper.sections)}\n\n"
            f"모델: research={cfg.research_model}"
            f"{' / reviewer=' + cfg.reviewer_model if reviewer_mode else ''}\n"
            f"리뷰: {review_rel}\n"
            f"근거 장부: {evidence_rel}\n"
            f"데이터: {paper_json_rel}\n\n"
            f"{Path(outputs['review']).read_text(encoding='utf-8')[:3000]}"
        ), paper_json_rel

    if action == "load_paper":
        from buildup.paper import (
            load_paper_from_path, summarize_paper,
            save_paper_json, LONG_PAPER_THRESHOLD,
        )
        from buildup.sandbox import require_read

        relpath = _relpath()
        jid = _current_job()
        base = job_dir(jid, cfg)
        path = ensure_within(base / relpath, base)
        require_read(path, cfg, context=f"load_paper {relpath}")
        paper = load_paper_from_path(path, cfg, session, logger)
        paper_chars = paper.total_chars()
        logger.info("load_paper: %s  chars=%d", relpath, paper_chars)

        # Save parsed sections as JSON for reuse
        json_path = save_paper_json(paper, base)

        # Generate structured summary
        summary = summarize_paper(paper, cfg, session, logger)

        # Save summary as markdown
        stem = path.stem[:50]
        summary_path = base / f"{stem}.summary.md"
        require_write(summary_path, jid, cfg, context="load_paper summary")
        summary_path.write_text(summary, encoding="utf-8")
        append_action_log(jid, cfg, "load_paper", relpath)

        long_note = (
            f"\n(논문 길이: {paper_chars:,}자 — "
            f"{'섹션별 처리' if paper_chars > LONG_PAPER_THRESHOLD else '전체 컨텍스트'})"
        )
        result = (
            f"논문 로드 완료: {paper.title}\n"
            f"저자: {paper.authors}\n"
            f"발표: {paper.venue} {paper.year}\n"
            f"섹션 {len(paper.sections)}개 파싱됨{long_note}\n\n"
            f"요약 파일: {summary_path.name}\n"
            f"데이터 파일: {json_path.name}\n\n"
            f"{summary[:3000]}"
        )
        return result, json_path.name

    if action == "paper_qa":
        from buildup.paper import (
            load_paper_json, find_paper_json, answer_question,
        )

        question = params.get("question", "")
        paper_hint = params.get("paper", "") or (
            last_file if last_file and last_file.endswith(".paper.json") else ""
        )

        jid = _current_job()
        base = job_dir(jid, cfg)

        paper_path = (
            ensure_within(base / paper_hint, base)
            if paper_hint else find_paper_json(base)
        )
        if paper_path is None or not paper_path.exists():
            return "[오류] 로드된 논문이 없습니다. 먼저 load_paper를 실행하세요.", None

        paper = load_paper_json(paper_path)
        answer = answer_question(paper, question, cfg, session, logger)
        return answer, paper_path.name

    if action == "explain_concept":
        from buildup.paper import (
            load_paper_json, find_paper_json, explain_concept as _explain,
        )

        concept = params.get("concept", "")
        paper_hint = params.get("paper", "") or (
            last_file if last_file and last_file.endswith(".paper.json") else ""
        )

        jid = _current_job()
        base = job_dir(jid, cfg)

        paper_path = (
            ensure_within(base / paper_hint, base)
            if paper_hint else find_paper_json(base)
        )
        if paper_path is None or not paper_path.exists():
            return "[오류] 로드된 논문이 없습니다. 먼저 load_paper를 실행하세요.", None

        paper = load_paper_json(paper_path)
        explanation = _explain(paper, concept, cfg, session, logger)
        return explanation, paper_path.name

    if action == "compare_papers":
        from buildup.paper import (
            load_paper_json, find_paper_json, compare_papers as _compare,
        )

        paper_a_hint = params.get("paper_a", "")
        paper_b_hint = params.get("paper_b", "")
        aspect = params.get("aspect", "방법론 및 성능")

        jid = _current_job()
        base = job_dir(jid, cfg)

        all_jsons = sorted(base.glob("*.paper.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if len(all_jsons) < 2 and not (paper_a_hint and paper_b_hint):
            return "[오류] 비교할 논문이 2개 필요합니다. load_paper로 두 논문을 먼저 로드하세요.", None

        def _find(hint: str, fallback_idx: int) -> Path:
            if hint:
                for p in all_jsons:
                    if hint.lower() in p.name.lower():
                        return p
            return all_jsons[fallback_idx]

        path_a = _find(paper_a_hint, 1)
        path_b = _find(paper_b_hint, 0)

        paper_a = load_paper_json(path_a)
        paper_b = load_paper_json(path_b)

        result = _compare(paper_a, paper_b, aspect, cfg, session, logger)
        return result, None

    if action == "edit_file":
        from buildup.jobs import cmd_edit
        relpath = _relpath()
        old_string = params.get("old_string", "")
        new_string = params.get("new_string", "")
        if not old_string:
            return "[오류] edit_file: old_string이 비어 있습니다.", None
        jid = _current_job()
        diff = cmd_edit(jid, relpath, old_string, new_string, cfg)
        return f"수정 완료: {relpath}\n\ndiff:\n{diff[:800]}", relpath

    if action == "glob_files":
        from buildup.paths import glob_job
        pattern = params.get("pattern", "**/*")
        jid = _current_job()
        base = job_dir(jid, cfg)
        results = glob_job(base, pattern)
        if not results:
            return "(매칭 없음)", None
        return "\n".join(results[:100]), None

    if action == "grep_files":
        from buildup.paths import grep_job
        pattern = params.get("pattern", "")
        path_glob = params.get("path_glob", "**/*")
        case_insensitive = bool(params.get("case_insensitive", False))
        if not pattern:
            return "[오류] grep_files: pattern이 비어 있습니다.", None
        jid = _current_job()
        base = job_dir(jid, cfg)
        try:
            results = grep_job(base, pattern, path_glob=path_glob, case_insensitive=case_insensitive)
        except ValueError as exc:
            return f"[오류] grep_files: {exc}", None
        if not results:
            return "(매칭 없음)", None
        return "\n".join(results[:80]), None

    if action == "run_shell":
        from buildup.command_runner import UnsafeCommandError, run_command
        command = params.get("command", "")
        if not command:
            return "[오류] run_shell: command가 비어 있습니다.", None

        jid = get_current_job(cfg, required=False)
        cwd = job_dir(jid, cfg) if jid else cfg.base_dir
        try:
            completed = run_command(command, cwd, timeout=60)
            output = completed.output
            lines = output.splitlines()
            if len(lines) > 150:
                lines = [f"(출력 {len(lines)}줄 중 마지막 150줄)"] + lines[-150:]
                output = "\n".join(lines)
            result = f"[exit {completed.returncode}]\n{output or '(출력 없음)'}"
            if jid:
                append_action_log(
                    jid, cfg, "shell",
                    f"exit={completed.returncode} cmd={command[:80]}",
                )
            return result, None
        except UnsafeCommandError as exc:
            return f"[차단] {exc}", None
        except Exception as exc:
            return f"[오류] {exc}", None

    if action == "git_run":
        from buildup.git_mgr import git_run as _git_run, RequiresConfirmation
        args = params.get("args", "")
        if not args:
            return "[오류] git_run: args가 비어 있습니다.", None
        jid = get_current_job(cfg, required=False)
        cwd = job_dir(jid, cfg) if jid else cfg.base_dir
        try:
            output, code = _git_run(args, cwd, confirm_destructive=False)
            result = f"[exit {code}]\n{output or '(출력 없음)'}"
            if jid:
                append_action_log(jid, cfg, "git", f"exit={code} args={args[:80]}")
            return result, None
        except RequiresConfirmation as exc:
            return f"[차단] {exc}\n에이전트는 파괴적 git 명령을 실행할 수 없습니다. 사용자가 /git 명령으로 직접 실행하세요.", None
        except Exception as exc:
            return f"[오류] git: {exc}", None

    return f"[알 수 없는 도구: {action}]", None


# ============================================================
# Main agent loop  (legacy ReAct — kept as fallback)
# ============================================================

def run_agent_legacy(
    user_input: str,
    cfg: BuildupConfig,
    session: requests.Session,
    logger: logging.Logger,
    mode: str,
    history,
    render_status,
    tracer=None,
    exemplar_index=None,
    task_frame=None,
    assistant_context: str = "",
    active_paper_id: Optional[str] = None,
) -> str:
    """Run the ReAct agent loop.

    Returns the final answer string to display to the user.
    """
    ctx = _build_context(cfg)
    ctx["bilingual_rule"] = bilingual_clause()
    if exemplar_index is not None:
        ctx["exemplar_context"] = exemplar_index.retrieve(user_input)
    system_prompt = AGENT_SYSTEM_PROMPT.format(**ctx)
    try:
        from buildup.skills import build_skills_context
        skill_context, _selected_skills = build_skills_context(user_input, cfg)
    except Exception as exc:
        logger.warning("Skill context unavailable: %s", exc)
        skill_context = ""

    # Build initial message list: system + condensed history + current input
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if history:
        for msg in history.get_messages("")[1:]:   # skip old system prompt
            messages.append(msg)
    llm_user_input = user_input
    if assistant_context:
        llm_user_input = f"{user_input}\n\n{assistant_context}"
    if task_frame is not None:
        try:
            frame_context = task_frame.agent_context()
        except Exception:
            frame_context = ""
        if frame_context:
            llm_user_input = f"{llm_user_input}\n\n{frame_context}"
    if skill_context:
        llm_user_input = f"{llm_user_input}\n\n{skill_context}"
    messages.append({"role": "user", "content": llm_user_input})

    last_file: Optional[str] = None
    step_log: List[str] = []

    import time as _time

    for step in range(AGENT_MAX_STEPS):
        render_status(f"Agent [{step + 1}/{AGENT_MAX_STEPS}] reasoning...")
        _step_start = _time.monotonic()

        try:
            raw = _stream_agent_step(session, cfg, messages, logger)
        except Exception as exc:
            return f"[Agent error] LLM call failed: {exc}"

        action_data = _parse_action(raw)
        if action_data is None:
            # Model produced non-JSON — treat as plain answer
            logger.warning("Agent: non-JSON response at step %d: %s", step + 1, raw[:200])
            if history:
                history.add("user", user_input)
                history.add("assistant", raw)
            return raw

        action = str(action_data.get("action", "")).strip()
        params = action_data.get("params", {}) or {}
        logger.info("Agent step %d: action=%s params=%s", step + 1, action, params)

        render_status(f"[{step + 1}] {action}")

        # Final answer
        if action == "answer":
            answer_text = params.get("content", "")
            if not answer_text:
                answer_text = raw
            if tracer:
                tracer.add_step(
                    step + 1, "answer", {},
                    answer_text[:200], True,
                    int((_time.monotonic() - _step_start) * 1000),
                )
            if history:
                history.add("user", user_input)
                history.add("assistant", answer_text)
            return answer_text

        # Execute tool
        try:
            tool_result, new_last_file = _execute_tool(
                action, params, cfg, session, logger, mode, last_file, active_paper_id
            )
            if new_last_file:
                last_file = new_last_file
            step_log.append(f"✓ {action}: {tool_result[:80]}")
            if tracer:
                tracer.add_step(
                    step + 1, action, params,
                    tool_result, True,
                    int((_time.monotonic() - _step_start) * 1000),
                )
        except Exception as exc:
            tool_result = f"[도구 오류] {exc}"
            logger.warning("Agent tool error at step %d: %s", step + 1, exc)
            step_log.append(f"✗ {action}: {exc}")
            if tracer:
                tracer.add_step(
                    step + 1, action, params,
                    str(exc), False,
                    int((_time.monotonic() - _step_start) * 1000),
                )

        # Feed result back into conversation
        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": (
                f"[도구 결과: {action}]\n{tool_result}\n\n"
                f"last_file={last_file or '(없음)'}"
            ),
        })

    # Max steps reached
    summary = "\n".join(step_log[-5:])
    return (
        f"[Agent] Max steps ({AGENT_MAX_STEPS}) reached.\n"
        f"Completed:\n{summary}"
    )


# ============================================================
# Public entry point — State Machine with ReAct fallback
# ============================================================

def run_agent(
    user_input: str,
    cfg: BuildupConfig,
    session: requests.Session,
    logger: logging.Logger,
    mode: str,
    history,
    render_status,
    tracer=None,
    exemplar_index=None,
    task_frame=None,
    assistant_context: str = "",
    active_paper_id: Optional[str] = None,
) -> str:
    """Dispatch to PlanStateMachine; fall back to legacy ReAct on failure.

    Set BUILDUP_USE_STATE_MACHINE=0 to bypass the state machine entirely.
    """
    import os
    use_sm = os.environ.get("BUILDUP_USE_STATE_MACHINE", "1") not in ("0", "false", "False")

    if use_sm:
        from buildup.state_machine import PlanStateMachine
        from buildup.plan import PlanningError
        try:
            machine = PlanStateMachine()
            return machine.run(
                user_input, cfg, session, logger, mode,
                history, render_status, tracer, exemplar_index, task_frame,
                assistant_context, active_paper_id,
            )
        except PlanningError as exc:
            logger.warning(
                "PlanStateMachine failed (%s): %s — falling back to legacy ReAct",
                exc.reason, exc.message,
            )
        except Exception:
            logger.exception("PlanStateMachine unexpected error — falling back to legacy ReAct")

    return run_agent_legacy(
        user_input, cfg, session, logger, mode,
        history, render_status, tracer, exemplar_index, task_frame,
        assistant_context, active_paper_id,
    )
