"""Interactive shell for the build-up CLI."""

from __future__ import annotations

import logging
import re
import readline  # noqa: F401 — imported for side-effect (input history)
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional

import requests
from rich.panel import Panel
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from buildup import PRODUCT_NAME
from buildup.calendar_mgr import (
    cal_add,
    cal_delete,
    cal_export_ics,
    cal_import_ics,
    cal_list,
    cal_today,
    format_events,
)
from buildup.config import BuildupConfig
from buildup.command_runner import (
    ALLOWED_COMMANDS,
    UnsafeCommandError,
    parse_command,
    run_command,
)
from buildup.conversation import ConversationHistory
from buildup.deep_research import (
    evaluate_research_run,
    format_research_runs,
    latest_completed_research_run,
    list_research_runs,
    resolve_research_run,
    run_deep_research,
)
from buildup.research.evaluation import format_evaluation
from buildup.knowledge import (
    add_research_to_vault,
    bind_job_to_vault,
    compile_vault,
    format_ingest_result,
    format_lint_report,
    format_query_results,
    format_vault_summary,
    lint_vault,
    query_vault,
    reject_claim,
    review_vault,
    rollback_proposal,
    verify_claim,
    vault_summary,
)
from buildup.conversation_memory import (
    append_conversation_memory,
    format_conversation_memory,
    load_conversation_memory,
    memory_markdown_path,
)
from buildup.agent import run_agent
from buildup.git_mgr import RequiresConfirmation, git_run
from buildup.jobs import (
    append_action_log,
    cmd_edit,
    cmd_files,
    cmd_import,
    cmd_job_list,
    cmd_job_new,
    cmd_job_rename,
    cmd_job_use,
    cmd_read,
    cmd_write,
    format_job_label,
    job_display_name,
    list_templates,
    read_action_log,
)
from buildup.embed_classifier import IntentEmbedClassifier
from buildup.exemplar import ExemplarIndex
from buildup.intent import is_chat, parse_intent_instant
from buildup.trace import TraceRecorder
from buildup.task_frame import (
    bind_active_paper_target,
    build_task_frame,
    enrich_intent_with_frame,
    format_execution_preview,
    frame_clarification,
)
from buildup.skills import (
    find_skill,
    format_skill_audit,
    format_skill_detail,
    format_skill_list,
    format_skill_matches,
    load_skills,
    rebuild_skill_index,
    select_skills,
)
from buildup.paper_library import (
    active_paper_context,
    find_paper_entry,
    format_active_paper,
    format_paper_shelf,
    list_paper_library,
)
from buildup.paper_translation import resolve_translation_target, translate_paper_pdf
from buildup.ollama import answer_query_stream, chat, chat_stream, check_ollama, rewrite_file
from buildup.openwebui import ask_openwebui_with_kb, extract_chat_completion_text, sync_job_to_openwebui
from buildup.paths import (
    compute_diff,
    ensure_within,
    export_job,
    is_text_file,
    move_to_trash,
    read_pdf_file,
    read_text_file,
    reject_symlink,
    resolve_path,
)
from buildup.permission import evaluate_intent_policy
from buildup.prompts import (
    system_main,
    system_search_synthesis,
    deep_search_file_prompt,
    bilingual_clause,
    english_brief_enabled,
    set_english_brief,
    set_runtime_steering,
    set_user_prefs,
)
from buildup.context import load_user_prefs
from buildup.rendering import (
    build_prompt,
    console,
    help_text,
    render_answer,
    render_day_banner,
    render_diff,
    render_header,
    render_info,
    render_research_banner,
    render_previous_conversation,
    read_prompt,
    render_status,
    StatusLine,
    render_streaming_answer,
)
from buildup.search import format_search_results, web_search
from buildup.state import get_current_job, job_dir
from buildup.session_store import (
    SessionInfo,
    SessionLease,
    archive_session,
    export_session,
    list_sessions,
    load_latest_session,
    load_session,
    mark_session_status,
    new_session_id,
    rename_session,
    resolve_session,
    save_session,
    search_sessions,
    workspace_key_for,
    workspace_label,
)
from buildup.study import (
    append_study_note,
    close_study,
    complete_study_review,
    due_studies,
    format_study_list,
    list_studies,
    load_study,
    resolve_study,
    start_study,
    study_context,
)
from buildup.steering import (
    SteeringState,
    add_directive,
    clear_steering,
    format_profile_list,
    format_steering_status,
    remove_directive,
    set_directives,
    set_profile,
    steering_prompt,
)

RESEARCH_PAPER_READER_CONTEXT = """\
[build-up assistant mode: research_first]
build-up is a source-grounded deep-research agent first and a personal study coach
second. Papers, PDFs, and arXiv links are primary research sources.

Operational rules:
- For an explicit deep-research request, use the deep_research workflow. Preserve
  the one-inference contract and never claim its stages are independent model sessions.
- Keep sources, atomic notes, critical synthesis, citation audit, and final report
  as separate artifacts. Never invent a source or unsupported citation.
- When a study session is active, follow its Socratic rules and let the learner
  attempt an explanation before completing it for them.
- Resolve ambiguous "this paper / abstract / summarize / translate / explain"
  requests against the active paper whenever one is selected.
- For arXiv URLs, arXiv IDs, PDF URLs, or new paper files, prefer
  load_paper_source first. It creates the durable library bundle:
  paper.pdf, metadata.json, paper.paper.json, summary.md, evidence.json,
  review.md, notes.md, and memory.md.
- For active-paper files or sections, use paper-specific tools. Do not switch
  to generic file reads unless the user explicitly asks for a non-paper file.
- Keep claim, evidence, interpretation, limitation, and background knowledge
  visibly separate.
- Be skeptical in a useful way: check method, assumptions, baselines, metrics,
  ablations, limitations, and reproducibility signals before praising a paper.
- Do not invent paper content. If the paper does not state something, say so.
- A strong answer usually starts with a one-line verdict, then covers the core
  problem, contribution, method reconstruction, evidence quality, important
  numbers, limitations, and what a senior researcher would verify next.
"""

DAYTIME_ASSISTANT_CONTEXT = """\
[build-up assistant mode: daytime_assistant]
The user activated Daytime/Day Mode. In this mode, behave like a practical
personal assistant for everyday local tasks.

Operational rules:
- Prioritize calendars, files, notes, project organization, search, lightweight
  summaries, coding chores, and general Q&A.
- Keep answers concise and action-oriented unless the user asks for depth.
- Use the normal build-up safety checks before writes, shell commands, git, or
  external actions.
- For paper/PDF requests, still use paper tools when clearly requested, but do
  not over-expand ordinary tasks into deep research reviews.
- When the user asks for an action, distinguish what build-up actually did from
  what it can recommend next.
"""


_RESEARCH_ON_ALIASES = {
    "리서치",
    "리서치타임",
    "리서치모드",
    "리서치 타임",
    "리서치 모드",
    "research",
    "research time",
    "researchmode",
    "research mode",
}

_RESEARCH_OFF_ALIASES = {
    "리서치오프",
    "리서치 오프",
    "리서치모드 꺼줘",
    "리서치 모드 꺼줘",
    "리서치타임 꺼줘",
    "리서치 타임 꺼줘",
    "기본모드",
    "기본 모드",
    "일반모드",
    "일반 모드",
    "researchoff",
    "research off",
    "researchmode off",
    "research mode off",
    "research off",
    "research time off",
}

_DAYTIME_ALIASES = {
    "데이",
    "데이타임",
    "데이 타임",
    "데이모드",
    "데이 모드",
    "daytime",
    "day time",
    "daymode",
    "day mode",
}

_REVIEWER_ON_ALIASES = {
    "리뷰어모드",
    "리뷰어 모드",
    "딥리뷰",
    "딥 리뷰",
    "딥리뷰어",
    "reviewer mode",
    "reviewermode",
    "deep review",
}

_REVIEWER_OFF_ALIASES = {
    "리뷰어모드 꺼줘",
    "리뷰어 모드 꺼줘",
    "리뷰어오프",
    "리뷰어 오프",
    "딥리뷰 꺼줘",
    "reviewer off",
    "reviewer mode off",
    "deep review off",
}

_SESSION_NEW_ALIASES = {
    "새 대화", "새 대화 시작", "새 대화 시작해줘",
    "새 세션", "새 세션 시작", "새 세션 시작해줘",
}

_SESSION_LIST_ALIASES = {
    "대화 목록", "세션 목록", "최근 대화 목록", "지난 대화 목록",
    "대화 이어가기", "세션 이어가기",
}

_SESSION_PREVIOUS_ALIASES = {
    "이전 대화", "이전 대화 이어서", "이전 대화 이어줘",
    "이전 세션", "이전 세션 이어서", "이전 세션 이어줘",
}


# ============================================================
# Readline completion
# ============================================================

def setup_readline(cfg: BuildupConfig) -> None:
    """Configure readline with history and tab completion."""
    history_file = cfg.history_file

    try:
        readline.read_history_file(str(history_file))
    except (FileNotFoundError, OSError):
        pass

    readline.set_history_length(1000)

    import atexit
    atexit.register(readline.write_history_file, str(history_file))

    COMMANDS = [
        "데이타임", "데이모드", "리서치타임", "리서치모드",
        "daytime", "daymode", "research", "researchmode",
        "/help", "/exit", "/clear", "/mode ", "/intent-debug ", "/frame ",
        "/steer", "/steer list", "/steer profile ", "/steer set ", "/steer add ",
        "/steer clear", "/steering",
        "/paper", "/paper list", "/paper current", "/paper use ",
        "/papers", "/papers list",
        "/job new ", "/job use ", "/job rename ", "/job list", "/job current", "/job log", "/job summary",
        "/templates",
        "/import ", "/files", "/read ", "/readpdf ", "/rewrite ",
        "/diff ", "/trash ", "/export",
        "/history", "/history clear",
        "/search ", "/research ", "/research --no-wiki ", "/research list", "/research show ", "/translate ",
        "/wiki status", "/wiki add", "/wiki ask ", "/wiki review", "/wiki lint",
        "/wiki verify ", "/wiki reject ", "/wiki rollback ", "/wiki bind ",
        "/study start ", "/study list", "/study use ", "/study note ",
        "/study status", "/study close ", "/study review", "/study review done", "/study verify ",
        "/cal add ", "/cal list", "/cal today", "/cal delete ", "/cal export", "/cal import ",
        "/owui sync ", "/owui ask ",
        "/mode auto", "/mode fast", "/mode main", "/mode refine",
        "/shell ", "/git ", "/edit ", "/glob ", "/grep ",
        "/session", "/session list", "/session new",
        "/session load ", "/session archive ", "/session export ", "/session info",
        "/session rename ", "/session search ",
        "/sessions", "/new", "/resume ", "/title ", "/undo", "/retry", "/compact",
    ]

    def completer(text: str, state: int) -> Optional[str]:
        buf = readline.get_line_buffer()

        if buf.startswith("/job use "):
            try:
                jobs = [
                    d.name for d in cfg.workspace_dir.iterdir()
                    if d.is_dir() and not d.is_symlink()
                ]
                prefix = buf[len("/job use "):]
                matches = [j for j in jobs if j.startswith(prefix)]
            except OSError:
                matches = []
        elif any(buf.startswith(cmd) for cmd in ("/read ", "/readpdf ", "/translate ", "/trash ", "/rewrite ", "/diff ")):
            current_job = get_current_job(cfg, required=False)
            if current_job:
                base = cfg.workspace_dir / current_job
                cmd_end = buf.index(" ") + 1
                prefix = buf[cmd_end:]
                try:
                    candidates = [
                        str(p.relative_to(base))
                        for p in base.rglob("*")
                        if p.is_file() and not p.is_symlink()
                    ]
                    matches = [c for c in candidates if c.startswith(prefix)]
                except OSError:
                    matches = []
            else:
                matches = []
        elif any(buf.startswith(cmd) for cmd in ("/session load ", "/session delete ", "/session rename ")):
            cmd_end = buf.index(" ", buf.index(" ") + 1) + 1  # skip two words + space
            prefix = buf[cmd_end:]
            try:
                sessions = list_sessions(cfg)
                matches = [s.session_id for s in sessions if s.session_id.startswith(prefix)]
            except Exception:
                matches = []
        elif "--tpl " in buf:
            prefix = buf.split("--tpl ")[-1]
            tpls = list(list_templates(cfg).keys())
            matches = [t for t in tpls if t.startswith(prefix)]
        else:
            matches = [c for c in COMMANDS if c.startswith(text)]

        return matches[state] if state < len(matches) else None

    readline.set_completer(completer)
    readline.set_completer_delims(" \t")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


# ============================================================
# Interactive shell
# ============================================================

class InteractiveShell:
    def __init__(self, cfg: BuildupConfig, session: requests.Session, logger: logging.Logger):
        self.cfg = cfg
        self.session = session
        self.logger = logger
        self.mode = "auto"
        self.assistant_mode = "research"
        self.paper_reviewer_mode = False
        self.active_paper_id: Optional[str] = None
        self.active_study_id: Optional[str] = None
        self._last_wiki_claim_ids: List[str] = []
        self.steering = SteeringState()
        self.running = True
        self.history = ConversationHistory(max_turns=cfg.max_history_turns)
        self._classifier = IntentEmbedClassifier(session, cfg)
        self._exemplars = ExemplarIndex(session, cfg)

        # Session persistence
        self._session_id: str = new_session_id()
        self._session_created_at: Optional[str] = None
        self._session_job_id: Optional[str] = get_current_job(cfg, required=False)
        self._workspace_key: str = workspace_key_for(cfg, self._session_job_id)
        self._session_lease: Optional[SessionLease] = None
        self._session_kind: str = "conversation"
        self._pending_session_title: str = ""
        self._memory_snapshot: str = load_conversation_memory(
            cfg, workspace_key=self._workspace_key
        )
        self._last_memory_signature: str = ""
        self.history.set_on_change(self._autosave_session)

        self._commands: Dict[str, Callable[[str], None]] = {
            "/exit": self._cmd_exit,
            "/help": self._cmd_help,
            "/clear": self._cmd_clear,
            "/prefs": self._cmd_prefs,
            "/memory": self._cmd_memory,
            "/mem": self._cmd_memory,
            "/intent-debug": self._cmd_intent_debug,
            "/frame": self._cmd_intent_debug,
            "/steer": self._cmd_steer,
            "/steering": self._cmd_steer,
            "/english": self._cmd_english,
            "/skills": self._cmd_skills,
            "/paper": self._cmd_paper,
            "/papers": self._cmd_paper,
            "/mode": self._cmd_mode,
            "/job": self._cmd_job,
            "/templates": self._cmd_templates,
            "/import": self._cmd_import,
            "/files": self._cmd_files,
            "/read": self._cmd_read,
            "/readpdf": self._cmd_readpdf,
            "/rewrite": self._cmd_rewrite,
            "/diff": self._cmd_diff,
            "/trash": self._cmd_trash,
            "/export": self._cmd_export,
            "/history": self._cmd_history,
            "/search": self._cmd_search,
            "/research": self._cmd_research,
            "/wiki": self._cmd_wiki,
            "/translate": self._cmd_translate,
            "/study": self._cmd_study,
            "/cal": self._cmd_cal,
            "/owui": self._cmd_owui,
            "/sandbox": self._cmd_sandbox,
            "/shell": self._cmd_shell,
            "/git": self._cmd_git,
            "/edit": self._cmd_edit_file,
            "/glob": self._cmd_glob,
            "/grep": self._cmd_grep,
            "/session": self._cmd_session,
            "/sessions": self._cmd_sessions,
            "/new": self._cmd_new_session,
            "/resume": self._cmd_resume,
            "/title": self._cmd_title,
            "/undo": self._cmd_undo,
            "/retry": self._cmd_retry,
            "/compact": self._cmd_compact,
        }

    @property
    def current_job(self) -> Optional[str]:
        return get_current_job(self.cfg, required=False)

    def _prompt_context_label(self) -> Optional[str]:
        """Return the compact context shown in the prompt/header."""
        if self.assistant_mode != "research":
            current_job = self.current_job
            return job_display_name(current_job, self.cfg) if current_job else None
        if self.active_study_id and self._session_job_id:
            study = load_study(self.active_study_id, self._session_job_id, self.cfg)
            if study:
                return f"study: {study.topic}"
        if not self.active_paper_id:
            return job_display_name(self._session_job_id, self.cfg) if self._session_job_id else "research"
        entry = find_paper_entry(self.cfg, self.active_paper_id)
        if entry:
            return entry.title or entry.paper_id
        return self.active_paper_id

    def _latest_paper_marker(self) -> tuple[str, float]:
        entries = list_paper_library(self.cfg)
        if not entries:
            return "", 0.0
        latest = entries[0]
        return latest.paper_id, latest.updated_at

    def _looks_like_new_paper_source(self, text: str) -> bool:
        lowered = text.lower()
        if "arxiv.org/" in lowered or ".pdf" in lowered:
            return True
        if re.search(r"\barxiv\s*:\s*\d{4}\.\d{4,5}(?:v\d+)?\b", lowered):
            return True
        if re.search(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", lowered):
            return True
        return False

    def _maybe_switch_to_loaded_paper(
        self,
        user_input: str,
        before_marker: tuple[str, float],
    ) -> None:
        """After a build-up paper-source request, follow the newly touched paper."""
        if self.assistant_mode != "research":
            return
        if not self._looks_like_new_paper_source(user_input):
            return
        before_id, before_updated = before_marker
        entries = list_paper_library(self.cfg)
        if not entries:
            return
        latest = entries[0]
        if latest.paper_id == before_id and latest.updated_at <= before_updated:
            return
        if self.active_paper_id == latest.paper_id:
            return
        self.active_paper_id = latest.paper_id
        self._save_session_state()
        render_info(
            "Active Paper",
            "새 논문을 active paper로 전환했어.\n\n" + format_active_paper(self.cfg, latest.paper_id),
            "green",
        )

    def _prompt_text(self) -> Text:
        return build_prompt(
            self.mode,
            self._prompt_context_label(),
            self.cfg.search_provider,
            assistant_mode=self._assistant_mode_label(),
        )

    def _header_runtime_info(self) -> list[tuple[str, str]]:
        """Return startup facts derived from the active configuration and session."""
        if self.assistant_mode == "research":
            model = self.cfg.research_model
        elif self.mode == "fast":
            model = self.cfg.fast_model
        elif self.mode == "main":
            model = self.cfg.main_model
        elif self.mode == "refine":
            model = f"{self.cfg.fast_model} -> {self.cfg.main_model}"
        else:
            model = f"{self.cfg.fast_model} / {self.cfg.main_model}"

        facts = [
            ("model", model),
            ("search", self.cfg.search_provider),
            ("session", f"{self._session_kind} · {self._session_id[:8]}"),
        ]
        if self.assistant_mode == "research":
            reviewer_state = "active" if self.paper_reviewer_mode else "standby"
            facts.insert(1, ("reviewer", f"{self.cfg.reviewer_model} · {reviewer_state}"))
        return facts

    def _assistant_mode_label(self) -> str:
        if self.assistant_mode == "research" and self.paper_reviewer_mode:
            base = "research/reviewer"
        elif self.assistant_mode == "research":
            base = "research"
        else:
            base = self.assistant_mode
        if self.steering.active():
            return f"{base}+steer:{self.steering.label()}"
        return base

    def _apply_steering(self) -> None:
        set_runtime_steering(steering_prompt(self.steering))

    def _steering_context(self) -> str:
        prompt = steering_prompt(self.steering)
        if not prompt:
            return ""
        return "[build-up steering]\n" + prompt

    def _build_task_frame(self, text: str):
        frame = build_task_frame(text, self.cfg, self.current_job)
        if self.assistant_mode == "research" and self.active_paper_id:
            entry = find_paper_entry(self.cfg, self.active_paper_id)
            frame = bind_active_paper_target(
                frame,
                self.active_paper_id,
                paper_title=entry.title if entry else "",
            )
        return frame

    def _agent_result_model_label(self) -> str:
        """Human-facing label for agent result panels.

        Agent runs may use multiple models: planner, completion, tool-specific
        models, and in research mode the research/reviewer models. The panel title
        should not pretend there was always one single current model.
        """
        if self.assistant_mode == "research":
            if self.paper_reviewer_mode:
                return (
                    f"build-up research agent (model={self.cfg.research_model}, "
                    f"reviewer={self.cfg.reviewer_model})"
                )
            return f"build-up research agent (model={self.cfg.research_model})"
        if self.mode == "auto":
            return f"agent router (fast={self.cfg.fast_model}, main={self.cfg.main_model})"
        if self.mode == "fast":
            return self.cfg.fast_model
        if self.mode == "main":
            return self.cfg.main_model
        if self.mode == "refine":
            return f"{self.cfg.fast_model} -> {self.cfg.main_model}"
        return self.cfg.main_model

    def _assistant_mode_context(self, *, include_steering: bool = True) -> str:
        # Frozen at session start/resume so another session cannot mutate the
        # current prompt prefix midway through a conversation.
        durable_memory = self._memory_snapshot
        active_study = ""
        if self.active_study_id and self._session_job_id:
            info = load_study(self.active_study_id, self._session_job_id, self.cfg)
            if info:
                active_study = study_context(info, self.cfg)
        english_brief_rule = (
            "\n- Prefer Korean first, then a compact English Brief unless the"
            " user requests a single language."
            if english_brief_enabled()
            else ""
        )
        if self.assistant_mode == "research":
            context = RESEARCH_PAPER_READER_CONTEXT + english_brief_rule
            # The shelf is a browsing UI, not global prompt memory. Only an
            # explicitly selected paper may enter this session's context.
            if self.active_paper_id:
                context += "\n\n" + active_paper_context(self.cfg, self.active_paper_id)
            if durable_memory:
                context += "\n\n" + durable_memory
            if active_study:
                context += "\n\n" + active_study
            if self.paper_reviewer_mode:
                context += (
                    "\n[build-up reviewer mode]\n"
                    f"Reviewer mode is active. For load_paper_source, set params.reviewer_mode=true. "
                    f"Use reviewer_model only for the final senior review pass: {self.cfg.reviewer_model}.\n"
                    f"Use research_model for normal paper passes: {self.cfg.research_model}.\n"
                )
            steering = self._steering_context() if include_steering else ""
            return context + ("\n\n" + steering if steering else "")
        steering = self._steering_context() if include_steering else ""
        context = DAYTIME_ASSISTANT_CONTEXT + english_brief_rule + (
            "\n\n" + durable_memory if durable_memory else ""
        )
        if active_study:
            context += "\n\n" + active_study
        return context + ("\n\n" + steering if steering else "")

    def _handle_assistant_mode_toggle(self, user_input: str) -> bool:
        text = re.sub(r"\s+", " ", user_input.strip().lower())
        if text in _REVIEWER_OFF_ALIASES:
            self.paper_reviewer_mode = False
            self._save_session_state()
            render_info(
                "Reviewer Mode",
                "리뷰어 모드를 껐어. 리서치모드는 유지하고 일반 research model로 리뷰할게.",
                "yellow",
            )
            return True
        if text in _REVIEWER_ON_ALIASES:
            if self.assistant_mode != "research":
                render_info(
                    "Reviewer Mode",
                    "리뷰어 모드는 build-up의 리서치타임에서만 사용할 수 있어. "
                    "먼저 `리서치타임`을 입력해줘.",
                    "yellow",
                )
                return True
            self.paper_reviewer_mode = True
            self._save_session_state()
            render_info(
                "Reviewer Mode",
                f"리뷰어 모드를 켰어. 최종 senior review pass에만 {self.cfg.reviewer_model}을 사용할게.",
                "cyan",
            )
            return True
        if text in _DAYTIME_ALIASES or text in _RESEARCH_OFF_ALIASES:
            self.assistant_mode = "daytime"
            self.paper_reviewer_mode = False
            self._save_session_state()
            render_day_banner(
                self.cfg.fast_model,
                self.cfg.main_model,
                current_job=self.current_job,
            )
            render_info(
                "Daytime Mode",
                "데이타임을 켰어. 이제 일상 작업, 일정, 파일, 검색, 가벼운 요약 중심으로 도울게.",
                "green",
            )
            return True
        if text in _RESEARCH_ON_ALIASES:
            self.assistant_mode = "research"
            self._save_session_state()
            render_research_banner(
                self.cfg.research_model,
                self.cfg.reviewer_model,
                reviewer_active=self.paper_reviewer_mode,
                active_paper_id=self.active_paper_id,
            )
            render_info(
                PRODUCT_NAME,
                (
                    "딥 리서치를 1순위로, 그 결과를 이어받는 개인 공부를 2순위로 동작해.\n"
                    "`주제를 딥 리서치해줘` 또는 `주제를 공부 시작해줘`라고 바로 말하면 돼.\n"
                    "논문/PDF 번역과 paper shelf는 필요할 때 그대로 사용할 수 있어."
                ),
                "cyan",
            )
            if self.active_paper_id:
                render_info("Active Paper", format_active_paper(self.cfg, self.active_paper_id), "green")
            return True
        return False

    def _handle_session_shortcut(self, user_input: str) -> bool:
        """Handle common Korean session phrases without sending them to a model."""
        text = re.sub(r"\s+", " ", user_input.strip().lower())
        if text in _SESSION_NEW_ALIASES:
            self._cmd_session("/session new")
            return True
        if text in _SESSION_LIST_ALIASES:
            self._cmd_session("/session list")
            return True
        if text in _SESSION_PREVIOUS_ALIASES:
            self._resume_previous_session()
            return True
        if re.search(r"(?:대화|세션)", text) and re.search(r"(?:이어|계속|열어|불러)", text):
            number = re.search(r"\d+", text)
            if number:
                self._cmd_session(f"/session load {number.group(0)}")
            else:
                self._cmd_session("/session list")
            return True
        return False

    def _handle_paper_shortcut(self, user_input: str) -> bool:
        if self.assistant_mode != "research":
            return False
        text = re.sub(r"\s+", " ", user_input.strip())
        if re.fullmatch(r"(?:논문\s*)?\d+\s*번?", text):
            number = re.search(r"\d+", text)
            if number:
                return self._select_active_paper(number.group(0))
        if text.lower() in {"논문 목록", "paper list", "papers", "목록"}:
            render_info("Paper Shelf", format_paper_shelf(self.cfg), "cyan")
            return True
        return False

    def _handle_study_shortcut(self, user_input: str) -> bool:
        text = re.sub(r"\s+", " ", user_input.strip())
        lowered = text.lower()
        if lowered in {"공부 목록", "학습 목록", "study list"}:
            self._cmd_study("/study list")
            return True
        if lowered in {"공부 이어서", "학습 이어서", "study resume"}:
            self._cmd_study("/study use latest")
            return True
        if lowered in {"복습 목록", "오늘 복습", "study review"}:
            self._cmd_study("/study review")
            return True
        match = re.fullmatch(r"(.+?)(?:를|을)?\s*(?:공부|학습)\s*시작(?:해줘|하자|할래)?", text)
        if match and match.group(1).strip():
            self._cmd_study(f"/study start {match.group(1).strip()}")
            return True
        return False

    def _handle_wiki_shortcut(self, user_input: str) -> bool:
        """Route common Korean knowledge requests without an LLM call."""
        text = re.sub(r"\s+", " ", user_input.strip())
        lowered = text.lower()
        if re.fullmatch(
            r"(?:이|이번|방금|최근)?\s*(?:딥\s*)?리서치(?:를|을)?\s*"
            r"(?:지식|위키)(?:에|로)?\s*반영(?:해줘|해|해주세요)?[.!]?",
            text,
            re.I,
        ):
            self._cmd_wiki("/wiki add latest")
            return True
        if re.search(r"(?:위키|지식)\s*(?:상태)?\s*(?:점검|검사|lint)", text, re.I):
            self._cmd_wiki("/wiki lint")
            return True
        if lowered in {"충돌하는 주장만 보여줘", "충돌하는 지식 보여줘", "wiki review", "위키 검토"}:
            self._cmd_wiki("/wiki review")
            return True
        if re.search(r"(?:이\s*)?논문.*기존\s*지식.*충돌", text, re.I):
            self._cmd_wiki("/wiki review")
            return True
        if re.fullmatch(
            r"(?:이번|방금|최근)?\s*(?:리서치|연구)(?:는|를|를\s*)?\s*"
            r"(?:지식|위키)(?:에|로)?\s*반영(?:하지\s*마|하지마|취소해줘)[.!]?",
            text,
            re.I,
        ):
            self._cmd_wiki("/wiki rollback latest")
            return True
        match = re.fullmatch(
            r"내가\s+(.+?)(?:에\s*대해)?\s*지금까지\s*(?:뭘|무엇을)\s*"
            r"알고\s*있(?:지|어)?[?？]?",
            text,
            re.I,
        )
        if match:
            self._cmd_wiki(f"/wiki ask {match.group(1).strip()}")
            return True
        claim_match = re.search(r"\b(C[0-9A-Fa-f]{14})\b", text)
        if claim_match and re.search(r"(?:확인|검증)(?:했어|했다|했음|완료)", text):
            self._cmd_wiki(f"/wiki verify {claim_match.group(1).upper()}")
            return True
        if claim_match and re.search(r"(?:잘못|틀렸|거짓|reject|기각)", text, re.I):
            self._cmd_wiki(f"/wiki reject {claim_match.group(1).upper()}")
            return True
        if len(self._last_wiki_claim_ids) == 1 and re.fullmatch(
            r"(?:이\s*)?주장은?\s*(?:내가\s*)?(?:확인|검증)(?:했어|했다|했음)[.!]?",
            text,
        ):
            self._cmd_wiki("/wiki verify")
            return True
        if len(self._last_wiki_claim_ids) == 1 and re.fullmatch(
            r"(?:이건|이\s*주장은?)\s*(?:잘못|틀렸|거짓).*[.!]?",
            text,
        ):
            self._cmd_wiki(f"/wiki reject {self._last_wiki_claim_ids[0]}")
            return True
        return False

    def _select_active_paper(self, identifier: str) -> bool:
        entry = find_paper_entry(self.cfg, identifier)
        if not entry:
            render_info("Paper", f"논문을 찾지 못했어: {identifier}", "yellow")
            return True
        self.active_paper_id = entry.paper_id
        self._save_session_state()
        render_info("Active Paper", format_active_paper(self.cfg, entry.paper_id), "green")
        return True

    def run(self) -> None:
        setup_readline(self.cfg)

        # ── Resume only inside the current workspace ──────────────────────
        restored_session: Optional[SessionInfo] = None
        last = load_latest_session(self.cfg, workspace_key=self._workspace_key)
        if last and (last.messages or last.active_study_id) and self._acquire_session_lease(last.session_id):
            self._restore_session(last)
            mark_session_status(last.session_id, "active", self.cfg)
            restored_session = last
        else:
            self._acquire_session_lease(self._session_id)

        render_header(
            self.mode,
            self._prompt_context_label(),
            self.history.turn_count,
            self._assistant_mode_label(),
            runtime_info=self._header_runtime_info(),
        )
        if restored_session:
            render_previous_conversation(
                restored_session.title,
                restored_session.messages,
                turn_count=restored_session.turn_count,
                updated_at=restored_session.updated_at,
            )

        if not check_ollama(self.session, self.cfg):
            render_info(
                "Warning",
                "Cannot connect to Ollama. Make sure 'ollama serve' is running.",
                "yellow",
            )
        self._classifier.init()
        self._exemplars.build()

        set_english_brief(self.cfg.english_brief)

        # ── Load user preferences (~/.buildup_prefs.md) ───────────────────
        prefs = load_user_prefs(self.cfg)
        if prefs:
            set_user_prefs(prefs)
            console.print(f"[dim]Preferences loaded (~/.buildup_prefs.md  {len(prefs.splitlines())} lines)[/dim]")
        self._apply_steering()

        try:
            while self.running:
                try:
                    user_input = read_prompt(self._prompt_text()).strip()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[bold red]Goodbye.[/bold red]")
                    break
                if not user_input:
                    continue
                if self._handle_session_shortcut(user_input):
                    continue
                if self._handle_assistant_mode_toggle(user_input):
                    continue
                if self._handle_paper_shortcut(user_input):
                    continue
                if self._handle_wiki_shortcut(user_input):
                    continue
                if self._handle_study_shortcut(user_input):
                    continue
                self._dispatch(user_input)
        finally:
            self._save_session_state()
            if self.history.turn_count or self._session_kind != "conversation" or self.active_study_id:
                mark_session_status(self._session_id, "ended", self.cfg)
            self._release_session_lease()

    def _dispatch(self, user_input: str) -> None:
        cmd_key = user_input.split()[0] if user_input.startswith("/") else None

        if cmd_key and cmd_key in self._commands:
            try:
                self._commands[cmd_key](user_input)
            except Exception as exc:
                self.logger.exception("Command error: %s", user_input)
                render_info("Error", str(exc), "red")
            return

        if cmd_key and cmd_key.startswith("/"):
            render_info("Error", f"Unknown command: {cmd_key}\nType /help for available commands.", "red")
            return

        tracer = TraceRecorder(self.cfg, self.current_job, user_input)
        frame = self._build_task_frame(user_input)
        tracer.set_task_frame(frame)
        try:
            clarification = frame_clarification(frame)
            if clarification and frame.task_type != "chat":
                tracer.set_tier("frame")
                tracer.set_policy(False, "clarify")
                render_info("확인 필요", clarification, "yellow")
                tracer.flush("clarification_requested", success=False)
                console.print()
                return

            # ── Tier-0: pure chat → streaming answer (1 LLM call, no agent) ──
            if is_chat(user_input):
                tracer.set_tier("0")
                tracer.set_intent("chat")
                gen, model_name, used_mode = answer_query_stream(
                    user_input, self.mode, self.session, self.cfg,
                    self.logger, self.history,
                    assistant_context=self._assistant_mode_context(include_steering=False),
                )
                full = render_streaming_answer(gen, model_name, used_mode)
                console.print()
                tracer.flush(full, success=True)
                return

            # ── Tier-1: rule-matched or embedding-classified action ──────────
            intent = parse_intent_instant(user_input, self.cfg)
            if intent is not None:
                tracer.set_tier("1a")
            else:
                intent = self._classifier.classify_and_extract(user_input, self.cfg)
                if intent is not None:
                    tracer.set_tier("1b")

            if intent:
                intent = enrich_intent_with_frame(intent, frame)
                confidence = intent.get("confidence")
                action = intent.get("intent", "")
                tracer.set_intent(action, confidence)
                # write without content still needs agent to figure out what to write
                needs_agent = action == "write" and not intent.get("params", {}).get("content")
                if not needs_agent:
                    decision = evaluate_intent_policy(
                        intent, self.cfg, self.current_job, confidence=confidence
                    )
                    tracer.set_policy(decision.auto_execute, decision.category)
                    if decision.auto_execute:
                        self._execute_intent(intent)
                        tracer.flush(intent.get("description", action), success=True)
                    else:
                        desc = intent.get("description", action)
                        preview = format_execution_preview(frame, intent, decision.reason)
                        console.print(Panel(
                            preview,
                            title="Confirm", border_style="yellow",
                        ))
                        try:
                            ans = input().strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            ans = "n"
                        if ans in ("y", "yes", "예"):
                            self._execute_intent(intent)
                            tracer.flush(desc, success=True)
                        else:
                            render_info("Cancelled", "Action cancelled.", "yellow")
                            tracer.flush("cancelled", success=False)
                    console.print()
                    return

            # ── Tier-2: complex / multi-step → full agent ──
            tracer.set_tier("2")
            agent_started = perf_counter()
            paper_marker = self._latest_paper_marker()
            result = run_agent(
                user_input,
                self.cfg,
                self.session,
                self.logger,
                self.mode,
                self.history,
                render_status,
                tracer=tracer,
                exemplar_index=self._exemplars,
                task_frame=frame,
                assistant_context=self._assistant_mode_context(),
                active_paper_id=self.active_paper_id,
            )
            self._maybe_switch_to_loaded_paper(user_input, paper_marker)
            render_status(f"Agent completed in {perf_counter() - agent_started:.1f}s")
            if result:  # empty = already rendered by state machine
                render_answer(result, self._agent_result_model_label(), self.mode)
            console.print()
            tracer.flush(result, success=True)
            self._exemplars.rebuild_if_needed()
        except (ConnectionError, TimeoutError) as exc:
            render_info("Connection Error", str(exc), "red")
            tracer.flush(str(exc), success=False)
        except Exception as exc:
            self.logger.exception("Agent error")
            render_info("Error", str(exc), "red")
            tracer.flush(str(exc), success=False)

    def _execute_intent(self, intent: Dict[str, Any]) -> None:
        name = intent.get("intent", "")
        params = intent.get("params", {})

        try:
            if name == "job_new":
                jid, created = cmd_job_new(params.get("label"), self.cfg, params.get("template"))
                msg = f"New job: {jid}"
                if created:
                    msg += "\nTemplate:\n  " + "\n  ".join(created)
                render_info("Job", msg)

            elif name == "job_use":
                cmd_job_use(params.get("job_id", ""), self.cfg)
                render_info("Job", f"→ {params.get('job_id')}")

            elif name == "import":
                dst, _used_job = cmd_import(params.get("path", ""), self.current_job, self.cfg)
                render_info("Import", f"Copied\n{dst}")

            elif name == "write":
                jid = get_current_job(self.cfg, required=True)
                out_path = cmd_write(jid, params.get("relpath", ""), params.get("content", ""), self.cfg)
                render_info("Write", f"Saved: {out_path.name}")

            elif name == "read":
                jid = get_current_job(self.cfg, required=True)
                content = cmd_read(jid, params.get("relpath", ""), self.cfg)
                render_info("Read", content[:3000], "cyan")

            elif name == "rewrite":
                jid = get_current_job(self.cfg, required=True)
                out_path, diff_text = rewrite_file(
                    jid, params.get("relpath", ""), params.get("instruction", ""),
                    None, self.mode, self.session, self.cfg, self.logger,
                )
                render_info("Rewrite", f"Rewritten: {out_path}")
                if diff_text:
                    render_diff(diff_text)

            elif name == "trash":
                jid = get_current_job(self.cfg, required=True)
                base = job_dir(jid, self.cfg)
                target = ensure_within(base / params.get("relpath", ""), base)
                moved = move_to_trash(target, jid, self.cfg)
                render_info("Trash", f"Moved to trash\n{moved}")

            elif name == "export":
                jid = get_current_job(self.cfg, required=True)
                dest = resolve_path(params["dest"]) if params.get("dest") else None
                exported = export_job(jid, self.cfg, dest)
                render_info("Export", f"Exported\n{exported}")

            elif name == "search":
                query = params.get("query", "")
                self._cmd_search(f"/search {query}")

            elif name == "deep_search":
                query = params.get("query", "")
                save_to_file = params.get("save_to_file", False)
                self._cmd_deep_search(query, save_to_file=save_to_file)

            elif name == "deep_research":
                self._cmd_research(f"/research {params.get('query', '')}")

            elif name == "wiki_add":
                self._cmd_wiki(f"/wiki add {params.get('selector', 'latest')}")

            elif name == "wiki_ask":
                self._cmd_wiki(f"/wiki ask {params.get('query', '')}")

            elif name == "wiki_lint":
                self._cmd_wiki("/wiki lint")

            elif name == "wiki_review":
                self._cmd_wiki("/wiki review")

            elif name == "wiki_verify":
                self._cmd_wiki(f"/wiki verify {params.get('claim_id', '')}")

            elif name == "wiki_reject":
                self._cmd_wiki(f"/wiki reject {params.get('claim_id', '')}")

            elif name == "paper_translate":
                source = params.get("source", "")
                self._cmd_translate(f"/translate {source}".rstrip())

            elif name == "cal_add":
                ev = cal_add(
                    title=params.get("title", ""),
                    date_str=params.get("date", ""),
                    time_str=params.get("time"),
                    duration_min=int(params.get("duration_min", 60)),
                    note=params.get("note", ""),
                    cfg=self.cfg,
                )
                render_info("Calendar", f"Event added: {ev['title']} ({ev['date']} {ev.get('time','')})")

            elif name == "cal_list":
                if params.get("date_from") or params.get("date_to"):
                    events = cal_list(self.cfg, date_from=params.get("date_from"), date_to=params.get("date_to"))
                else:
                    events = cal_list(self.cfg, upcoming_days=7)
                render_info("Calendar", format_events(events), "cyan")

            elif name == "cal_today":
                events = cal_today(self.cfg)
                render_info("Today", format_events(events), "cyan")

            elif name == "cal_delete":
                removed = cal_delete(params.get("event_id", ""), self.cfg)
                if removed:
                    render_info("Calendar", f"Deleted: {removed['title']}")
                else:
                    render_info("Calendar", "Event not found.", "yellow")

            elif name == "cal_export_ics":
                ics_path = cal_export_ics(self.cfg)
                render_info("Calendar", f"ICS exported\n{ics_path}")

            elif name == "cal_import_ics":
                path = resolve_path(params.get("path", ""))
                count = cal_import_ics(path, self.cfg)
                render_info("Calendar", f"{count} event(s) imported")

            else:
                render_info("Error", f"Unknown intent: {name}", "red")

        except Exception as exc:
            self.logger.exception("Intent execution error: %s", name)
            render_info("Error", str(exc), "red")

    def _execute_multi_intents(self, intents: list) -> None:
        """Execute a list of intents sequentially, stopping on first error."""


        # Check if any step requires confirmation
        needs_confirm = []
        for i, intent in enumerate(intents):
            decision = evaluate_intent_policy(intent, self.cfg, self.current_job)
            needs_confirm.append((intent, decision))

        non_auto = [d for _, d in needs_confirm if not d.auto_execute]
        if non_auto:
            steps_desc = "\n".join(
                f"  {i+1}. {intent.get('description', intent.get('intent'))}"
                for i, (intent, _) in enumerate(needs_confirm)
            )
            console.print(Panel(
                f"[bold]{len(intents)}-step task[/bold]\n\n{steps_desc}\n\n"
                f"[dim]Some steps require confirmation.[/dim]",
                title="Confirm plan",
                border_style="yellow",
            ))
            try:
                confirm = input("[y/n] > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("[dim]Cancelled[/dim]")
                return
            if confirm not in {"y", "yes", "ㅛ", "네"}:
                console.print("[dim]Cancelled[/dim]")
                return

        for i, (intent, decision) in enumerate(needs_confirm):
            render_status(f"[{i+1}/{len(intents)}] {intent.get('description', intent.get('intent'))}...")
            try:
                self._execute_intent(intent)
            except Exception as exc:
                self.logger.exception("Multi-step error at step %d", i + 1)
                render_info("Error", f"[Step {i+1}] {exc}\nAborting remaining steps.", "red")
                return

    # --- New command handlers: shell, git, edit, glob, grep ---

    _ALLOWED_COMMANDS = ALLOWED_COMMANDS

    def _shell_safe(self, cmd: str) -> None:
        """Validate that *cmd* is one direct allowlisted process."""
        parse_command(cmd, allowed_commands=self._ALLOWED_COMMANDS)

    def _cmd_shell(self, user_input: str) -> None:
        """/shell CMD — run a shell command in the current job directory."""
        cmd = user_input[len("/shell"):].strip()
        if not cmd:
            render_info("Shell", "Usage: /shell CMD\nExample: /shell ls -la", "yellow")
            return

        try:
            self._shell_safe(cmd)
        except UnsafeCommandError as exc:
            render_info("Shell Blocked", str(exc), "red")
            return

        jid = get_current_job(self.cfg, required=False)
        cwd = job_dir(jid, self.cfg) if jid else self.cfg.base_dir
        render_info("Shell", f"$ {cmd}", "dim")
        try:
            completed = run_command(cmd, cwd, timeout=60)
            output = completed.output
            lines = output.splitlines()
            if len(lines) > 200:
                lines = [f"(output truncated to last 200 of {len(lines)} lines)"] + lines[-200:]
                output = "\n".join(lines)
            style = "green" if completed.returncode == 0 else "red"
            title = f"Shell [exit {completed.returncode}]"
            render_info(title, output or "(no output)", style)
            if jid:
                append_action_log(
                    jid, self.cfg, "shell",
                    f"exit={completed.returncode} cmd={cmd[:80]}"
                )
        except Exception as exc:
            render_info("Shell Error", str(exc), "red")

    def _cmd_git(self, user_input: str) -> None:
        """/git ARGS — run a git command in the current job directory."""
        args = user_input[len("/git"):].strip()
        if not args:
            render_info("Git", "Usage: /git ARGS\nExample: /git status", "yellow")
            return

        jid = get_current_job(self.cfg, required=False)
        cwd = self.cfg.workspace_dir / jid if jid else self.cfg.base_dir

        try:
            output, code = git_run(args, cwd, confirm_destructive=False)
        except RequiresConfirmation as exc:
            console.print(Panel(
                f"[bold yellow]{exc}[/bold yellow]\n\nProceed? (y/n)",
                title="Git Confirm", border_style="yellow",
            ))
            try:
                ans = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans not in ("y", "yes"):
                render_info("Git", "Cancelled", "yellow")
                return
            try:
                output, code = git_run(args, cwd, confirm_destructive=True)
            except Exception as exc2:
                render_info("Git Error", str(exc2), "red")
                return
        except Exception as exc:
            render_info("Git Error", str(exc), "red")
            return

        style = "green" if code == 0 else "red"
        render_info(f"Git [exit {code}]", output or "(no output)", style)
        if jid:
            append_action_log(jid, self.cfg, "git", f"exit={code} args={args[:80]}")

    def _cmd_edit_file(self, user_input: str) -> None:
        """/edit RELPATH :: old_string :: new_string"""
        body = user_input[len("/edit"):].strip()
        parts = body.split("::", 2)
        if len(parts) != 3:
            render_info(
                "Edit",
                "Usage: /edit RELPATH :: old_string :: new_string\n"
                "Example: /edit notes.md :: old content :: new content",
                "yellow",
            )
            return
        relpath, old_str, new_str = (p.strip() for p in parts)
        jid = get_current_job(self.cfg, required=True)
        try:
            diff = cmd_edit(jid, relpath, old_str, new_str, self.cfg)
            render_info("Edit", f"Edited: {relpath}")
            if diff:
                render_diff(diff)
        except Exception as exc:
            render_info("Edit Error", str(exc), "red")

    def _cmd_glob(self, user_input: str) -> None:
        """/glob PATTERN — find files matching a glob pattern in current job."""
        from buildup.paths import glob_job
        pattern = user_input[len("/glob"):].strip()
        if not pattern:
            render_info("Glob", "Usage: /glob PATTERN\nExample: /glob **/*.py", "yellow")
            return
        jid = get_current_job(self.cfg, required=True)
        base = self.cfg.workspace_dir / jid
        results = glob_job(base, pattern)
        body = "\n".join(results) if results else "(no matches)"
        render_info(f"Glob [{len(results)} matches]", body, "cyan")

    def _cmd_grep(self, user_input: str) -> None:
        """/grep PATTERN [-- PATH_GLOB] — search file contents in current job."""
        from buildup.paths import grep_job
        body = user_input[len("/grep"):].strip()
        path_glob = "**/*"
        if " -- " in body:
            body, path_glob = body.split(" -- ", 1)
            body, path_glob = body.strip(), path_glob.strip()
        pattern = body
        if not pattern:
            render_info("Grep", "Usage: /grep PATTERN\nExample: /grep def\\s+\\w+", "yellow")
            return
        jid = get_current_job(self.cfg, required=True)
        base = self.cfg.workspace_dir / jid
        try:
            results = grep_job(base, pattern, path_glob=path_glob)
        except ValueError as exc:
            render_info("Grep Error", str(exc), "red")
            return
        body_text = "\n".join(results) if results else "(no matches)"
        render_info(f"Grep [{len(results)} matches]", body_text, "cyan")

    # --- Command handlers ---

    def _cmd_exit(self, _: str) -> None:
        console.print("[bold red]Goodbye.[/bold red]")
        self.running = False

    def _cmd_help(self, _: str) -> None:
        render_info("Help", help_text(), "green")

    def _cmd_intent_debug(self, user_input: str) -> None:
        """Show research's natural-language task frame without executing it."""
        parts = user_input.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            render_info(
                "Intent Debug",
                "Usage: /intent-debug 자연어 명령\nExample: /intent-debug 방금 가져온 pdf 요약해줘",
                "yellow",
            )
            return

        text = parts[1].strip()
        frame = self._build_task_frame(text)
        intent = parse_intent_instant(text, self.cfg)
        intent_source = "rules" if intent else "none"

        if intent is None:
            try:
                intent = self._classifier.classify_and_extract(text, self.cfg)
                if intent:
                    intent_source = "embedding"
            except Exception as exc:
                self.logger.warning("intent-debug classifier failed: %s", exc)

        policy = None
        if intent:
            intent = enrich_intent_with_frame(intent, frame)
            try:
                decision = evaluate_intent_policy(
                    intent, self.cfg, self.current_job,
                    confidence=intent.get("confidence"),
                )
                policy = {
                    "auto_execute": decision.auto_execute,
                    "category": decision.category,
                    "reason": decision.reason,
                }
            except Exception as exc:
                policy = {"error": str(exc)}

        debug = {
            "assistant_mode": self.assistant_mode,
            "paper_reviewer_mode": self.paper_reviewer_mode,
            "active_paper_id": self.active_paper_id,
            "steering": {
                "label": self.steering.label(),
                "profile": self.steering.profile,
                "directives": list(self.steering.directives),
            },
            "task_frame": frame.to_dict(),
            "clarification": frame_clarification(frame),
            "skills": [
                {
                    "name": skill.name,
                    "id": skill.skill_id,
                    "score": score,
                    "scripts_disabled": skill.has_scripts,
                }
                for skill, score in select_skills(text, self.cfg, limit=5)
            ],
            "intent_source": intent_source,
            "intent": intent,
            "policy": policy,
        }
        import json
        render_info("Intent Debug", json.dumps(debug, ensure_ascii=False, indent=2), "cyan")

    def _cmd_english(self, user_input: str) -> None:
        """Toggle the English Brief companion section for this session."""
        parts = user_input.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        if arg in {"on", "켜기", "true", "1"}:
            set_english_brief(True)
        elif arg in {"off", "끄기", "false", "0"}:
            set_english_brief(False)
        elif arg in {"", "show", "status"}:
            pass
        else:
            render_info("English Brief", "사용법: /english [on|off]", "red")
            return
        state = "on" if english_brief_enabled() else "off"
        render_info(
            "English Brief",
            f"현재 상태: {state}\n"
            "이 세션에만 적용됩니다. 영구 설정은 BUILDUP_ENGLISH_BRIEF=true 를 쓰세요.",
            "cyan",
        )

    def _cmd_steer(self, user_input: str) -> None:
        """Set session-scoped steering directives."""
        parts = user_input.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else "show"

        try:
            if sub in {"show", "status"}:
                render_info("Steering", format_steering_status(self.steering), "cyan")
                return

            if sub in {"list", "profiles"}:
                render_info("Steering Profiles", format_profile_list(), "cyan")
                return

            if sub == "profile":
                if len(parts) < 3 or not parts[2].strip():
                    render_info("Steering", "Usage: /steer profile critical", "yellow")
                    return
                set_profile(self.steering, parts[2].strip())
                self._apply_steering()
                self._save_session_state()
                render_info("Steering", format_steering_status(self.steering), "green")
                return

            if sub == "set":
                if len(parts) < 3 or not parts[2].strip():
                    render_info("Steering", "Usage: /steer set 답변은 짧게, 단 리스크는 꼭 말해줘", "yellow")
                    return
                set_directives(self.steering, parts[2].strip())
                self._apply_steering()
                self._save_session_state()
                render_info("Steering", format_steering_status(self.steering), "green")
                return

            if sub == "add":
                if len(parts) < 3 or not parts[2].strip():
                    render_info("Steering", "Usage: /steer add 불확실하면 먼저 확인 질문해줘", "yellow")
                    return
                add_directive(self.steering, parts[2].strip())
                self._apply_steering()
                self._save_session_state()
                render_info("Steering", format_steering_status(self.steering), "green")
                return

            if sub in {"remove", "rm", "delete", "del"}:
                if len(parts) < 3 or not parts[2].strip().isdigit():
                    render_info("Steering", "Usage: /steer remove 1", "yellow")
                    return
                remove_directive(self.steering, int(parts[2].strip()))
                self._apply_steering()
                self._save_session_state()
                render_info("Steering", format_steering_status(self.steering), "green")
                return

            if sub in {"clear", "reset", "off"}:
                clear_steering(self.steering)
                self._apply_steering()
                self._save_session_state()
                render_info("Steering", "steering을 초기화했어.", "green")
                return

            # Handy shortcut: `/steer 답변 짧게` behaves like `/steer set ...`.
            body = user_input.split(maxsplit=1)[1] if len(user_input.split(maxsplit=1)) > 1 else ""
            if body:
                set_directives(self.steering, body)
                self._apply_steering()
                self._save_session_state()
                render_info("Steering", format_steering_status(self.steering), "green")
                return
        except ValueError as exc:
            render_info("Steering", str(exc), "yellow")
            return

        render_info(
            "Steering",
            "Subcommands: show / list / profile / set / add / remove / clear",
            "yellow",
        )

    def _cmd_skills(self, user_input: str) -> None:
        """Inspect installed Agent Skills without executing bundled scripts."""
        parts = user_input.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else "list"

        if sub in {"list", "ls"}:
            render_info("Skills", format_skill_list(load_skills(self.cfg)), "cyan")
            return

        if sub == "search":
            if len(parts) < 3 or not parts[2].strip():
                render_info("Skills", "Usage: /skills search 코드 리뷰", "yellow")
                return
            render_info(
                "Skill Search",
                format_skill_matches(select_skills(parts[2].strip(), self.cfg, limit=10)),
                "cyan",
            )
            return

        if sub == "show":
            if len(parts) < 3 or not parts[2].strip():
                render_info("Skills", "Usage: /skills show code-reviewer", "yellow")
                return
            skill = find_skill(parts[2].strip(), self.cfg)
            if not skill:
                render_info("Skills", f"Skill not found: {parts[2].strip()}", "yellow")
                return
            render_info("Skill", format_skill_detail(skill), "cyan")
            return

        if sub == "reindex":
            skills = rebuild_skill_index(self.cfg)
            render_info("Skills", f"Indexed {len(skills)} skills.", "green")
            return

        if sub == "audit":
            render_info("Skill Audit", format_skill_audit(load_skills(self.cfg)), "cyan")
            return

        # Handy shortcut: `/skills code review` behaves like search.
        query = user_input.split(maxsplit=1)[1] if len(user_input.split(maxsplit=1)) > 1 else ""
        render_info(
            "Skill Search",
            format_skill_matches(select_skills(query, self.cfg, limit=10)),
            "cyan",
        )

    def _cmd_paper(self, user_input: str) -> None:
        """Inspect or select the active research paper."""
        parts = user_input.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else "list"

        if sub in {"list", "ls"}:
            render_info("Paper Shelf", format_paper_shelf(self.cfg), "cyan")
            return

        if sub in {"current", "active"}:
            if not self.active_paper_id:
                render_info("Active Paper", "아직 선택된 논문이 없어. `/paper list` 후 `/paper use 1`처럼 골라줘.", "yellow")
                return
            render_info("Active Paper", format_active_paper(self.cfg, self.active_paper_id), "green")
            return

        if sub == "use":
            if len(parts) < 3 or not parts[2].strip():
                render_info("Paper", "Usage: /paper use 1  또는  /paper use <paper-id>", "yellow")
                return
            self._select_active_paper(parts[2].strip())
            return

        # Shortcut: `/paper 1` or `/papers <id>`.
        identifier = user_input.split(maxsplit=1)[1] if len(user_input.split(maxsplit=1)) > 1 else ""
        if identifier:
            self._select_active_paper(identifier)
            return
        render_info("Paper", "Subcommands: list / use / current", "yellow")

    def _cmd_prefs(self, _: str) -> None:
        """Show current user preferences and project context."""
        from pathlib import Path
        from buildup.prompts import get_user_prefs
        from buildup.context import load_job_prefs

        prefs_path = Path.home() / ".buildup_prefs.md"
        prefs = get_user_prefs()
        job_prefs = load_job_prefs(self.cfg)

        lines = []
        lines.append("[bold]User prefs[/bold]  (~/.buildup_prefs.md)")
        if prefs:
            lines.append(prefs)
        else:
            lines.append(f"[dim](없음 — {prefs_path} 를 만들어 선호 설정을 작성하세요)[/dim]")

        lines.append("")
        job_label = self.current_job or "(없음)"
        lines.append(f"[bold]Project context[/bold]  (workspace/{job_label}/.buildup.md)")
        if job_prefs:
            lines.append(job_prefs)
        else:
            lines.append("[dim](없음 — 현재 job 폴더에 .buildup.md 를 만들어 프로젝트 맥락을 작성하세요)[/dim]")

        lines.append("")
        lines.append("[dim]예시 ~/.buildup_prefs.md:[/dim]")
        lines.append("[dim]  - 결론을 먼저 쓰고 근거를 뒤에 붙이기[/dim]")
        lines.append("[dim]  - 코드 설명은 간결하게[/dim]")
        lines.append("[dim]  - 파일명은 kebab-case 사용[/dim]")

        render_info("Preferences", "\n".join(lines), "cyan")

    def _cmd_memory(self, user_input: str) -> None:
        """Inspect or manually add durable conversation memory."""
        parts = user_input.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else "show"

        if sub in {"show", "list", "ls"}:
            include_all = len(parts) > 2 and parts[2].strip() == "--all"
            render_info(
                "Conversation Memory",
                format_conversation_memory(
                    self.cfg,
                    workspace_key=self._workspace_key,
                    include_all=include_all,
                ),
                "cyan",
            )
            return

        if sub == "path":
            render_info("Conversation Memory", str(memory_markdown_path(self.cfg)), "cyan")
            return

        if sub == "add":
            if len(parts) < 3 or not parts[2].strip():
                render_info(
                    "Conversation Memory",
                    "Usage: /memory add [workspace|user] 기억할 내용",
                    "yellow",
                )
                return
            value = parts[2].strip()
            scope = "workspace"
            first, separator, remainder = value.partition(" ")
            if first.lower() in {"workspace", "user"} and separator:
                scope = first.lower()
                value = remainder.strip()
            entry = append_conversation_memory(
                self.cfg,
                value,
                "사용자가 수동으로 장기 메모리에 추가함.",
                session_id=self._session_id,
                job_id=self._session_job_id,
                workspace_key=self._workspace_key,
                scope=scope,
                manual=True,
            )
            if entry:
                render_info("Conversation Memory", f"저장했어.\n{entry['summary']}", "green")
            else:
                render_info("Conversation Memory", "이미 저장된 내용이거나 저장할 수 없는 내용이야.", "yellow")
            return

        render_info("Conversation Memory", "Subcommands: show [--all] / add [workspace|user] / path", "yellow")

    def _cmd_clear(self, _: str) -> None:
        self._start_new_session(announce=False)
        console.clear()
        render_header(
            self.mode,
            self._prompt_context_label(),
            0,
            self._assistant_mode_label(),
            runtime_info=self._header_runtime_info(),
        )

    def _cmd_mode(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=1)
        if len(parts) < 2:
            render_info("Mode", f"Current mode: {self.mode}\n/mode auto|fast|main|refine")
            return
        new_mode = parts[1].strip()
        if new_mode in {"auto", "fast", "main", "refine"}:
            self.mode = new_mode
            self._save_session_state()
            render_info("Mode", f"→ {self.mode}")
        else:
            render_info("Error", "Supported modes: auto, fast, main, refine", "red")

    def _cmd_job(self, user_input: str) -> None:
        parts = user_input.split()
        sub = parts[1] if len(parts) > 1 else ""

        if sub == "new":
            rest = parts[2:]
            template = None
            label_parts: List[str] = []
            i = 0
            while i < len(rest):
                if rest[i] == "--tpl" and i + 1 < len(rest):
                    template = rest[i + 1]
                    i += 2
                else:
                    label_parts.append(rest[i])
                    i += 1
            label = " ".join(label_parts) if label_parts else None
            jid, created = cmd_job_new(label, self.cfg, template)
            self._start_new_session(job_id=jid, announce=False)
            msg = f"New job: {jid}\n{job_dir(jid, self.cfg)}"
            if created:
                msg += f"\n\nTemplate applied ({template}):\n  " + "\n  ".join(created)
            render_info("Job", msg)

        elif sub == "use":
            if len(parts) < 3:
                raise ValueError("Usage: /job use JOB_ID")
            jid = parts[2].strip()
            path = cmd_job_use(jid, self.cfg)
            self._start_new_session(job_id=jid, announce=False)
            render_info("Job", f"→ {jid}\n{path}\n새 workspace 대화를 시작했어.")

        elif sub == "rename":
            if len(parts) < 3:
                raise ValueError("Usage: /job rename NEW_NAME")
            jid = get_current_job(self.cfg, required=True)
            display_name = cmd_job_rename(jid, " ".join(parts[2:]), self.cfg)
            render_info("Job", f"name: {display_name}\nid: {jid}")

        elif sub == "current":
            current = self.current_job
            if current:
                render_info("Job", f"current: {job_display_name(current, self.cfg)}\nid: {current}")
            else:
                render_info("Job", "current: -")

        elif sub == "list":
            jobs = cmd_job_list(self.cfg)
            current = self.current_job
            lines = [
                f"  {format_job_label(j, self.cfg)}{' ← current' if j == current else ''}"
                for j in jobs
            ]
            render_info("Jobs", "\n".join(lines) or "(no jobs)")

        elif sub == "log":
            jid = get_current_job(self.cfg, required=True)
            entries = read_action_log(jid, self.cfg)
            if not entries:
                render_info("Action Log", "(empty)")
                return
            table = Table(title=f"Action Log — {jid}", border_style="dim")
            table.add_column("time", style="dim", width=19)
            table.add_column("action", style="cyan", width=12)
            table.add_column("detail", style="white")
            for e in entries[-30:]:
                table.add_row(e.get("ts", ""), e.get("action", ""), e.get("detail", ""))
            console.print(table)

        elif sub == "summary":
            jid = get_current_job(self.cfg, required=True)
            entries = read_action_log(jid, self.cfg)
            if not entries:
                render_info("Summary", "(no action log)")
                return
            log_text = "\n".join(
                f"[{e.get('ts','')}] {e.get('action','')}: {e.get('detail','')}"
                for e in entries
            )
            render_status("Summarizing job actions...")
            gen = chat_stream(
                self.session, self.cfg, self.cfg.fast_model,
                [{"role": "system", "content": f"작업 이력을 간결하게 요약하라.{bilingual_clause()}"},
                 {"role": "user", "content": f"다음 작업 이력을 요약해줘:\n\n{log_text}"}],
                keep_alive="2m", logger=self.logger, think=True,
            )
            render_streaming_answer(gen, "Job Summary", "fast")

        else:
            render_info("Error", "Subcommands: new / use / rename / current / list / log / summary", "red")

    def _cmd_templates(self, _: str) -> None:
        tpls = list_templates(self.cfg)
        if not tpls:
            render_info("Templates", "(none)")
            return
        lines = [f"  {name:12s}  {desc}" for name, desc in sorted(tpls.items())]
        render_info("Templates", "Usage: /job new myproject --tpl paper\n\n" + "\n".join(lines))

    def _cmd_import(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=1)
        if len(parts) < 2:
            raise ValueError("Usage: /import PATH")
        dst, _used_job = cmd_import(parts[1].strip(), self.current_job, self.cfg)
        render_info("Import", f"Copied\n{dst}")

    def _cmd_files(self, _: str) -> None:
        jid = get_current_job(self.cfg, required=True)
        files = cmd_files(jid, self.cfg)
        body = "\n".join(files) if files else "(empty)"
        render_info("Files", body)

    def _cmd_read(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=1)
        if len(parts) < 2:
            raise ValueError("Usage: /read RELPATH")
        jid = get_current_job(self.cfg, required=True)
        relpath = parts[1].strip()
        content = cmd_read(jid, relpath, self.cfg)
        render_info(f"Read: {relpath}", content, "cyan")

    def _cmd_readpdf(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=1)
        if len(parts) < 2:
            raise ValueError("Usage: /readpdf RELPATH")
        jid = get_current_job(self.cfg, required=True)
        relpath = parts[1].strip()
        base = job_dir(jid, self.cfg)
        path = ensure_within(base / relpath, base)
        reject_symlink(path)
        if not path.exists():
            raise ValueError(f"File not found: {path}")
        content = read_pdf_file(path, self.cfg)
        if len(content) > 5000:
            content = content[:5000] + f"\n\n... (first 5,000 of {len(content):,} chars)"
        render_info(f"PDF: {relpath}", content, "cyan")

    def _cmd_rewrite(self, user_input: str) -> None:
        jid = get_current_job(self.cfg, required=True)
        payload = user_input[len("/rewrite "):]
        if " :: " not in payload:
            raise ValueError("Usage: /rewrite RELPATH :: instruction")
        relpath, instruction = payload.split(" :: ", 1)
        output_path, diff_text = rewrite_file(
            jid, relpath.strip(), instruction.strip(), None, self.mode,
            self.session, self.cfg, self.logger, show_diff=True,
        )
        render_info("Rewrite", f"Rewritten\n{output_path}")
        if diff_text:
            render_diff(diff_text)

    def _cmd_diff(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=2)
        if len(parts) < 3:
            raise ValueError("Usage: /diff FILE_A FILE_B")
        jid = get_current_job(self.cfg, required=True)
        base = job_dir(jid, self.cfg)
        path_a = ensure_within(base / parts[1].strip(), base)
        path_b = ensure_within(base / parts[2].strip(), base)
        for p in [path_a, path_b]:
            reject_symlink(p)
            if not p.exists() or not p.is_file():
                raise ValueError(f"File not found: {p}")
            if not is_text_file(p, self.cfg):
                raise ValueError(f"Only text files can be compared: {p.suffix}")
        text_a = read_text_file(path_a, self.cfg)
        text_b = read_text_file(path_b, self.cfg)
        diff_text = compute_diff(text_a, text_b, filename=path_a.name)
        render_diff(diff_text)

    def _cmd_trash(self, user_input: str) -> None:
        jid = get_current_job(self.cfg, required=True)
        parts = user_input.split(maxsplit=1)
        if len(parts) < 2:
            raise ValueError("Usage: /trash RELPATH")
        relpath = parts[1].strip()
        base = job_dir(jid, self.cfg)
        target = ensure_within(base / relpath, base)
        moved = move_to_trash(target, jid, self.cfg)
        append_action_log(jid, self.cfg, "trash", relpath)
        render_info("Trash", f"Moved to trash\n{moved}")

    def _cmd_export(self, user_input: str) -> None:
        jid = get_current_job(self.cfg, required=True)
        parts = user_input.split(maxsplit=1)
        dest = resolve_path(parts[1]) if len(parts) > 1 else None
        exported = export_job(jid, self.cfg, dest)
        append_action_log(jid, self.cfg, "export", str(exported))
        render_info("Export", f"Exported\n{exported}")

    def _cmd_history(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip() == "clear":
            # A cleared prompt must not erase the durable transcript of the
            # current session.  Start a fresh, isolated session instead.
            self._start_new_session()
            return
        text = self.history.summary_text()
        if not text:
            render_info("History", "(no history)")
            return
        render_info(f"History ({self.history.turn_count} turns)", text, "magenta")

    def _cmd_cal(self, user_input: str) -> None:
        parts = user_input.split()
        sub = parts[1] if len(parts) > 1 else ""

        if sub == "add":
            rest = parts[2:]
            if len(rest) < 2:
                raise ValueError("Usage: /cal add YYYY-MM-DD [HH:MM] title [-- note]")
            date_str = rest[0]
            idx = 1
            time_str = None
            if idx < len(rest) and re.match(r"^\d{1,2}:\d{2}$", rest[idx]):
                time_str = rest[idx]
                idx += 1
            remaining = " ".join(rest[idx:])
            if " -- " in remaining:
                title, note = remaining.split(" -- ", 1)
            else:
                title, note = remaining, ""
            if not title:
                raise ValueError("Event title is required.")
            ev = cal_add(title.strip(), date_str, time_str, 60, note.strip(), self.cfg)
            render_info("Calendar", f"Added: {ev['title']}  📅 {ev['date']} {ev.get('time','all-day')}  [{ev['id']}]")

        elif sub == "list":
            rest = parts[2:]
            if len(rest) == 2:
                events = cal_list(self.cfg, date_from=rest[0], date_to=rest[1])
            elif len(rest) == 1 and rest[0].isdigit():
                events = cal_list(self.cfg, upcoming_days=int(rest[0]))
            else:
                events = cal_list(self.cfg, upcoming_days=7)
            render_info("Calendar", format_events(events), "cyan")

        elif sub == "today":
            events = cal_today(self.cfg)
            render_info("Today", format_events(events), "cyan")

        elif sub == "delete":
            if len(parts) < 3:
                raise ValueError("Usage: /cal delete EVENT_ID")
            event_id = parts[2].strip()
            removed = cal_delete(event_id, self.cfg)
            if removed:
                render_info("Calendar", f"Deleted: {removed['title']} ({removed['date']})")
            else:
                render_info("Calendar", f"No event found with ID '{event_id}'", "yellow")

        elif sub == "export":
            ics_path = cal_export_ics(self.cfg)
            render_info("Calendar", f"ICS exported\n{ics_path}\n\nOpen this file in Apple Calendar to import.")

        elif sub == "import":
            if len(parts) < 3:
                raise ValueError("Usage: /cal import PATH.ics")
            ics_path = resolve_path(parts[2].strip())
            if not ics_path.exists():
                raise ValueError(f"File not found: {ics_path}")
            count = cal_import_ics(ics_path, self.cfg)
            render_info("Calendar", f"{count} event(s) imported")

        else:
            render_info("Error", "Subcommands: add / list / today / delete / export / import", "red")

    def _cmd_search(self, user_input: str) -> None:
        """Enhanced search: bilingual + deep synthesis with main model."""
        parts = user_input.split(maxsplit=1)
        if len(parts) < 2:
            raise ValueError("Usage: /search QUERY")
        query = parts[1].strip()

        render_status("Searching web sources...")
        search_started = perf_counter()
        results, engine = web_search(
            query, self.cfg, self.session,
            max_results=self.cfg.web_search_max_results,
            bilingual=True,
        )
        render_status(
            f"Search finished in {perf_counter() - search_started:.1f}s "
            f"({len(results)} results, engine={engine})"
        )
        formatted = format_search_results(results, engine)

        if not results:
            render_info("Search", f"No results for '{query}'", "yellow")
            return

        render_info("Search Results", formatted, "dim")

        # Use main model + structured synthesis prompt
        render_status("Synthesizing with main model...")
        synthesis_prompt = system_search_synthesis(query, formatted)
        synth_started = perf_counter()
        gen = chat_stream(
            self.session, self.cfg, self.cfg.main_model,
            [{"role": "system", "content": system_main()},
             {"role": "user", "content": synthesis_prompt}],
            keep_alive="10m", logger=self.logger, think=True,
        )
        result = render_streaming_answer(gen, f"Search ({engine}) + {self.cfg.main_model}", "main")
        render_status(f"Synthesis completed in {perf_counter() - synth_started:.1f}s")
        self.history.add("user", f"[web search] {query}")
        self.history.add("assistant", result)

    def _cmd_deep_search(self, query: str, save_to_file: bool = False) -> None:
        """Multi-step workflow: search → analyze → optionally save to file."""
        from datetime import datetime as _dt

        # Step 1: Search (more results, bilingual)
        render_status("🔍 [1/3] Searching web sources (bilingual)...")
        search_started = perf_counter()
        results, engine = web_search(
            query, self.cfg, self.session,
            max_results=8,
            bilingual=True,
        )
        render_status(
            f"Search: {perf_counter() - search_started:.1f}s "
            f"({len(results)} results, engine={engine})"
        )
        formatted = format_search_results(results, engine)

        if not results:
            render_info("Deep Search", f"No results for '{query}'", "yellow")
            return

        render_info("Search Results", formatted, "dim")

        # Step 2: Deep synthesis with main model
        render_status("🧠 [2/3] Analyzing with main model...")
        synthesis_prompt = system_search_synthesis(query, formatted)
        synth_started = perf_counter()
        gen = chat_stream(
            self.session, self.cfg, self.cfg.main_model,
            [{"role": "system", "content": system_main()},
             {"role": "user", "content": synthesis_prompt}],
            keep_alive="10m", logger=self.logger, think=True,
        )
        analysis = render_streaming_answer(gen, f"Deep Search ({engine}) + {self.cfg.main_model}", "main")
        render_status(f"Analysis: {perf_counter() - synth_started:.1f}s")

        self.history.add("user", f"[deep search] {query}")
        self.history.add("assistant", analysis)

        # Step 3: Optionally save to file
        if save_to_file:
            try:
                jid = get_current_job(self.cfg, required=True)
            except Exception:
                render_info(
                    "Info",
                    "An active job is required to save the file. Run /job new to create one.",
                    "yellow",
                )
                return

            render_status("📄 [3/3] Saving to file...")
            sources_text = "\n".join(
                f"- [{i+1}] {r['title']}: {r['href']}"
                for i, r in enumerate(results)
            )
            file_prompt = deep_search_file_prompt(
                title=query,
                content=analysis,
                sources=sources_text,
            )
            doc_content = chat(
                self.session, self.cfg, self.cfg.fast_model,
                [{"role": "system",
                  "content": "주어진 내용을 깔끔한 Markdown 문서로 정리하라. "
                             "추가 설명 없이 문서 내용만 반환하라."},
                 {"role": "user", "content": file_prompt}],
                keep_alive="2m", logger=self.logger,
            )

            base = job_dir(jid, self.cfg)
            timestamp = _dt.now().strftime("%Y%m%d-%H%M%S")
            safe_query = re.sub(r'[^\w\s-]', '', query)[:30].strip().replace(' ', '-')
            filename = f"research-{safe_query}-{timestamp}.md"
            filepath = base / filename

            # Sandbox: validate write permission
            from buildup.sandbox import require_write
            require_write(filepath, jid, self.cfg, context="deep_search save")

            filepath.write_text(doc_content, encoding="utf-8")

            append_action_log(jid, self.cfg, "deep_search", f"{query} → {filename}")
            render_info("File Saved", f"{filepath}\n\n{len(doc_content):,} chars", "green")

    def _cmd_research(self, user_input: str) -> None:
        """Run or inspect source-grounded research runs."""
        body = user_input.split(maxsplit=1)[1].strip() if len(user_input.split(maxsplit=1)) > 1 else ""
        ingest_knowledge: Optional[bool] = None
        if body.lower() == "--no-wiki":
            ingest_knowledge = False
            body = ""
        elif body.lower().startswith("--no-wiki "):
            ingest_knowledge = False
            body = body[len("--no-wiki "):].strip()
        if body.lower() in {"list", "ls", "목록"}:
            jid = get_current_job(self.cfg, required=True)
            render_info("Research Runs", format_research_runs(list_research_runs(jid, self.cfg)), "cyan")
            return
        if body.lower().startswith("show ") or body.lower().startswith("보기 "):
            jid = get_current_job(self.cfg, required=True)
            selector = body.split(maxsplit=1)[1].strip()
            run = resolve_research_run(selector, jid, self.cfg)
            if not run:
                render_info("Research Run", f"찾지 못했어: {selector}", "yellow")
                return
            run_dir = resolve_path(str(run["run_dir"]))
            report = run_dir / "06-report.md"
            guide = run_dir / "07-study-guide.md"
            text = report.read_text(encoding="utf-8") if report.is_file() else "보고서가 없습니다."
            render_answer(text, f"Research · {run.get('run_id', '')}", "research")
            render_info("Research Files", f"{run_dir}\nstudy guide: {guide if guide.exists() else '-'}", "green")
            return
        if body.lower().startswith(("eval ", "evaluate ", "평가 ")):
            jid = get_current_job(self.cfg, required=True)
            selector = body.split(maxsplit=1)[1].strip()
            evaluation = evaluate_research_run(selector, jid, self.cfg)
            render_info(
                "Research Evaluation",
                format_evaluation(evaluation),
                "green" if evaluation["passed"] else "yellow",
            )
            return
        resume_selector = ""
        if body.lower().startswith(("resume ", "이어 ", "재개 ")):
            resume_selector = body.split(maxsplit=1)[1].strip()
            body = ""
        if body.lower().startswith("new "):
            body = body.split(maxsplit=1)[1].strip()
        query = body
        if not query and not resume_selector:
            render_info(
                "Deep Research",
                "Usage: /research 조사할 주제 · /research resume 1 · "
                "/research list · /research show 1 · /research eval 1",
                "yellow",
            )
            return
        jid = self.current_job
        if not jid:
            if resume_selector:
                raise ValueError("research run을 재개하려면 먼저 해당 job을 선택하세요.")
            jid, _ = cmd_job_new(f"research-{query[:28]}", self.cfg, "research")
        # A research run owns a clean session. It may reference artifacts in
        # the same job, but it never inherits another conversation transcript.
        self._start_new_session(job_id=jid, announce=False)
        self._session_kind = "research"
        self._pending_session_title = (
            f"Research resume · {resume_selector}" if resume_selector else f"Research · {query[:68]}"
        )
        request_label = f"resume {resume_selector}" if resume_selector else query
        self.history.add("user", f"[deep research] {request_label}", kind="research")
        try:
            with StatusLine() as status_line:
                result = run_deep_research(
                    query,
                    self.cfg,
                    self.session,
                    self.logger,
                    job_id=jid,
                    status=status_line.update,
                    session_id=self._session_id,
                    workspace_key=self._workspace_key,
                    resume=resume_selector,
                    ingest_knowledge=ingest_knowledge,
                )
        except Exception as exc:
            self.history.add(
                "assistant",
                f"[research failed] {exc}",
                kind="research_failed",
            )
            raise
        render_answer(
            result.report,
            f"{result.model} · {result.model_calls} model calls · {result.rounds} rounds",
            "research",
        )
        render_info(
            "Research Files",
            f"{result.run_dir}\n{result.source_count} sources · "
            f"{result.evidence_count} evidence · coverage {result.coverage:.2f} · "
            "checkpointed artifacts · study guide ready",
            "green",
        )
        if result.knowledge_vault_path:
            render_info(
                "Knowledge",
                f"+{result.knowledge_claims_added} claims · "
                f"{result.knowledge_conflicts} conflicts · "
                f"{result.knowledge_invalid} invalid\n"
                f"proposal: {result.knowledge_proposal_id}\n"
                f"{result.knowledge_vault_path}",
                "green" if result.knowledge_invalid == 0 else "yellow",
            )
        elif result.knowledge_error:
            render_info(
                "Knowledge Warning",
                "Research는 정상 완료됐지만 파생 Wiki 반영에 실패했습니다.\n"
                + result.knowledge_error,
                "yellow",
            )
        self.history.add(
            "assistant", result.report,
            kind="research", research_run_id=result.run_dir.name,
            research_run_dir=str(result.run_dir),
        )

    def _cmd_wiki(self, user_input: str) -> None:
        """Manage the current job's append-only grounded Knowledge Vault."""
        body = user_input[len("/wiki"):].strip()
        parts = body.split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""
        jid = get_current_job(self.cfg, required=True)

        if sub in {"status", "상태"}:
            render_info(
                "Knowledge Vault",
                format_vault_summary(vault_summary(jid, self.cfg)),
                "cyan",
            )
            return
        if sub in {"add", "ingest", "반영"}:
            result = add_research_to_vault(rest or "latest", jid, self.cfg)
            render_info(
                "Knowledge Ingest",
                format_ingest_result(result),
                "green" if result.invalid == 0 else "yellow",
            )
            return
        if sub in {"ask", "query", "질문"}:
            if not rest:
                render_info("Knowledge", "Usage: /wiki ask 질문", "yellow")
                return
            hits = query_vault(rest, jid, self.cfg)
            self._last_wiki_claim_ids = [hit.claim_id for hit in hits]
            render_info("Knowledge", format_query_results(rest, hits), "cyan")
            return
        if sub in {"review", "검토"}:
            render_info(
                "Knowledge Review", review_vault(jid, self.cfg, rest), "cyan"
            )
            return
        if sub in {"lint", "점검", "검사"}:
            report = lint_vault(jid, self.cfg, repair_stale=True)
            render_info(
                "Knowledge Lint",
                format_lint_report(report),
                "green" if report.passed else "red",
            )
            return
        if sub in {"compile", "재생성"}:
            render_info(
                "Knowledge Compile", str(compile_vault(jid, self.cfg)), "green"
            )
            return
        if sub in {"verify", "확인", "검증"}:
            claim_id, _, reason = rest.partition(" ")
            if not claim_id and len(self._last_wiki_claim_ids) == 1:
                claim_id = self._last_wiki_claim_ids[0]
            if not claim_id:
                render_info("Knowledge", "Usage: /wiki verify CLAIM_ID [reason]", "yellow")
                return
            claim = verify_claim(
                claim_id.upper(),
                jid,
                self.cfg,
                reason=reason or "user directly checked this claim",
            )
            append_action_log(jid, self.cfg, "wiki_verify", claim["claim_id"])
            render_info(
                "Knowledge Verified",
                f"{claim['claim_id']}\n{escape(str(claim['text']))}",
                "green",
            )
            return
        if sub in {"reject", "기각", "거절"}:
            claim_id, _, reason = rest.partition(" ")
            if not claim_id:
                render_info("Knowledge", "Usage: /wiki reject CLAIM_ID [reason]", "yellow")
                return
            claim = reject_claim(
                claim_id.upper(), jid, self.cfg,
                reason=reason or "user rejected this claim",
            )
            append_action_log(jid, self.cfg, "wiki_reject", claim["claim_id"])
            render_info(
                "Knowledge Rejected",
                f"{claim['claim_id']}\n{escape(str(claim['text']))}",
                "yellow",
            )
            return
        if sub in {"rollback", "되돌리기"}:
            if not rest:
                render_info("Knowledge", "Usage: /wiki rollback PROPOSAL_ID", "yellow")
                return
            index = rollback_proposal(rest, jid, self.cfg)
            append_action_log(jid, self.cfg, "wiki_rollback", rest)
            render_info("Knowledge Rollback", f"append-only rollback 완료\n{index}", "green")
            return
        if sub in {"bind", "연결"}:
            if not rest:
                render_info("Knowledge", "Usage: /wiki bind VAULT_ID", "yellow")
                return
            console.print(Panel(
                f"job `{jid}`을 vault `{rest}`에 연결합니다. 다른 job과 지식이 공유될 수 있습니다. [y/N]",
                title="Confirm cross-vault bind",
                border_style="yellow",
            ))
            try:
                answer = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer not in {"y", "yes", "예"}:
                render_info("Cancelled", "vault bind를 취소했습니다.", "yellow")
                return
            store = bind_job_to_vault(jid, rest, self.cfg, confirmed=True)
            append_action_log(jid, self.cfg, "wiki_bind", store.vault_id)
            render_info("Knowledge Bind", f"{jid} → {store.vault_id}\n{store.root}", "green")
            return
        render_info(
            "Knowledge",
            "Usage: /wiki status|add|ask|review|lint|compile|verify|reject|rollback|bind",
            "yellow",
        )

    def _cmd_translate(self, user_input: str) -> None:
        """Translate a current-job or active-paper PDF into Korean Markdown."""
        source = user_input.split(maxsplit=1)[1].strip() if len(user_input.split(maxsplit=1)) > 1 else ""
        target = resolve_translation_target(
            source,
            self.cfg,
            current_job=self.current_job,
            active_paper_id=self.active_paper_id,
        )
        with StatusLine() as status_line:
            result = translate_paper_pdf(
                target,
                self.cfg,
                self.session,
                self.logger,
                status=status_line.update,
            )
        render_info(
            "Paper Translation",
            f"저장 완료: {result.output_path}\n"
            f"{result.page_count} pages · {result.model_calls} model call(s) · {result.model}",
            "green",
        )

    def _cmd_study(self, user_input: str) -> None:
        """Manage a project-scoped Socratic study session."""
        body = user_input[len("/study"):].strip()
        parts = body.split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in {"start", "new", "시작"}:
            if not rest:
                render_info("Study", "Usage: /study start 공부할 주제", "yellow")
                return
            jid = self.current_job
            if not jid:
                jid, _ = cmd_job_new(f"study-{rest[:24]}", self.cfg, "study")
            latest = latest_completed_research_run(jid, self.cfg)
            source = resolve_path(str(latest["run_dir"])) if latest else None
            # Study gets its own transcript while retaining a durable pointer
            # to the completed research artifacts.
            self._start_new_session(job_id=jid, announce=False)
            self._session_kind = "study"
            info = start_study(rest, jid, self.cfg, source_research=source)
            self.active_study_id = info.study_id
            self._pending_session_title = f"Study · {rest[:71]}"
            self._save_session_state()
            append_action_log(jid, self.cfg, "study_start", info.study_id)
            render_info(
                "Study Started",
                f"{info.topic}\n{info.path}\nnext: {info.next_action}\nreview: {', '.join(info.review_dates)}",
                "green",
            )
            return

        jid = get_current_job(self.cfg, required=True)
        if sub in {"list", "ls", "목록"}:
            render_info("Studies", format_study_list(list_studies(jid, self.cfg)), "cyan")
            return
        if sub in {"use", "resume", "이어", "열기"}:
            info = resolve_study(rest or "latest", jid, self.cfg)
            if not info:
                render_info("Study", f"공부 세션을 찾지 못했어: {rest}", "yellow")
                return
            if self._session_kind != "study" or self.active_study_id != info.study_id:
                self._start_new_session(job_id=jid, announce=False)
                self._session_kind = "study"
                self.active_study_id = info.study_id
                self._pending_session_title = f"Study · {info.topic[:71]}"
                self._save_session_state()
            render_info("Active Study", f"{info.topic}\nnext: {info.next_action}\n{info.path}", "green")
            return
        if sub in {"note", "기록"}:
            if not self.active_study_id:
                render_info("Study", "먼저 `/study start 주제` 또는 `/study use 번호`를 실행해줘.", "yellow")
                return
            info = load_study(self.active_study_id, jid, self.cfg)
            if not info:
                raise ValueError("활성 공부 세션의 파일을 찾지 못했습니다.")
            updated = append_study_note(info, rest, self.cfg)
            append_action_log(jid, self.cfg, "study_note", updated.study_id)
            render_info("Verified Note", f"저장했어. verified notes: {updated.verified_notes_count}\nnext: {updated.next_action}", "green")
            return
        if sub in {"verify", "claim-verify", "지식확인"}:
            if not self.active_study_id:
                render_info(
                    "Study",
                    "Study 검증 승격은 활성 공부 세션 안에서만 가능합니다. "
                    "먼저 `/study use 번호`를 실행해줘.",
                    "yellow",
                )
                return
            claim_id, _, reason = rest.partition(" ")
            if not claim_id:
                render_info("Study", "Usage: /study verify CLAIM_ID [reason]", "yellow")
                return
            info = load_study(self.active_study_id, jid, self.cfg)
            if not info:
                raise ValueError("활성 공부 세션의 파일을 찾지 못했습니다.")
            claim = verify_claim(
                claim_id.upper(),
                jid,
                self.cfg,
                reason=reason or "learner directly explained and checked this claim",
                via=f"study:{info.study_id}",
            )
            append_action_log(jid, self.cfg, "study_verify_claim", claim["claim_id"])
            render_info(
                "Study → Knowledge Verified",
                f"{claim['claim_id']}\n{escape(str(claim['text']))}",
                "green",
            )
            return
        if sub in {"close", "end", "종료"}:
            if not self.active_study_id:
                render_info("Study", "활성 공부 세션이 없어.", "yellow")
                return
            info = load_study(self.active_study_id, jid, self.cfg)
            if not info:
                raise ValueError("활성 공부 세션의 파일을 찾지 못했습니다.")
            closed = close_study(info, self.cfg, rest)
            append_action_log(jid, self.cfg, "study_close", closed.study_id)
            self.active_study_id = None
            self._save_session_state()
            render_info("Study Closed", f"{closed.topic}\nnext: {closed.next_action}", "green")
            return
        if sub in {"review", "복습"}:
            if rest.lower() in {"done", "complete", "완료"}:
                if not self.active_study_id:
                    render_info("Study Review", "먼저 `/study review 번호`로 복습할 주제를 열어줘.", "yellow")
                    return
                info = load_study(self.active_study_id, jid, self.cfg)
                if not info:
                    raise ValueError("활성 공부 세션의 파일을 찾지 못했습니다.")
                updated, review_date = complete_study_review(info, self.cfg)
                append_action_log(jid, self.cfg, "study_review", f"{updated.study_id}:{review_date}")
                render_info(
                    "Study Review",
                    f"{review_date} 회차 완료 · {updated.topic}\nnext: {updated.next_action}",
                    "green",
                )
                return
            if rest:
                info = resolve_study(rest, jid, self.cfg)
                if not info:
                    render_info("Study Review", f"공부 세션을 찾지 못했어: {rest}", "yellow")
                    return
                self._start_new_session(job_id=jid, announce=False)
                self._session_kind = "study"
                self.active_study_id = info.study_id
                self._pending_session_title = f"Review · {info.topic[:70]}"
                self._save_session_state()
                render_info(
                    "Study Review",
                    f"{info.topic}\n노트를 열기 전에 핵심 개념을 자료 없이 설명해줘.\n"
                    "설명·예제·한계를 확인한 뒤 `/study review done`으로 마쳐.",
                    "cyan",
                )
                return
            render_info("Due Reviews", format_study_list(due_studies(jid, self.cfg)), "cyan")
            return

        info = load_study(self.active_study_id, jid, self.cfg) if self.active_study_id else None
        if info:
            render_info(
                "Active Study",
                f"{info.topic} · {info.status}\nverified: {info.verified_notes_count}\n"
                f"next: {info.next_action}\n{info.path}",
                "cyan",
            )
        else:
            render_info(
                "Study",
                "활성 공부 세션이 없어. `/study start 주제`로 시작하거나 `/study list`를 확인해줘.",
                "yellow",
            )


    def _cmd_owui(self, user_input: str) -> None:
        parts = user_input.split(maxsplit=2)
        sub = parts[1] if len(parts) > 1 else ""

        if sub == "sync":
            jid = get_current_job(self.cfg, required=True)
            if len(parts) < 3:
                raise ValueError("Usage: /owui sync KBKEY")
            kb_key = parts[2].strip().lower()
            synced = sync_job_to_openwebui(jid, kb_key, self.session, self.cfg)
            body = "\n".join(f"{rel} -> {fid}" for rel, fid in synced) or "(no files)"
            append_action_log(jid, self.cfg, "owui_sync", kb_key)
            render_info("Open WebUI Sync", body)
        elif sub == "ask":
            payload = user_input[len("/owui ask "):]
            if " :: " not in payload:
                raise ValueError("Usage: /owui ask KBKEY :: question")
            kb_key, question = payload.split(" :: ", 1)
            kb_key = kb_key.strip().lower()
            kb_id = self.cfg.openwebui_kb_map.get(kb_key, "")
            if not kb_id:
                raise ValueError(f"Environment variable OPENWEBUI_KB_{kb_key.upper()} is not set.")
            # KB Q&A requires deep reasoning — always use main model
            resp = ask_openwebui_with_kb(question.strip(), self.cfg.main_model, kb_id, self.session, self.cfg)
            text = extract_chat_completion_text(resp.json())
            render_answer(text, f"Open WebUI KB:{kb_key}", self.mode)
        else:
            render_info("Error", "Subcommands: sync / ask", "red")

    def _cmd_sandbox(self, _: str) -> None:
        """Display the current sandbox policy."""
        from buildup.sandbox import describe_policy
        render_info("Sandbox Policy", describe_policy(), "green")

    # ── Session management ────────────────────────────────────────────────────

    def _save_session_state(self) -> None:
        """Persist messages together with the context that must not cross sessions."""
        messages = self.history.raw_messages()
        save_session(
            session_id=self._session_id,
            messages=messages,
            job_id=self._session_job_id,
            cfg=self.cfg,
            created_at=self._session_created_at,
            assistant_mode=self.assistant_mode,
            response_mode=self.mode,
            active_paper_id=self.active_paper_id,
            paper_reviewer_mode=self.paper_reviewer_mode,
            steering_profile=self.steering.profile,
            steering_directives=self.steering.directives,
            workspace_key=self._workspace_key,
            context_summary=self.history.context_summary,
            active_study_id=self.active_study_id,
            kind=self._session_kind,
            model_config={
                "fast": self.cfg.fast_model,
                "main": self.cfg.main_model,
                "research": self.cfg.research_model,
                "reviewer": self.cfg.reviewer_model,
            },
            persist_empty=self._session_kind != "conversation" or bool(self.active_study_id),
        )
        if (messages or self._session_kind != "conversation" or self.active_study_id) and self._pending_session_title:
            if rename_session(self._session_id, self._pending_session_title, self.cfg):
                self._pending_session_title = ""

    def _autosave_session(self) -> None:
        """Auto-save current conversation after every message. Called by history hook."""
        self._save_session_state()
        messages = self.history.raw_messages()
        self._maybe_autosave_memory(messages)

    def _maybe_autosave_memory(self, messages: List[Dict[str, Any]]) -> None:
        """Persist important user/assistant pairs into durable memory."""
        if len(messages) < 2:
            return
        user_msg, assistant_msg = messages[-2], messages[-1]
        if user_msg.get("role") != "user" or assistant_msg.get("role") != "assistant":
            return
        signature = f"{hash(user_msg.get('content', ''))}:{hash(assistant_msg.get('content', ''))}"
        if signature == self._last_memory_signature:
            return
        entry = append_conversation_memory(
            self.cfg,
            user_msg.get("content", ""),
            assistant_msg.get("content", ""),
            session_id=self._session_id,
            job_id=self._session_job_id,
            workspace_key=self._workspace_key,
            scope="workspace",
        )
        if entry:
            self._last_memory_signature = signature

    def _restore_session(self, info: SessionInfo) -> str:
        """Restore one session and clear context that belonged to another one."""
        self._session_id = info.session_id
        self._session_created_at = info.created_at
        self._session_job_id = info.job_id
        self._session_kind = info.kind or "conversation"
        self._workspace_key = info.workspace_key or workspace_key_for(self.cfg, info.job_id)
        self.history.load_messages(info.messages)
        self.history.set_context_summary(info.context_summary)
        self.mode = info.response_mode if info.response_mode in {"auto", "fast", "main", "refine"} else "auto"
        self.assistant_mode = info.assistant_mode if info.assistant_mode in {"daytime", "research"} else "research"
        self.paper_reviewer_mode = bool(info.paper_reviewer_mode and self.assistant_mode == "research")
        self.active_paper_id = info.active_paper_id
        if self.active_paper_id and not find_paper_entry(self.cfg, self.active_paper_id):
            self.active_paper_id = None
        self.active_study_id = info.active_study_id
        if self.active_study_id and (
            not info.job_id or not load_study(self.active_study_id, info.job_id, self.cfg)
        ):
            self.active_study_id = None

        self.steering = SteeringState()
        try:
            set_profile(self.steering, info.steering_profile)
        except ValueError:
            pass
        self.steering.directives = list(info.steering_directives)
        self._apply_steering()
        self._last_memory_signature = ""
        self._pending_session_title = ""
        self._memory_snapshot = load_conversation_memory(
            self.cfg, workspace_key=self._workspace_key
        )

        if info.job_id:
            try:
                cmd_job_use(info.job_id, self.cfg)
                return f"job: {info.job_id}"
            except ValueError:
                return f"job: {info.job_id} (더 이상 존재하지 않음)"
        return "job: -"

    def _acquire_session_lease(self, session_id: str) -> bool:
        lease = SessionLease(self.cfg, session_id)
        if not lease.acquire():
            return False
        self._release_session_lease()
        self._session_lease = lease
        return True

    def _release_session_lease(self) -> None:
        if self._session_lease is not None:
            self._session_lease.release()
            self._session_lease = None

    def _session_number(self, session_id: str) -> Optional[int]:
        for index, info in enumerate(
            list_sessions(self.cfg, workspace_key=self._workspace_key), start=1
        ):
            if info.session_id == session_id:
                return index
        return None

    def _load_session_selector(self, selector: str) -> None:
        info = resolve_session(selector, self.cfg, workspace_key=self._workspace_key)
        if info is None:
            render_info("Session", f"대화를 찾지 못했어: {selector}\n`/sessions`에서 번호를 확인해줘.", "red")
            return
        if info.session_id == self._session_id:
            render_info("Session", f"이미 이 대화를 사용 중이야: {info.title}", "cyan")
            return

        # Acquire the destination before releasing the current lease.  This
        # prevents a failed resume from leaving the active session unlocked,
        # while the save below still happens under its original lease.
        target_lease = SessionLease(self.cfg, info.session_id)
        if not target_lease.acquire():
            render_info(
                "Session",
                "다른 build-up 프로세스가 이 대화를 사용 중이야. "
                "그 프로세스를 종료한 뒤 다시 시도해줘.",
                "yellow",
            )
            return
        previous_id = self._session_id
        self._save_session_state()
        if self.history.turn_count or self._session_kind != "conversation" or self.active_study_id:
            mark_session_status(previous_id, "ended", self.cfg)
        self._release_session_lease()
        self._session_lease = target_lease
        context_note = self._restore_session(info)
        mark_session_status(info.session_id, "active", self.cfg)
        number = self._session_number(info.session_id)
        label = f"#{number}" if number is not None else info.session_id[:8]
        render_info(
            "Session",
            f"{label} 대화를 이어갈게.\n"
            f"제목: {info.title}\n"
            f"대화: {info.turn_count}턴 · 최근 저장: {info.updated_at.replace('T', ' ')[:16]}\n"
            f"컨텍스트: {context_note}",
            "cyan",
        )
        render_previous_conversation(
            info.title,
            info.messages,
            turn_count=info.turn_count,
            updated_at=info.updated_at,
        )

    def _resume_previous_session(self) -> None:
        sessions = list_sessions(self.cfg, workspace_key=self._workspace_key)
        if not sessions:
            render_info("Session", "저장된 대화가 없어.", "yellow")
            return
        current_index = next(
            (index for index, info in enumerate(sessions) if info.session_id == self._session_id),
            None,
        )
        if current_index is None:
            target = sessions[0]
        elif current_index + 1 < len(sessions):
            target = sessions[current_index + 1]
        else:
            render_info("Session", "이보다 이전 대화는 없어.", "yellow")
            return
        self._load_session_selector(target.session_id)

    def _start_new_session(self, *, job_id: Optional[str] = None, announce: bool = True) -> None:
        previous_id = self._session_id
        self._save_session_state()
        if self.history.turn_count or self._session_kind != "conversation" or self.active_study_id:
            mark_session_status(previous_id, "ended", self.cfg)
        self._release_session_lease()
        self._session_id = new_session_id()
        self._session_created_at = None
        self._session_kind = "conversation"
        self._session_job_id = self.current_job if job_id is None else job_id
        self._workspace_key = workspace_key_for(self.cfg, self._session_job_id)
        self._acquire_session_lease(self._session_id)
        self.history.clear()
        self.mode = "auto"
        self.assistant_mode = "research"
        self.paper_reviewer_mode = False
        self.active_paper_id = None
        self.active_study_id = None
        self.steering = SteeringState()
        self._apply_steering()
        self._last_memory_signature = ""
        self._pending_session_title = ""
        self._memory_snapshot = load_conversation_memory(
            self.cfg, workspace_key=self._workspace_key
        )
        if announce:
            render_info(
                "Session",
                "새 대화를 시작했어. workspace는 유지하고 대화·논문·공부 컨텍스트는 분리했어.",
                "green",
            )

    def _cmd_sessions(self, user_input: str) -> None:
        rest = user_input[len("/sessions"):].strip()
        if rest:
            self._cmd_session(f"/session search {rest}")
        else:
            self._cmd_session("/session list")

    def _cmd_new_session(self, _: str) -> None:
        self._start_new_session()

    def _cmd_resume(self, user_input: str) -> None:
        selector = user_input.split(maxsplit=1)[1].strip() if len(user_input.split(maxsplit=1)) > 1 else ""
        if not selector:
            self._cmd_session("/session list")
            return
        if selector.lower() in {"previous", "prev", "이전"}:
            self._resume_previous_session()
            return
        self._load_session_selector(selector)

    def _cmd_session(self, user_input: str) -> None:
        """Workspace-scoped session browser, search, archive, and export."""
        body = user_input[len("/session"):].strip()
        parts = body.split(maxsplit=1)
        sub = parts[0].lower() if parts else "info"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in {"list", "ls"}:
            show_all = rest == "--all"
            key = None if show_all else self._workspace_key
            sessions = list_sessions(self.cfg, workspace_key=key)
            if not sessions:
                render_info("Sessions", "이 workspace에 저장된 대화가 없어. `/new`로 시작하면 돼.")
                return
            title = "모든 workspace 대화" if show_all else f"최근 대화 · {workspace_label(self._workspace_key)}"
            table = Table(title=title, border_style="dim")
            table.add_column("#", style="bold cyan", width=3, justify="right")
            table.add_column("현재", width=4, justify="center")
            table.add_column("제목")
            table.add_column("종류", style="dim", width=9)
            if show_all:
                table.add_column("workspace", style="dim")
            table.add_column("턴", width=4, justify="right")
            table.add_column("최근 저장", style="dim", width=16)
            table.add_column("ID", style="dim", width=8)
            for index, info in enumerate(sessions, start=1):
                row = [
                    str(index),
                    "●" if info.session_id == self._session_id else "",
                    info.title[:48],
                    info.kind,
                ]
                if show_all:
                    row.append(workspace_label(info.workspace_key))
                row.extend([
                    str(info.turn_count),
                    info.updated_at.replace("T", " ")[:16],
                    info.session_id[:8],
                ])
                table.add_row(*row)
            console.print(table)

        elif sub == "search":
            show_all = rest.startswith("--all ")
            query = rest[6:].strip() if show_all else rest
            if not query:
                render_info("Session Search", "사용법: `/sessions 검색어` 또는 `/session search 검색어`", "yellow")
                return
            hits = search_sessions(
                query,
                self.cfg,
                workspace_key=None if show_all else self._workspace_key,
            )
            if not hits:
                render_info("Session Search", f"검색 결과가 없어: {query}", "yellow")
                return
            lines = []
            for index, hit in enumerate(hits, start=1):
                lines.append(
                    f"{index}. {hit.session.title} [{hit.session.session_id[:8]}]\n"
                    f"   {workspace_label(hit.session.workspace_key)} · {hit.snippet}"
                )
            render_info("Session Search", "\n".join(lines), "cyan")

        elif sub in {"load", "use", "resume"}:
            if not rest:
                render_info("Session", "사용법: `/resume 2` 또는 `/session load 2`", "yellow")
                return
            self._load_session_selector(rest)

        elif sub == "new":
            self._start_new_session()

        elif sub in {"archive", "delete"}:
            if not rest:
                render_info("Session", "사용법: `/session archive 2`", "yellow")
                return
            info = resolve_session(rest, self.cfg, workspace_key=self._workspace_key)
            if info is None:
                render_info("Session", f"대화를 찾지 못했어: {rest}", "red")
                return
            if info.session_id == self._session_id:
                render_info("Session", "현재 대화는 보관할 수 없어. 먼저 `/new`를 입력해줘.", "red")
                return
            if archive_session(info.session_id, self.cfg):
                render_info("Session", f"보관했어. 데이터는 삭제되지 않았어: {info.title}")
            else:
                render_info("Session", f"Session not found: {rest}", "red")

        elif sub == "rename":
            if not rest or " " not in rest:
                render_info("Session", "사용법: `/session rename 2 새 제목`", "yellow")
                return
            sid, new_title = rest.split(maxsplit=1)
            info = resolve_session(sid, self.cfg, workspace_key=self._workspace_key)
            if info and rename_session(info.session_id, new_title, self.cfg):
                render_info("Session", f"이름을 바꿨어: {new_title[:60]}")
            else:
                render_info("Session", "대화를 찾지 못했거나 같은 제목이 이미 있어.", "red")

        elif sub == "export":
            export_parts = rest.split()
            if not export_parts:
                render_info("Session", "사용법: `/session export 2 [md|json]`", "yellow")
                return
            selector = export_parts[0]
            fmt = export_parts[1] if len(export_parts) > 1 else "md"
            info = resolve_session(selector, self.cfg, workspace_key=self._workspace_key)
            if not info:
                render_info("Session", f"대화를 찾지 못했어: {selector}", "red")
                return
            target = export_session(info.session_id, self.cfg, fmt=fmt)
            render_info("Session Export", str(target), "green")

        elif sub.isdigit():
            self._load_session_selector(sub)

        elif sub == "info":
            current = load_session(self._session_id, self.cfg)
            render_info(
                "Session",
                f"현재 대화: {(current.title if current else '새 대화')}\n"
                f"대화: {self.history.turn_count}턴 · 종류: {self._session_kind} · 모드: {self._assistant_mode_label()}\n"
                f"workspace: {workspace_label(self._workspace_key)}\n"
                f"job: {self._session_job_id or '-'}\n\n"
                f"/new                    새 대화\n"
                f"/sessions               현재 workspace 대화\n"
                f"/sessions 검색어         대화 검색\n"
                f"/resume latest|제목|ID   대화 이어서",
                "cyan",
            )
        else:
            render_info("Session", "사용법: `/new` · `/sessions` · `/resume latest` · `/sessions 검색어`", "yellow")

    def _cmd_title(self, user_input: str) -> None:
        title = user_input[len("/title"):].strip()
        if not title:
            current = load_session(self._session_id, self.cfg)
            render_info("Session Title", current.title if current else self._pending_session_title or "(아직 제목 없음)", "cyan")
            return
        if not self.history.raw_messages():
            self._pending_session_title = title[:100]
            render_info("Session Title", f"첫 메시지와 함께 저장할게: {self._pending_session_title}", "green")
            return
        if rename_session(self._session_id, title, self.cfg):
            render_info("Session Title", f"→ {title[:100]}", "green")
        else:
            render_info("Session Title", "같은 workspace에 동일한 제목이 있어.", "yellow")

    def _cmd_undo(self, _: str) -> None:
        removed = self.history.pop_last_turn()
        if removed is None:
            render_info("Undo", "되돌릴 대화가 없어.", "yellow")
            return
        render_info(
            "Undo",
            "마지막 대화 turn만 제거했어. 파일이나 도구 실행 결과는 되돌리지 않았어.\n"
            f"제거된 요청: {removed[:180]}",
            "green",
        )

    def _cmd_retry(self, _: str) -> None:
        user_text = next(
            (
                str(message.get("content") or "")
                for message in reversed(self.history.raw_messages())
                if message.get("role") == "user"
            ),
            "",
        )
        if not user_text:
            render_info("Retry", "다시 실행할 대화가 없어.", "yellow")
            return
        if user_text.startswith("["):
            render_info("Retry", "research·search 같은 workflow는 원래 명령으로 다시 실행해줘.", "yellow")
            return
        self.history.pop_last_turn()
        self._dispatch(user_text)

    def _cmd_compact(self, _: str) -> None:
        messages = self.history.raw_messages()
        keep = self.cfg.max_history_turns * 2
        older = messages[:-keep] if len(messages) > keep else []
        if not older:
            render_info("Compact", "아직 context window를 넘지 않았어. 전체 기록은 계속 보존 중이야.", "cyan")
            return
        transcript = "\n".join(
            f"[{message.get('role')}] {str(message.get('content') or '')}"
            for message in older
        )[-24_000:]
        render_status("오래된 대화를 한 번의 local model call로 압축하는 중...")
        summary = chat(
            self.session,
            self.cfg,
            self.cfg.research_model,
            [
                {"role": "system", "content": "대화의 결정, 근거, 미해결 질문, 다음 행동만 충실하게 요약하라. 새로운 정보를 추가하지 마라."},
                {"role": "user", "content": transcript},
            ],
            keep_alive="10m",
            logger=self.logger,
        )
        self.history.set_context_summary(summary)
        self._save_session_state()
        render_info("Compact", "전체 transcript는 보존하고 active prompt용 summary만 갱신했어.", "green")
