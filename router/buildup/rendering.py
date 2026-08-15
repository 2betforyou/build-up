"""Rich console rendering: panels, headers, diffs, answers."""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from io import StringIO
from typing import Any, Iterator, Optional, Sequence, Tuple

try:
    from pyfiglet import Figlet
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("ERROR: build-up dependency가 필요합니다.  python -m pip install -e .", file=sys.stderr)
    raise SystemExit(1)

from buildup import PRODUCT_NAME

# Shared console instance — import this from other modules.
console = Console()

# Terminal palette
HEADER_BORDER = "grey50"
HEADER_DIM = "grey70"
HEADER_LABEL = "bold"
HEADER_BANNER = "bold white"
DAY_BORDER = "green"
DAY_BANNER = "bold green"
DAY_LABEL = "bold green"
BUILDUP_BORDER = "white"
BUILDUP_BANNER = "bold white"
BUILDUP_LABEL = "bold white"

STATUS_STYLE = "dim"
PROMPT_NAME = "bold cyan"
PROMPT_ASSISTANT = "cyan"
PROMPT_MODE = "bold yellow"
PROMPT_META = "grey50"
PROMPT_MARKER = "bold cyan"

# Header animation
HEADER_ANIMATE = True
HEADER_DELAY = 0.02

_BANNER_RENDERER = Figlet(font="ansi_shadow", width=200)
_ANSI_STYLE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _render_banner_word(word: str) -> list[str]:
    """Render any word at runtime in the original block font."""
    normalized = re.sub(r"\s+", " ", word).strip().upper()
    if not normalized:
        return []
    return [line.rstrip() for line in _BANNER_RENDERER.renderText(normalized).splitlines() if line.rstrip()]


def _weekday_name(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%A").upper()


def _ascii_banner_lines(now: Optional[datetime] = None) -> list[str]:
    """Return today's weekday as a block-letter banner."""
    return _render_banner_word(_weekday_name(now))


def _ascii_research_banner_lines() -> list[str]:
    """Return today's weekday plus the build-up product mark."""
    weekday = _ascii_banner_lines()
    product = _render_banner_word("BUILD-UP")
    combined = [
        f"{weekday_line}  {product_line}"
        for weekday_line, product_line in zip(weekday, product)
    ]
    if max(len(line) for line in combined) <= max(40, console.width - 4):
        return combined
    return weekday + [""] + product


def _assistant_visuals(assistant_mode: str) -> tuple[str, str, str, str]:
    """Return title, border, banner, and label styles for the active assistant mode."""
    label = assistant_mode.lower()
    if "research" in label:
        return f" {PRODUCT_NAME} ", BUILDUP_BORDER, BUILDUP_BANNER, BUILDUP_LABEL
    return f" {PRODUCT_NAME} ", DAY_BORDER, DAY_BANNER, DAY_LABEL


def _render_boxed_lines(
    title: str,
    content_lines: list[tuple[str, str]],
    *,
    delay: float = 0.0,
    border_style: str = HEADER_BORDER,
) -> None:
    """Render plain text lines inside build-up's terminal box style."""
    inner_width = max(40, console.width - 4)
    top = "╭─" + title + "─" * max(0, inner_width - len(title) + 1) + "╮"
    bottom = "╰" + "─" * (inner_width + 2) + "╯"

    rendered: list[Text] = [Text(top, style=border_style)]
    for raw, style in content_lines:
        padded = _pad_line(raw, inner_width)
        line = Text()
        line.append("│", style=border_style)
        line.append(" ", style=border_style)
        line.append(padded, style=style)
        line.append(" ", style=border_style)
        line.append("│", style=border_style)
        rendered.append(line)
    rendered.append(Text(bottom, style=border_style))

    for idx, line in enumerate(rendered):
        console.print(line)
        if delay and idx < len(rendered) - 1:
            time.sleep(delay)


def _display_job_name(job: str | None, max_len: int = 32) -> str:
    """Return a readable job name for display in header/prompt."""
    if not job:
        return "-"

    name = re.sub(r"^\d{8}-\d{6}-", "", job)

    if len(name) > max_len:
        return name[: max_len - 3] + "..."
    return name


def _pad_line(text: str, width: int) -> str:
    """Pad or clip a plain string to fixed width."""
    if len(text) < width:
        return text + (" " * (width - len(text)))
    return text[:width]


def _build_header_lines(
    mode: str,
    current_job: Optional[str],
    history_turns: int = 0,
    assistant_mode: str = "research",
    *,
    runtime_info: Sequence[tuple[str, str]] = (),
    now: Optional[datetime] = None,
) -> list[Text]:
    """Build a header from the current date and caller-supplied runtime facts."""
    current_time = now or datetime.now().astimezone()
    timezone = current_time.tzname() or "local"
    job_str = _display_job_name(current_job, max_len=48)
    header_title, border_style, banner_style, label_style = _assistant_visuals(assistant_mode)
    assistant_display = assistant_mode
    context_label = "context" if "research" in assistant_mode.lower() else "job"
    facts = [
        ("assistant", assistant_display),
        ("mode", mode),
        (context_label, job_str),
        *((str(label), str(value)) for label, value in runtime_info if str(value).strip()),
        ("history", f"{history_turns} turns"),
        ("local time", f"{current_time.strftime('%Y-%m-%d %H:%M:%S')} {timezone}"),
    ]
    label_width = max(12, *(len(label) for label, _ in facts))

    content_lines: list[tuple[str, str]] = []
    content_lines.append(("", "default"))
    for line in _ascii_banner_lines(current_time):
        content_lines.append((line, banner_style))
    content_lines.append(("", "default"))
    for label, value in facts:
        style = label_style if label == "assistant" else "default"
        content_lines.append((f" {label:<{label_width}} {value}", style))
    content_lines.append(("", "default"))

    rendered: list[Text] = []
    inner_width = max(40, console.width - 4)
    top = "╭─" + header_title + "─" * max(0, inner_width - len(header_title) + 1) + "╮"
    bottom = "╰" + "─" * (inner_width + 2) + "╯"
    rendered.append(Text(top, style=border_style))

    for raw, style in content_lines:
        padded = _pad_line(raw, inner_width)
        line = Text()
        line.append("│", style=border_style)
        line.append(" ", style=border_style)
        line.append(padded, style=style)
        line.append(" ", style=border_style)
        line.append("│", style=border_style)
        rendered.append(line)

    rendered.append(Text(bottom, style=border_style))
    return rendered


def render_header(
    mode: str,
    current_job: Optional[str],
    history_turns: int = 0,
    assistant_mode: str = "research",
    *,
    runtime_info: Sequence[tuple[str, str]] = (),
) -> None:
    """Print the build-up session header line-by-line, like neofetch."""
    lines = _build_header_lines(
        mode,
        current_job,
        history_turns,
        assistant_mode,
        runtime_info=runtime_info,
    )
    for idx, line in enumerate(lines):
        console.print(line)
        if HEADER_ANIMATE and idx < len(lines) - 1:
            time.sleep(HEADER_DELAY)


def render_research_banner(
    research_model: str,
    reviewer_model: str,
    *,
    reviewer_active: bool = False,
    active_paper_id: str | None = None,
) -> None:
    """Print a dedicated build-up research-mode activation banner."""
    reviewer_state = "armed" if reviewer_active else "standby"
    active_paper = active_paper_id or "-"

    content_lines: list[tuple[str, str]] = [("", "default")]
    for line in _ascii_research_banner_lines():
        content_lines.append((line, BUILDUP_BANNER))
    content_lines.extend(
        [
            ("", "default"),
            (" phase         RESEARCH", BUILDUP_LABEL),
            (" workspace     deep research cockpit", BUILDUP_LABEL),
            (" focus         cited research -> personal study", "default"),
            (f" research model {research_model}", "default"),
            (f" reviewer      {reviewer_model} ({reviewer_state})", "default"),
            (f" active paper  {_display_job_name(active_paper, max_len=56)}", "default"),
            (" sessions      workspace-scoped research / study transcripts", "default"),
            ("", "default"),
            (" build-up commands", HEADER_DIM),
            (" /research   /wiki   /study   /sessions   /translate   daytime", "default"),
            ("", "default"),
        ]
    )
    _render_boxed_lines(
        f" {PRODUCT_NAME} ",
        content_lines,
        delay=HEADER_DELAY if HEADER_ANIMATE else 0.0,
        border_style=BUILDUP_BORDER,
    )


def render_day_banner(
    fast_model: str,
    main_model: str,
    *,
    current_job: str | None = None,
) -> None:
    """Print a dedicated build-up everyday-mode activation banner."""
    job_str = _display_job_name(current_job, max_len=56)
    content_lines: list[tuple[str, str]] = [("", "default")]
    for line in _ascii_banner_lines():
        content_lines.append((line, DAY_BANNER))
    content_lines.extend(
        [
            ("", "default"),
            (" phase         DAYTIME", DAY_LABEL),
            (" workspace     everyday command desk", DAY_LABEL),
            (" focus         files / calendar / search / lightweight work", "default"),
            (f" fast model    {fast_model}", "default"),
            (f" main model    {main_model}", "default"),
            (f" active job    {job_str}", "default"),
            ("", "default"),
            (" day commands", HEADER_DIM),
            (" /files   /cal today   /search   /job list   research", "default"),
            ("", "default"),
        ]
    )
    _render_boxed_lines(
        f" {PRODUCT_NAME} ",
        content_lines,
        delay=HEADER_DELAY if HEADER_ANIMATE else 0.0,
        border_style=DAY_BORDER,
    )


def build_prompt(
    mode: str,
    current_job: Optional[str],
    search_provider: str = "",
    assistant_mode: str = "research",
) -> Text:
    """Build an English-only prompt without workspace or user-content labels."""
    now_str = datetime.now().strftime("%H:%M")
    steer_suffix = ""
    if "+steer:" in assistant_mode:
        assistant_mode, steer_suffix = assistant_mode.split("+steer:", 1)
    assistant_label = assistant_mode.lower()
    if "research" in assistant_label:
        assistant = "research/reviewer" if "reviewer" in assistant_label else "research"
    else:
        assistant = "daytime"
    if steer_suffix:
        assistant = f"{assistant}/{steer_suffix}"

    prompt = Text()
    prompt.append(PRODUCT_NAME, style=PROMPT_NAME)
    prompt.append("  ", style=PROMPT_META)
    prompt.append(assistant, style=PROMPT_ASSISTANT)
    if mode != "auto":
        prompt.append(" / ", style=PROMPT_META)
        prompt.append(mode, style=PROMPT_MODE)
    prompt.append(f"  {now_str}  ", style=PROMPT_META)
    prompt.append("> ", style=PROMPT_MARKER)
    return prompt


def read_prompt(prompt: Text) -> str:
    """Read a line while keeping the styled prompt inside Readline's boundary."""
    if not console.color_system or console.no_color:
        return input(prompt.plain)

    buffer = StringIO()
    prompt_console = Console(
        file=buffer,
        color_system=console.color_system,
        force_terminal=True,
        no_color=False,
        width=console.width,
    )
    prompt_console.print(prompt, end="", soft_wrap=True)
    styled_prompt = _ANSI_STYLE_RE.sub(
        lambda match: f"\x01{match.group(0)}\x02",
        buffer.getvalue(),
    )
    return input(styled_prompt)


def render_status(message: str) -> None:
    """Render a dim status line with timestamp for long-running steps."""
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[{STATUS_STYLE}][{ts}] {message}[/{STATUS_STYLE}]")


# ── Think-tag helpers ────────────────────────────────────────────────────────

_THINK_OPEN_RE  = re.compile(r"<(?:think|thought|thinking)>",  re.I)
_THINK_CLOSE_RE = re.compile(r"</(?:think|thought|thinking)>", re.I)


def _split_thinking(text: str) -> Tuple[str, str]:
    """Split text with <think>...</think> into (thinking, answer).

    Returns (thinking_text, clean_answer).  Both stripped.
    """
    think_parts:  list[str] = []
    answer_parts: list[str] = []
    remaining = text
    in_think = False

    while remaining:
        if not in_think:
            m = _THINK_OPEN_RE.search(remaining)
            if m:
                answer_parts.append(remaining[:m.start()])
                remaining = remaining[m.end():]
                in_think = True
            else:
                answer_parts.append(remaining)
                break
        else:
            m = _THINK_CLOSE_RE.search(remaining)
            if m:
                think_parts.append(remaining[:m.start()])
                remaining = remaining[m.end():]
                in_think = False
            else:
                think_parts.append(remaining)   # unclosed tag — treat rest as thinking
                break

    return "".join(think_parts).strip(), "".join(answer_parts).strip()


def render_answer(result: str, model_name: str, mode: str) -> None:
    """Render an LLM response.

    If the response contains <think>...</think> blocks, display them first in
    a dim grey panel, then show only the actual final answer in the main panel.
    """
    thinking, answer = _split_thinking(result)
    title = f"{PRODUCT_NAME} [{mode}]  {model_name}"

    if thinking:
        try:
            console.print(Panel(Markdown(thinking), title="[grey50]thinking[/grey50]", border_style="grey50"))
        except Exception:
            console.print(Panel(Text(thinking, style="dim"), title="[grey50]thinking[/grey50]", border_style="grey50"))

    if answer:
        display = answer
    elif thinking:
        display = "모델이 최종 답변 없이 내부 추론만 반환했습니다. 같은 요청을 한 번 더 보내면 최종 답변만 다시 생성하겠습니다."
    else:
        display = result
    try:
        console.print(Panel(Markdown(display), title=title, border_style="cyan"))
    except Exception:
        console.print(Panel(display, title=title, border_style="cyan"))


def render_streaming_answer(
    stream_gen: Iterator[str],
    model_name: str,
    mode: str,  # included in Panel title
) -> str:
    """Stream LLM output with grey think blocks and a live final-answer panel.

    Returns the final answer text. If the model emits only thinking, the final
    panel never falls back to raw thinking; the caller may append a repair answer.
    """
    think_buf:  list[str] = []
    answer_buf: list[str] = []
    pending = ""
    in_think = False
    full_text = ""
    think_live: "Live | None" = None
    answer_live: "Live | None" = None
    showed_thinking = False
    streamed_answer = False
    title = f"{PRODUCT_NAME} [{mode}]  {model_name}"

    def _make_think_panel(content: str) -> Panel:
        return Panel(
            Text(content, style="dim grey50"),
            title="[grey50]thinking[/grey50]",
            border_style="grey50",
        )

    def _make_answer_panel(content: str) -> Panel:
        content = content or " "
        try:
            return Panel(Markdown(content), title=title, border_style="cyan")
        except Exception:
            return Panel(content, title=title, border_style="cyan")

    def _update_answer_panel() -> None:
        nonlocal answer_live, streamed_answer
        answer_text_live = "".join(answer_buf).strip()
        if not answer_text_live:
            return
        streamed_answer = True
        panel = _make_answer_panel(answer_text_live)
        if answer_live is None:
            answer_live = Live(
                panel,
                console=console,
                refresh_per_second=12,
                transient=True,
            )
            answer_live.start()
        else:
            answer_live.update(panel)

    for chunk in stream_gen:
        full_text += chunk
        pending  += chunk

        while True:
            if not in_think:
                m = _THINK_OPEN_RE.search(pending)
                if m:
                    before = pending[:m.start()]
                    if before:
                        answer_buf.append(before)
                        _update_answer_panel()
                    pending = pending[m.end():]
                    in_think = True
                    showed_thinking = True
                    if answer_live:
                        answer_live.stop()
                        answer_live = None
                    think_live = Live(_make_think_panel(""), console=console, refresh_per_second=12)
                    think_live.start()
                else:
                    # Safe to flush everything except last 20 chars
                    # (a partial opening tag might span the chunk boundary)
                    cutoff = max(0, len(pending) - 20)
                    if cutoff:
                        safe, pending = pending[:cutoff], pending[cutoff:]
                        answer_buf.append(safe)
                        _update_answer_panel()
                    break
            else:
                m = _THINK_CLOSE_RE.search(pending)
                if m:
                    think_chunk = pending[:m.start()]
                    if think_chunk:
                        think_buf.append(think_chunk)
                    if think_live:
                        think_live.update(_make_think_panel("".join(think_buf)))
                    pending = pending[m.end():]
                    in_think = False
                    if think_live:
                        think_live.stop()
                        think_live = None
                else:
                    cutoff = max(0, len(pending) - 20)
                    if cutoff:
                        safe, pending = pending[:cutoff], pending[cutoff:]
                        think_buf.append(safe)
                        if think_live:
                            think_live.update(_make_think_panel("".join(think_buf)))
                    break

    # Flush whatever remained
    if pending:
        if in_think:
            think_buf.append(pending)
            if think_live:
                think_live.update(_make_think_panel("".join(think_buf)))
        else:
            answer_buf.append(pending)
            _update_answer_panel()

    if think_live:
        think_live.stop()
        think_live = None

    if answer_live:
        answer_live.stop()
        answer_live = None

    if showed_thinking:
        console.print()  # spacing before answer panel after live thinking output

    answer_text = "".join(answer_buf).strip()
    display = answer_text
    if not display and _split_thinking(full_text)[0]:
        display = "모델이 최종 답변 없이 내부 추론만 반환했습니다. 같은 요청을 한 번 더 보내면 최종 답변만 다시 생성하겠습니다."
        answer_text = display
    if display:
        if streamed_answer:
            # The live display is transient to avoid duplicated partial panels.
            # Print one stable final panel after the streamed answer finishes.
            console.print(_make_answer_panel(display))
        else:
            console.print(_make_answer_panel(display))

    return answer_text


def render_info(title: str, body: str, style: str = "green") -> None:
    """Render an informational panel."""
    console.print(Panel(body, title=title, border_style=style))


def render_previous_conversation(
    title: str,
    messages: Sequence[dict[str, Any]],
    *,
    turn_count: int,
    updated_at: str = "",
    exchanges: int = 4,
) -> None:
    """Render a compact, role-aligned preview of a restored conversation."""
    visible = [
        message for message in messages
        if message.get("role") in {"user", "assistant"}
    ]
    selected = visible[-(exchanges * 2):]
    if not selected:
        return

    table = Table.grid(expand=True, padding=(0, 2))
    table.add_column(width=10, no_wrap=True)
    table.add_column(ratio=1)

    session_title = re.sub(r"\s+", " ", title).strip()[:68] or "untitled"
    turn_label = "turn" if turn_count == 1 else "turns"
    metadata = Text()
    metadata.append(session_title, style="bold white")
    metadata.append(f"  {turn_count} {turn_label}", style="dim")
    if updated_at:
        metadata.append(f"  updated {updated_at.replace('T', ' ')[:16]}", style="dim")
    table.add_row(Text("session", style="dim"), metadata)

    omitted = len(visible) - len(selected)
    if omitted:
        table.add_row("", Text(f"{omitted} earlier messages hidden", style="dim italic"))
    table.add_row("", "")

    for index, message in enumerate(selected):
        is_user = message.get("role") == "user"
        role = "you" if is_user else PRODUCT_NAME
        role_style = "bold white" if is_user else "bold cyan"
        content_style = "white" if is_user else "grey70"
        limit = 260 if is_user else 320
        content = re.sub(r"\s+", " ", str(message.get("content") or "")).strip()
        if len(content) > limit:
            content = content[: limit - 3].rstrip() + "..."
        table.add_row(Text(role, style=role_style), Text(content, style=content_style))
        if index < len(selected) - 1:
            table.add_row("", "")

    console.print(Panel(
        table,
        title=Text(" previous conversation ", style="bold"),
        subtitle=Text(" /new starts fresh   /sessions shows all ", style="dim"),
        border_style=HEADER_BORDER,
        padding=(1, 2),
    ))


def render_diff(diff_text: str) -> None:
    """Render a unified diff with syntax highlighting."""
    if not diff_text.strip():
        render_info("Diff", "(no changes)", "dim")
        return
    try:
        syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
        console.print(Panel(syntax, title="Diff", border_style="magenta"))
    except Exception:
        console.print(Panel(diff_text, title="Diff", border_style="magenta"))


def help_text() -> str:
    """Return the full help string."""
    return (
        "/help                              Show this help\n"
        "/exit                              Quit\n"
        "/clear                             Clear screen + start a new session\n"
        "/mode auto|fast|main|refine        Change response mode\n"
        "데이타임 / 데이모드                 Everyday assistant mode\n"
        "리서치타임 / 리서치모드             Deep-research-first mode\n"
        "/prefs                             Show user prefs & project context\n"
        "/memory show [--all]               Show scoped durable memory\n"
        "/memory add [workspace|user] TEXT  Add explicit durable memory\n"
        "/intent-debug TEXT                  Show natural-language task frame\n"
        "/steer [show|list|profile|set]      Runtime behavior steering\n"
        "/skills [list|search|show|audit]    Manage installed Agent Skills\n"
        "/paper list|use|current             build-up paper shelf / active paper\n"
        "\n"
        "── Job management ──\n"
        "/job new [name] [--tpl TPL]        Create job folder (template optional)\n"
        "/job use JOB_ID                    Switch to existing job folder\n"
        "/job rename NEW_NAME               Rename current job for display\n"
        "/job list                          List all jobs\n"
        "/job current                       Show current job\n"
        "/job log                           Show current job action log\n"
        "/job summary                       AI summary of job log\n"
        "/templates                         List available templates\n"
        "\n"
        "── File operations ──\n"
        "/import PATH                       Copy file/folder into current job\n"
        "/files                             List files in current job\n"
        "/read RELPATH                      View text file\n"
        "/readpdf RELPATH                   Extract text from PDF\n"
        "/rewrite RELPATH :: instruction    Rewrite file (shows diff)\n"
        "/diff FILE_A FILE_B                Compare two files\n"
        "/trash RELPATH                     Move file to trash\n"
        "/export [DEST]                     Export current job\n"
        "\n"
        "── Calendar ──\n"
        "/cal add DATE [TIME] title [-- note]  Add event\n"
        "/cal list [days|FROM TO]           List events (default 7 days)\n"
        "/cal today                         Today's events\n"
        "/cal delete ID                     Delete event\n"
        "/cal export                        Export ICS (Apple Calendar etc.)\n"
        "/cal import PATH.ics               Import ICS\n"
        "\n"
        "── Code / Shell / Git ──\n"
        "/shell CMD                         Run allowed shell command (ls, python, pytest, git…)\n"
        "/git ARGS                          Run git command (status, diff, add, commit, log…)\n"
        "/edit RELPATH :: OLD :: NEW        Precise string replacement in file (shows diff)\n"
        "/glob PATTERN                      Glob search in job folder (e.g. **/*.py)\n"
        "/grep PATTERN [-- PATH_GLOB]       Regex search in file contents\n"
        "\n"
        "── Deep research (primary) ──\n"
        "/research QUERY                    Adaptive evidence-first deep research\n"
        "/research --no-wiki QUERY          Run once without Knowledge ingestion\n"
        "/research resume NUMBER            Resume from a durable checkpoint\n"
        "/research list                     List research runs in this job\n"
        "/research show NUMBER              Open a report and its study guide\n"
        "/research eval NUMBER              Recompute offline quality gates\n"
        "/translate PDF|PAPER_ID             Translate paper PDF to Korean Markdown\n"
        "\n"
        "── Compounding knowledge (grounded Wiki) ──\n"
        "/wiki status                       Show current job vault state\n"
        "/wiki add [RUN]                    Ingest a sealed research run\n"
        "/wiki ask QUERY                    Query grounded claims with original citations\n"
        "/wiki review|lint                  Review conflicts/staleness or audit the vault\n"
        "/wiki verify CLAIM_ID              User-verify a supported claim\n"
        "/wiki reject CLAIM_ID              Reject without deleting history\n"
        "/wiki rollback PROPOSAL_ID         Append a non-destructive rollback event\n"
        "/wiki bind VAULT_ID                Explicitly share/bind a vault (confirmation)\n"
        "\n"
        "── Personal study (secondary) ──\n"
        "/study start TOPIC                 Start a Socratic study session\n"
        "/study list|use NUMBER             List/resume study sessions\n"
        "/study note TEXT                   Save a learner-verified note\n"
        "/study status|close|review         Status, close, or due reviews\n"
        "/study review NUMBER              Start retrieval-first review\n"
        "/study review done                Complete the current due review\n"
        "/study verify CLAIM_ID             Promote a directly checked claim to verified\n"
        "\n"
        "── Conversation / Search ──\n"
        "/history                           Show conversation history\n"
        "/history clear                     Preserve log and start a new session\n"
        "/search QUERY                      Web search + AI answer\n"
        "/undo                              Remove last chat turn only\n"
        "/retry                             Retry last ordinary chat turn\n"
        "/compact                           Summarize old active context (full log kept)\n"
        "\n"
        "── Open WebUI ──\n"
        "/owui sync KBKEY                   Upload job to KB\n"
        "/owui ask KBKEY :: question        Ask KB-based question\n"
        "\n"
        "── Session management ──\n"
        "/new                               Start a clean conversation\n"
        "/sessions                          List conversations in this workspace\n"
        "/sessions QUERY                    Full-text search conversation history\n"
        "/resume latest|TITLE|ID             Continue a conversation\n"
        "/resume prev                       Continue the previous conversation\n"
        "/title TITLE                       Name the current conversation\n"
        "/session rename NUMBER title       Rename a conversation\n"
        "/session archive NUMBER            Soft-hide a conversation\n"
        "/session export NUMBER [md|json]   Export a conversation\n"
        "Natural language: 새 대화 · 대화 목록 · 2번 대화 이어서\n"
        "\n"
        "You can also use natural language:\n"
        "   \"Schedule a meeting tomorrow at 2pm\"\n"
        "   \"Import draft.md and polish the writing style\"\n"
        "   \"Show this week's schedule\"\n"
        "   → build-up infers intent and confirms before any write actions.\n"
    )
