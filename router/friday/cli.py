"""CLI entry point: argparse definitions and command routing."""

from __future__ import annotations

import argparse
import signal

from friday.calendar_mgr import (
    cal_add,
    cal_delete,
    cal_export_ics,
    cal_import_ics,
    cal_list,
    cal_today,
    format_events,
)
from friday.config import load_config
from friday.http_client import build_session
from friday.jobs import (
    append_action_log,
    cmd_files,
    cmd_import,
    cmd_job_list,
    cmd_job_new,
    cmd_job_use,
    cmd_read,
    read_action_log,
)
from friday.logging_setup import setup_logging
from friday.ollama import (
    answer_query,
    auto_route,
    chat,
    rewrite_file,
)
from friday.openwebui import (
    ask_openwebui_with_kb,
    extract_chat_completion_text,
    sync_job_to_openwebui,
)
from friday.paths import (
    compute_diff,
    ensure_within,
    export_job,
    move_to_trash,
    read_text_file,
    resolve_path,
)
from friday.prompts import system_fast
from friday.rendering import (
    console,
    render_answer,
    render_diff,
    render_info,
)
from friday.search import format_search_results, web_search
from friday.shell import InteractiveShell
from friday.state import get_current_job, job_dir
from friday.task_frame import build_task_frame, format_task_frame_debug, frame_clarification
from friday.harness import format_harness_results, make_cases, results_to_json, run_harness
from friday.deep_research import (
    format_research_runs,
    latest_completed_research_run,
    list_research_runs,
    resolve_research_run,
    run_deep_research,
)
from friday.doctor import format_doctor, run_doctor
from friday.paper_translation import resolve_translation_target, translate_paper_pdf
from friday.skills import (
    find_skill,
    format_skill_audit,
    format_skill_detail,
    format_skill_list,
    format_skill_matches,
    load_skills,
    rebuild_skill_index,
    select_skills,
)
from friday.session_store import (
    archive_session,
    export_session,
    list_sessions,
    new_session_id,
    rename_session,
    resolve_session,
    save_session,
    search_sessions,
    workspace_key_for,
    workspace_label,
)
from friday.study import (
    append_study_note,
    close_study,
    complete_study_review,
    due_studies,
    format_study_list,
    list_studies,
    resolve_study,
    start_study,
)
from friday import COMMAND_NAME, PRODUCT_NAME, __version__


def main() -> None:
    cfg = load_config()
    for d in cfg.all_dirs:
        d.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(cfg)
    session = build_session(cfg)
    logger.info("%s started", PRODUCT_NAME)

    parser = argparse.ArgumentParser(
        prog=COMMAND_NAME,
        description=f"{PRODUCT_NAME} — local deep-research agent and study coach",
    )
    parser.add_argument("--version", action="version", version=f"{PRODUCT_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("shell", help="interactive shell")
    sub.add_parser("doctor", help="check local release readiness")

    ask_p = sub.add_parser("ask", help="ask a question")
    ask_p.add_argument("query", nargs="+")
    ask_p.add_argument("--mode", choices=["auto", "fast", "main", "refine"], default="auto")

    frame_p = sub.add_parser("frame", help="debug natural-language task framing")
    frame_p.add_argument("text", nargs="+")

    harness_p = sub.add_parser("harness", help="run deterministic dispatch harness")
    harness_p.add_argument("inputs", nargs="*", help="optional natural-language inputs to inspect")
    harness_p.add_argument("--job", help="job id to use as current-job context")
    harness_p.add_argument("--with-embed", action="store_true", help="include Tier-1b embedding classifier")
    harness_p.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    skills_p = sub.add_parser("skills", help="manage installed Agent Skills")
    skills_sub = skills_p.add_subparsers(dest="skills_command")
    skills_sub.add_parser("list")
    skills_search = skills_sub.add_parser("search")
    skills_search.add_argument("query", nargs="+")
    skills_show = skills_sub.add_parser("show")
    skills_show.add_argument("identifier")
    skills_sub.add_parser("reindex")
    skills_sub.add_parser("audit")

    job_p = sub.add_parser("job", help="manage jobs")
    job_sub = job_p.add_subparsers(dest="job_command")
    job_new = job_sub.add_parser("new")
    job_new.add_argument("name", nargs="?")
    job_new.add_argument("--tpl", dest="template")
    job_use = job_sub.add_parser("use")
    job_use.add_argument("job_id")
    job_sub.add_parser("current")
    job_sub.add_parser("list")
    job_sub.add_parser("log")
    job_sub.add_parser("summary")

    import_p = sub.add_parser("import", help="copy into current job")
    import_p.add_argument("src")
    import_p.add_argument("--job")

    files_p = sub.add_parser("files", help="list files")
    files_p.add_argument("--job")

    read_p = sub.add_parser("read", help="read a file")
    read_p.add_argument("relpath")
    read_p.add_argument("--job")

    rewrite_p = sub.add_parser("rewrite", help="rewrite a text file")
    rewrite_p.add_argument("relpath")
    rewrite_p.add_argument("instruction")
    rewrite_p.add_argument("--output")
    rewrite_p.add_argument("--job")
    rewrite_p.add_argument("--mode", choices=["auto", "fast", "main", "refine"], default="auto")
    rewrite_p.add_argument("--no-diff", action="store_true")

    diff_p = sub.add_parser("diff", help="diff two files")
    diff_p.add_argument("file_a")
    diff_p.add_argument("file_b")
    diff_p.add_argument("--job")

    trash_p = sub.add_parser("trash", help="move to trash")
    trash_p.add_argument("relpath")
    trash_p.add_argument("--job")

    export_p = sub.add_parser("export", help="export a job")
    export_p.add_argument("--job")
    export_p.add_argument("--dest")

    search_p = sub.add_parser("search", help="web search + AI synthesis")
    search_p.add_argument("query", nargs="+")
    search_p.add_argument("--mode", choices=["auto", "fast", "main", "refine"], default="auto")

    research_p = sub.add_parser("research", help="run or inspect one-call staged deep research")
    research_p.add_argument("query", nargs="*")
    research_p.add_argument("--list", action="store_true", help="list research runs in current job")
    research_p.add_argument("--show", metavar="SELECTOR", help="show a saved research report")
    research_p.add_argument("--max-sources", type=int, default=10, help="maximum normalized sources (1-30)")

    sessions_p = sub.add_parser("sessions", help="list or search durable sessions")
    sessions_p.add_argument("query", nargs="*")
    sessions_p.add_argument("--all", action="store_true", help="include every workspace")

    session_p = sub.add_parser("session", help="manage one durable session")
    session_sub = session_p.add_subparsers(dest="session_command")
    session_export = session_sub.add_parser("export")
    session_export.add_argument("selector")
    session_export.add_argument("--format", choices=["md", "json"], default="md")
    session_archive = session_sub.add_parser("archive")
    session_archive.add_argument("selector")
    session_rename = session_sub.add_parser("rename")
    session_rename.add_argument("selector")
    session_rename.add_argument("title", nargs="+")

    study_p = sub.add_parser("study", help="manage personal study sessions")
    study_sub = study_p.add_subparsers(dest="study_command")
    study_start = study_sub.add_parser("start")
    study_start.add_argument("topic", nargs="+")
    study_sub.add_parser("list")
    study_use = study_sub.add_parser("use")
    study_use.add_argument("selector", nargs="?", default="latest")
    study_note = study_sub.add_parser("note")
    study_note.add_argument("text", nargs="+")
    study_close = study_sub.add_parser("close")
    study_close.add_argument("reflection", nargs="*")
    study_review = study_sub.add_parser("review")
    study_review.add_argument("selector", nargs="?", default="")
    study_review.add_argument("--done", action="store_true", help="complete the earliest due review")
    study_status = study_sub.add_parser("status")
    study_status.add_argument("selector", nargs="?", default="latest")

    translate_p = sub.add_parser("translate", help="translate a research PDF into Korean Markdown")
    translate_p.add_argument("source", help="current-job PDF path or paper-library id")

    owui_p = sub.add_parser("owui", help="Open WebUI integration")
    owui_sub = owui_p.add_subparsers(dest="owui_command")
    owui_sync = owui_sub.add_parser("sync")
    owui_sync.add_argument("kb_key", choices=["research", "coding", "ops"])
    owui_sync.add_argument("--job")
    owui_ask = owui_sub.add_parser("ask")
    owui_ask.add_argument("kb_key", choices=["research", "coding", "ops"])
    owui_ask.add_argument("question", nargs="+")
    owui_ask.add_argument("--mode", choices=["auto", "fast", "main", "refine"], default="auto")

    cal_p = sub.add_parser("cal", help="calendar management")
    cal_sub = cal_p.add_subparsers(dest="cal_command")
    cal_add_p = cal_sub.add_parser("add")
    cal_add_p.add_argument("date")
    cal_add_p.add_argument("title", nargs="+")
    cal_add_p.add_argument("--time")
    cal_add_p.add_argument("--duration", type=int, default=60)
    cal_add_p.add_argument("--note", default="")
    cal_list_p = cal_sub.add_parser("list")
    cal_list_p.add_argument("--days", type=int, default=7)
    cal_list_p.add_argument("--from", dest="date_from")
    cal_list_p.add_argument("--to", dest="date_to")
    cal_sub.add_parser("today")
    cal_del_p = cal_sub.add_parser("delete")
    cal_del_p.add_argument("event_id")
    cal_sub.add_parser("export")
    cal_imp_p = cal_sub.add_parser("import")
    cal_imp_p.add_argument("ics_path")

    args = parser.parse_args()

    # Default → interactive
    if args.command is None or args.command == "shell":
        shell = InteractiveShell(cfg, session, logger)

        def _sig(signum, frame):
            console.print("\n[bold red]종료합니다.[/bold red]")
            logger.info("Terminated by signal %s", signum)
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
        shell.run()
        logger.info("%s exited normally", PRODUCT_NAME)
        return

    try:
        if args.command == "doctor":
            report = run_doctor(cfg, session)
            render_info(f"{PRODUCT_NAME} doctor", format_doctor(report), "green" if report.ready else "red")
            if not report.ready:
                raise SystemExit(1)

        elif args.command == "ask":
            query = " ".join(args.query)
            console.print("[dim]Thinking...[/dim]")
            result, model_name, used_mode = answer_query(
                query, args.mode, session, cfg, logger,
            )
            render_answer(result, model_name, used_mode)

        elif args.command == "frame":
            text = " ".join(args.text)
            frame = build_task_frame(text, cfg, get_current_job(cfg, required=False))
            body = format_task_frame_debug(frame)
            clarification = frame_clarification(frame)
            if clarification:
                body += "\n\nclarification:\n" + clarification
            render_info("Task Frame", body, "cyan")

        elif args.command == "harness":
            classifier = None
            if args.with_embed:
                from friday.embed_classifier import IntentEmbedClassifier

                classifier = IntentEmbedClassifier(session, cfg)
                classifier.init()
            current_job = args.job if args.job is not None else get_current_job(cfg, required=False)
            cases = make_cases(args.inputs) if args.inputs else None
            results = run_harness(
                cfg,
                current_job=current_job,
                cases=cases,
                classifier=classifier,
            )
            if args.json:
                console.print(results_to_json(results))
            else:
                style = "green" if all(result.passed for result in results) else "red"
                render_info("Harness", format_harness_results(results), style)
            if not all(result.passed for result in results):
                raise SystemExit(1)

        elif args.command == "skills":
            if args.skills_command in (None, "list"):
                skills = load_skills(cfg)
                render_info("Skills", format_skill_list(skills), "cyan")
            elif args.skills_command == "search":
                query = " ".join(args.query)
                render_info("Skill Search", format_skill_matches(select_skills(query, cfg, limit=10)), "cyan")
            elif args.skills_command == "show":
                skill = find_skill(args.identifier, cfg)
                if not skill:
                    raise ValueError(f"스킬을 찾지 못했습니다: {args.identifier}")
                render_info("Skill", format_skill_detail(skill), "cyan")
            elif args.skills_command == "reindex":
                skills = rebuild_skill_index(cfg)
                render_info("Skills", f"색인 완료: {len(skills)} skills", "green")
            elif args.skills_command == "audit":
                render_info("Skill Audit", format_skill_audit(load_skills(cfg)), "cyan")
            else:
                raise ValueError("skills 하위 명령: list / search / show / reindex / audit")

        elif args.command == "job":
            if args.job_command == "new":
                jid, created = cmd_job_new(args.name, cfg, args.template)
                msg = f"새 작업 폴더: {jid}\n{job_dir(jid, cfg)}"
                if created:
                    msg += "\n\n템플릿:\n  " + "\n  ".join(created)
                render_info("Job", msg)
            elif args.job_command == "use":
                path = cmd_job_use(args.job_id, cfg)
                render_info("Job", f"→ {args.job_id}\n{path}")
            elif args.job_command == "current":
                render_info("Job", f"current: {get_current_job(cfg, required=False) or '-'}")
            elif args.job_command == "list":
                jobs = cmd_job_list(cfg)
                current = get_current_job(cfg, required=False)
                lines = [f"  {j}{' ← current' if j == current else ''}" for j in jobs]
                render_info("Jobs", "\n".join(lines) or "(no jobs)")
            elif args.job_command == "log":
                jid = get_current_job(cfg, required=True)
                entries = read_action_log(jid, cfg)
                for e in entries:
                    console.print(f"  [{e.get('ts','')}] {e.get('action','')}: {e.get('detail','')}")
            elif args.job_command == "summary":
                jid = get_current_job(cfg, required=True)
                entries = read_action_log(jid, cfg)
                if not entries:
                    render_info("Summary", "이력 없음")
                else:
                    log_text = "\n".join(
                        f"[{e.get('ts','')}] {e.get('action','')}: {e.get('detail','')}"
                        for e in entries
                    )
                    summary = chat(
                        session, cfg, cfg.fast_model,
                        [{"role": "system", "content": "작업 이력을 간결하게 요약하라. 한국어 먼저, 마지막에 짧은 English Brief를 붙여라."},
                         {"role": "user", "content": f"요약:\n{log_text}"}],
                        keep_alive="2m", logger=logger, display_thinking=True,
                    )
                    render_answer(summary, "Summary", "fast")
            else:
                raise ValueError("job 하위 명령: new / use / current / list / log / summary")

        elif args.command == "import":
            dst, _ = cmd_import(
                args.src, args.job or get_current_job(cfg, required=False), cfg,
            )
            render_info("Import", f"복사 완료\n{dst}")

        elif args.command == "files":
            jid = args.job or get_current_job(cfg, required=True)
            body = "\n".join(cmd_files(jid, cfg)) or "(empty)"
            render_info("Files", body)

        elif args.command == "read":
            jid = args.job or get_current_job(cfg, required=True)
            content = cmd_read(jid, args.relpath, cfg)
            render_info(f"Read: {args.relpath}", content, "cyan")

        elif args.command == "rewrite":
            jid = args.job or get_current_job(cfg, required=True)
            out_path, diff_text = rewrite_file(
                jid, args.relpath, args.instruction, args.output,
                args.mode, session, cfg, logger, show_diff=not args.no_diff,
            )
            render_info("Rewrite", f"수정본 생성 완료\n{out_path}")
            if diff_text:
                render_diff(diff_text)

        elif args.command == "diff":
            jid = args.job or get_current_job(cfg, required=True)
            base = job_dir(jid, cfg)
            pa = ensure_within(base / args.file_a, base)
            pb = ensure_within(base / args.file_b, base)
            ta = read_text_file(pa, cfg)
            tb = read_text_file(pb, cfg)
            render_diff(compute_diff(ta, tb, filename=pa.name))

        elif args.command == "trash":
            jid = args.job or get_current_job(cfg, required=True)
            base = job_dir(jid, cfg)
            target = ensure_within(base / args.relpath, base)
            moved = move_to_trash(target, jid, cfg)
            append_action_log(jid, cfg, "trash", args.relpath)
            render_info("Trash", f"이동 완료\n{moved}")

        elif args.command == "export":
            jid = args.job or get_current_job(cfg, required=True)
            dest = resolve_path(args.dest) if args.dest else None
            exported = export_job(jid, cfg, dest)
            append_action_log(jid, cfg, "export", str(exported))
            render_info("Export", f"내보내기 완료\n{exported}")

        elif args.command == "search":
            query = " ".join(args.query)
            console.print("[dim]Searching...[/dim]")
            results, engine = web_search(query, cfg, session, max_results=cfg.web_search_max_results)
            formatted = format_search_results(results, engine)
            render_info("Search Results", formatted, "dim")
            if results:
                console.print("[dim]Synthesizing...[/dim]")
                synthesis = chat(
                    session, cfg, cfg.fast_model,
                    [{"role": "system", "content": system_fast()},
                     {"role": "user", "content": f"검색 결과를 바탕으로 답하라:\n\n[질문] {query}\n\n[검색 결과]\n{formatted}"}],
                    keep_alive="2m", logger=logger, display_thinking=True,
                )
                render_answer(synthesis, f"Search ({engine}) + {cfg.fast_model}", "fast")

        elif args.command == "research":
            jid = get_current_job(cfg, required=False)
            if args.list:
                if not jid:
                    raise ValueError(
                        f"현재 job이 없습니다. 먼저 `{COMMAND_NAME} job new NAME --tpl research`를 실행하세요."
                    )
                render_info("Research Runs", format_research_runs(list_research_runs(jid, cfg)), "cyan")
                return
            if args.show:
                if not jid:
                    raise ValueError("현재 job이 없습니다.")
                run = resolve_research_run(args.show, jid, cfg)
                if not run:
                    raise ValueError(f"research run을 찾지 못했습니다: {args.show}")
                run_dir = resolve_path(str(run["run_dir"]))
                report_path = run_dir / "06-report.md"
                if not report_path.is_file():
                    raise ValueError(f"보고서가 없습니다: {report_path}")
                render_answer(report_path.read_text(encoding="utf-8"), f"Research · {run.get('run_id', '')}", "research")
                render_info("Research Files", str(run_dir), "green")
                return
            query = " ".join(args.query)
            if not query.strip():
                raise ValueError("research 주제를 입력하세요.")
            research_session_id = new_session_id()
            result = run_deep_research(
                query,
                cfg,
                session,
                logger,
                max_results=args.max_sources,
                status=lambda msg: console.print(f"[dim]{msg}[/dim]"),
                session_id=research_session_id,
                workspace_key=workspace_key_for(cfg, jid) if jid else "",
            )
            messages = [
                {"role": "user", "content": f"[deep research] {query}", "kind": "research"},
                {
                    "role": "assistant", "content": result.report, "kind": "research",
                    "research_run_id": result.run_dir.name,
                    "research_run_dir": str(result.run_dir),
                },
            ]
            save_session(
                research_session_id,
                messages,
                result.job_id,
                cfg,
                workspace_key=workspace_key_for(cfg, result.job_id),
                kind="research",
                status="ended",
                model_config={"night": cfg.night_model},
            )
            rename_session(research_session_id, f"Research · {query[:68]}", cfg)
            render_answer(result.report, f"{result.model} · 1 model call", "research")
            render_info("Research Files", str(result.run_dir), "green")

        elif args.command == "sessions":
            current_job = get_current_job(cfg, required=False)
            key = None if args.all else workspace_key_for(cfg, current_job)
            query = " ".join(args.query).strip()
            if query:
                hits = search_sessions(query, cfg, workspace_key=key)
                body = "\n".join(
                    f"{index}. {hit.session.title} [{hit.session.session_id[:8]}]\n"
                    f"   {workspace_label(hit.session.workspace_key)} · {hit.snippet}"
                    for index, hit in enumerate(hits, start=1)
                ) or "검색 결과가 없습니다."
                render_info("Session Search", body, "cyan")
            else:
                infos = list_sessions(cfg, workspace_key=key)
                body = "\n".join(
                    f"{index}. {info.title} · {info.kind} · {workspace_label(info.workspace_key)} · "
                    f"{info.turn_count} turns · {info.session_id[:8]}"
                    for index, info in enumerate(infos, start=1)
                ) or "저장된 대화가 없습니다."
                render_info("Sessions", body, "cyan")

        elif args.command == "session":
            key = workspace_key_for(cfg, get_current_job(cfg, required=False))
            if not args.session_command:
                raise ValueError("session 하위 명령: export / archive / rename")
            info = resolve_session(args.selector, cfg, workspace_key=key)
            if not info:
                raise ValueError(f"세션을 찾지 못했습니다: {args.selector}")
            if args.session_command == "export":
                render_info("Session Export", str(export_session(info.session_id, cfg, fmt=args.format)), "green")
            elif args.session_command == "archive":
                archive_session(info.session_id, cfg)
                render_info("Session", f"보관 완료: {info.title}", "green")
            elif args.session_command == "rename":
                title = " ".join(args.title)
                if not rename_session(info.session_id, title, cfg):
                    raise ValueError("같은 workspace에 동일한 제목이 있거나 제목이 올바르지 않습니다.")
                render_info("Session", f"이름 변경: {title}", "green")

        elif args.command == "study":
            command = args.study_command or "list"
            jid = get_current_job(cfg, required=False)
            if command == "start":
                topic = " ".join(args.topic)
                if not jid:
                    jid, _ = cmd_job_new(f"study-{topic[:24]}", cfg, "study")
                latest = latest_completed_research_run(jid, cfg)
                source = resolve_path(str(latest["run_dir"])) if latest else None
                info = start_study(topic, jid, cfg, source_research=source)
                append_action_log(jid, cfg, "study_start", info.study_id)
                render_info("Study Started", f"{info.topic}\n{info.path}\nnext: {info.next_action}", "green")
            else:
                if not jid:
                    raise ValueError("현재 job이 없습니다.")
                if command == "list":
                    render_info("Studies", format_study_list(list_studies(jid, cfg)), "cyan")
                elif command == "review":
                    if args.done:
                        info = resolve_study(args.selector or "latest", jid, cfg)
                        if not info:
                            raise ValueError(f"공부 세션을 찾지 못했습니다: {args.selector}")
                        updated, review_date = complete_study_review(info, cfg)
                        append_action_log(jid, cfg, "study_review", f"{updated.study_id}:{review_date}")
                        render_info("Study Review", f"{review_date} 회차 완료\nnext: {updated.next_action}", "green")
                    elif args.selector:
                        info = resolve_study(args.selector, jid, cfg)
                        if not info:
                            raise ValueError(f"공부 세션을 찾지 못했습니다: {args.selector}")
                        render_info(
                            "Study Review",
                            f"{info.topic}\n노트를 열기 전에 핵심 개념을 자료 없이 설명하세요.\n{info.path}",
                            "cyan",
                        )
                    else:
                        render_info("Due Reviews", format_study_list(due_studies(jid, cfg)), "cyan")
                elif command == "status":
                    info = resolve_study(args.selector, jid, cfg)
                    if not info:
                        raise ValueError(f"공부 세션을 찾지 못했습니다: {args.selector}")
                    render_info(
                        "Study",
                        f"{info.topic} · {info.status}\nverified: {info.verified_notes_count}\n"
                        f"reviews: {len(info.completed_reviews)}/{len(info.review_dates)}\n"
                        f"next: {info.next_action}\n{info.path}",
                        "cyan",
                    )
                else:
                    selector = getattr(args, "selector", "latest")
                    info = resolve_study(selector, jid, cfg)
                    if not info:
                        raise ValueError(f"공부 세션을 찾지 못했습니다: {selector}")
                    if command == "use":
                        render_info("Study", f"{info.topic}\nnext: {info.next_action}\n{info.path}", "cyan")
                    elif command == "note":
                        updated = append_study_note(info, " ".join(args.text), cfg)
                        append_action_log(jid, cfg, "study_note", updated.study_id)
                        render_info("Verified Note", f"저장 완료 · {updated.verified_notes_count}", "green")
                    elif command == "close":
                        closed = close_study(info, cfg, " ".join(args.reflection))
                        append_action_log(jid, cfg, "study_close", closed.study_id)
                        render_info("Study Closed", closed.next_action, "green")

        elif args.command == "translate":
            current_job = get_current_job(cfg, required=False)
            target = resolve_translation_target(args.source, cfg, current_job=current_job)
            result = translate_paper_pdf(
                target,
                cfg,
                session,
                logger,
                status=lambda msg: console.print(f"[dim]{msg}[/dim]"),
            )
            render_info(
                "Paper Translation",
                f"{result.output_path}\n{result.page_count} pages · {result.model_calls} model call(s)",
                "green",
            )

        elif args.command == "cal":
            if args.cal_command == "add":
                title = " ".join(args.title)
                ev = cal_add(title, args.date, args.time, args.duration, args.note, cfg)
                render_info("Calendar", f"추가됨: {ev['title']}  📅 {ev['date']} {ev.get('time','종일')}")
            elif args.cal_command == "list":
                events = cal_list(cfg, date_from=args.date_from, date_to=args.date_to, upcoming_days=args.days)
                render_info("Calendar", format_events(events), "cyan")
            elif args.cal_command == "today":
                render_info("오늘 일정", format_events(cal_today(cfg)), "cyan")
            elif args.cal_command == "delete":
                removed = cal_delete(args.event_id, cfg)
                if removed:
                    render_info("Calendar", f"삭제됨: {removed['title']}")
                else:
                    render_info("Calendar", "해당 ID 없음", "yellow")
            elif args.cal_command == "export":
                ics_path = cal_export_ics(cfg)
                render_info("Calendar", f"ICS 내보내기 완료\n{ics_path}")
            elif args.cal_command == "import":
                path = resolve_path(args.ics_path)
                count = cal_import_ics(path, cfg)
                render_info("Calendar", f"{count}개 일정 가져오기 완료")
            else:
                raise ValueError("cal 하위 명령: add / list / today / delete / export / import")

        elif args.command == "owui":
            if args.owui_command == "sync":
                jid = args.job or get_current_job(cfg, required=True)
                synced = sync_job_to_openwebui(jid, args.kb_key, session, cfg)
                body = "\n".join(f"{r} -> {f}" for r, f in synced) or "(no files)"
                render_info("Open WebUI Sync", body)
            elif args.owui_command == "ask":
                kb_id = cfg.openwebui_kb_map.get(args.kb_key, "")
                if not kb_id:
                    raise ValueError(f"OPENWEBUI_KB_{args.kb_key.upper()} 환경변수가 없습니다.")
                question = " ".join(args.question)
                um = auto_route(question, cfg) if args.mode == "auto" else args.mode
                model = cfg.main_model if um in {"main", "refine"} else cfg.fast_model
                resp = ask_openwebui_with_kb(question, model, kb_id, session, cfg)
                text = extract_chat_completion_text(resp.json())
                render_answer(text, f"Open WebUI KB:{args.kb_key}", um)
            else:
                raise ValueError("owui 하위 명령: sync / ask")
        else:
            raise ValueError(f"알 수 없는 명령: {args.command}")

    except (ConnectionError, TimeoutError) as exc:
        render_info("Connection Error", str(exc), "red")
        logger.error("Connection: %s", exc)
        raise SystemExit(1)
    except Exception as exc:
        render_info("Error", str(exc), "red")
        logger.exception("Failed: %s", args.command)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
