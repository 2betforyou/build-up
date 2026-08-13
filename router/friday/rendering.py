"""Rich console rendering: panels, headers, diffs, answers."""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from typing import Iterator, Optional, Tuple

try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text
except ImportError:
    print("ERROR: 'rich' 패키지가 필요합니다.  pip install rich", file=sys.stderr)
    raise SystemExit(1)

from friday import PRODUCT_NAME
from friday.prompts import WEEKDAYS_KO

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
NIGHT_BORDER = "white"
NIGHT_BANNER = "bold white"
NIGHT_LABEL = "bold white"

STATUS_STYLE = "dim"
PROMPT_BRACKET = "dim"
PROMPT_VALUE = "bold"

# Header animation
HEADER_ANIMATE = True
HEADER_DELAY = 0.02


_ASCII_BANNERS = {
    "MONDAY": (
        "███╗   ███╗ ██████╗ ███╗   ██╗██████╗  █████╗ ██╗   ██╗",
        "████╗ ████║██╔═══██╗████╗  ██║██╔══██╗██╔══██╗╚██╗ ██╔╝",
        "██╔████╔██║██║   ██║██╔██╗ ██║██║  ██║███████║ ╚████╔╝",
        "██║╚██╔╝██║██║   ██║██║╚██╗██║██║  ██║██╔══██║  ╚██╔╝",
        "██║ ╚═╝ ██║╚██████╔╝██║ ╚████║██████╔╝██║  ██║   ██║",
        "╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚═════╝ ╚═╝  ╚═╝   ╚═╝",
    ),
    "TUESDAY": (
        "████████╗██╗   ██╗███████╗███████╗██████╗  █████╗ ██╗   ██╗",
        "╚══██╔══╝██║   ██║██╔════╝██╔════╝██╔══██╗██╔══██╗╚██╗ ██╔╝",
        "   ██║   ██║   ██║█████╗  ███████╗██║  ██║███████║ ╚████╔╝",
        "   ██║   ██║   ██║██╔══╝  ╚════██║██║  ██║██╔══██║  ╚██╔╝",
        "   ██║   ╚██████╔╝███████╗███████║██████╔╝██║  ██║   ██║",
        "   ╚═╝    ╚═════╝ ╚══════╝╚══════╝╚═════╝ ╚═╝  ╚═╝   ╚═╝",
    ),
    "WEDNESDAY": (
        "██╗   ██╗███████╗██████╗ ███╗   ██╗███████╗███████╗██████╗  █████╗ ██╗   ██╗",
        "██║   ██║██╔════╝██╔══██╗████╗  ██║██╔════╝██╔════╝██╔══██╗██╔══██╗╚██╗ ██╔╝",
        "██║ █╗ ██║█████╗  ██║  ██║██╔██╗ ██║█████╗  ███████╗██║  ██║███████║ ╚████╔╝",
        "██║███╗██║██╔══╝  ██║  ██║██║╚██╗██║██╔══╝  ╚════██║██║  ██║██╔══██║  ╚██╔╝",
        "╚███╔███╔╝███████╗██████╔╝██║ ╚████║███████╗███████║██████╔╝██║  ██║   ██║",
        " ╚══╝╚══╝ ╚══════╝╚═════╝ ╚═╝  ╚═══╝╚══════╝╚══════╝╚═════╝ ╚═╝  ╚═╝   ╚═╝",
    ),
    "THURSDAY": (
        "████████╗██╗  ██╗██╗   ██╗██████╗ ███████╗██████╗  █████╗ ██╗   ██╗",
        "╚══██╔══╝██║  ██║██║   ██║██╔══██╗██╔════╝██╔══██╗██╔══██╗╚██╗ ██╔╝",
        "   ██║   ███████║██║   ██║██████╔╝███████╗██║  ██║███████║ ╚████╔╝",
        "   ██║   ██╔══██║██║   ██║██╔══██╗╚════██║██║  ██║██╔══██║  ╚██╔╝",
        "   ██║   ██║  ██║╚██████╔╝██║  ██║███████║██████╔╝██║  ██║   ██║",
        "   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═════╝ ╚═╝  ╚═╝   ╚═╝",
    ),
    "FRIDAY": (
        "███████╗██████╗ ██╗██████╗  █████╗ ██╗   ██╗",
        "██╔════╝██╔══██╗██║██╔══██╗██╔══██╗╚██╗ ██╔╝",
        "█████╗  ██████╔╝██║██║  ██║███████║ ╚████╔╝",
        "██╔══╝  ██╔══██╗██║██║  ██║██╔══██║  ╚██╔╝",
        "██║     ██║  ██║██║██████╔╝██║  ██║   ██║",
        "╚═╝     ╚═╝  ╚═╝╚═╝╚═════╝ ╚═╝  ╚═╝   ╚═╝",
    ),
    "SATURDAY": (
        "███████╗ █████╗ ████████╗██╗   ██╗██████╗ ██████╗  █████╗ ██╗   ██╗",
        "██╔════╝██╔══██╗╚══██╔══╝██║   ██║██╔══██╗██╔══██╗██╔══██╗╚██╗ ██╔╝",
        "███████╗███████║   ██║   ██║   ██║██████╔╝██║  ██║███████║ ╚████╔╝",
        "╚════██║██╔══██║   ██║   ██║   ██║██╔══██╗██║  ██║██╔══██║  ╚██╔╝",
        "███████║██║  ██║   ██║   ╚██████╔╝██║  ██║██████╔╝██║  ██║   ██║",
        "╚══════╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝   ╚═╝",
    ),
    "SUNDAY": (
        "███████╗██╗   ██╗███╗   ██╗██████╗  █████╗ ██╗   ██╗",
        "██╔════╝██║   ██║████╗  ██║██╔══██╗██╔══██╗╚██╗ ██╔╝",
        "███████╗██║   ██║██╔██╗ ██║██║  ██║███████║ ╚████╔╝",
        "╚════██║██║   ██║██║╚██╗██║██║  ██║██╔══██║  ╚██╔╝",
        "███████║╚██████╔╝██║ ╚████║██████╔╝██║  ██║   ██║",
        "╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚═════╝ ╚═╝  ╚═╝   ╚═╝",
    ),
    "BUILD-UP": (
        "██████╗ ██╗   ██╗██╗██╗     ██████╗       ██╗   ██╗██████╗ ",
        "██╔══██╗██║   ██║██║██║     ██╔══██╗      ██║   ██║██╔══██╗",
        "██████╔╝██║   ██║██║██║     ██║  ██║█████╗██║   ██║██████╔╝",
        "██╔══██╗██║   ██║██║██║     ██║  ██║╚════╝██║   ██║██╔═══╝ ",
        "██████╔╝╚██████╔╝██║███████╗██████╔╝      ╚██████╔╝██║     ",
        "╚═════╝  ╚═════╝ ╚═╝╚══════╝╚═════╝        ╚═════╝ ╚═╝     ",
    ),
}


def _render_banner_word(word: str) -> list[str]:
    """Return a prebuilt banner in the original block font."""
    try:
        return list(_ASCII_BANNERS[word])
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 ASCII 배너: {word}") from exc


def _weekday_name(now: Optional[datetime] = None) -> str:
    return (
        "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
        "FRIDAY", "SATURDAY", "SUNDAY",
    )[(now or datetime.now()).weekday()]


def _ascii_banner_lines(now: Optional[datetime] = None) -> list[str]:
    """Return today's weekday as a block-letter banner."""
    return _render_banner_word(_weekday_name(now))


def _ascii_night_banner_lines() -> list[str]:
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
    if "night" in label:
        return f" {PRODUCT_NAME} ", NIGHT_BORDER, NIGHT_BANNER, NIGHT_LABEL
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
    assistant_mode: str = "nighttime",
) -> list[Text]:
    """Build fully rendered header lines for animated output."""
    now = datetime.now()
    wd = WEEKDAYS_KO[now.weekday()]
    date_str = f"{now.strftime('%Y-%m-%d')} ({wd})"
    job_str = _display_job_name(current_job, max_len=48)
    header_title, border_style, banner_style, label_style = _assistant_visuals(assistant_mode)
    assistant_display = re.sub(r"\bnight(?=/|\b)", "research", assistant_mode)

    content_lines: list[tuple[str, str]] = []
    content_lines.append(("", "default"))
    for line in _ascii_banner_lines():
        content_lines.append((line, banner_style))
    content_lines.append(("", "default"))
    content_lines.append((f" mode          {mode}", "default"))
    content_lines.append((f" assistant     {assistant_display}", label_style))
    context_label = "context" if "night" in assistant_mode.lower() else "job"
    content_lines.append((f" {context_label:<12} {job_str}", "default"))
    content_lines.append((f" weekday       {_weekday_name(now)}", "default"))
    content_lines.append((f" date          {date_str}", "default"))
    content_lines.append((f" history       {history_turns} turns", "default"))
    content_lines.append((" role          Deep research agent · study coach", "default"))
    content_lines.append((" shell         interactive", "default"))
    content_lines.append(("", "default"))
    content_lines.append((" quick commands", HEADER_DIM))
    content_lines.append((" /research  /study  /translate  /sessions  /new  /help  /exit", "default"))
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
    assistant_mode: str = "nighttime",
) -> None:
    """Print the build-up session header line-by-line, like neofetch."""
    lines = _build_header_lines(mode, current_job, history_turns, assistant_mode)
    for idx, line in enumerate(lines):
        console.print(line)
        if HEADER_ANIMATE and idx < len(lines) - 1:
            time.sleep(HEADER_DELAY)


def render_night_banner(
    night_model: str,
    reviewer_model: str,
    *,
    reviewer_active: bool = False,
    active_paper_id: str | None = None,
) -> None:
    """Print a dedicated build-up research-mode activation banner."""
    reviewer_state = "armed" if reviewer_active else "standby"
    active_paper = active_paper_id or "-"

    content_lines: list[tuple[str, str]] = [("", "default")]
    for line in _ascii_night_banner_lines():
        content_lines.append((line, NIGHT_BANNER))
    content_lines.extend(
        [
            ("", "default"),
            (" phase         NIGHTTIME", NIGHT_LABEL),
            (" workspace     deep research cockpit", NIGHT_LABEL),
            (" focus         cited research -> personal study", "default"),
            (f" research model {night_model}", "default"),
            (f" reviewer      {reviewer_model} ({reviewer_state})", "default"),
            (f" active paper  {_display_job_name(active_paper, max_len=56)}", "default"),
            (" sessions      workspace-scoped research / study transcripts", "default"),
            ("", "default"),
            (" build-up commands", HEADER_DIM),
            (" /research   /study   /sessions   /translate   daytime", "default"),
            ("", "default"),
        ]
    )
    _render_boxed_lines(
        f" {PRODUCT_NAME} ",
        content_lines,
        delay=HEADER_DELAY if HEADER_ANIMATE else 0.0,
        border_style=NIGHT_BORDER,
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
            (" /files   /cal today   /search   /job list   nighttime", "default"),
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
    assistant_mode: str = "nighttime",
) -> str:
    """Build a compact monotone input prompt with useful session context."""
    now_str = datetime.now().strftime("%H:%M")
    job = _display_job_name((current_job or "-").strip() or "-", max_len=28)
    steer_suffix = ""
    if "+steer:" in assistant_mode:
        assistant_mode, steer_suffix = assistant_mode.split("+steer:", 1)
    assistant_label = assistant_mode.lower()
    if "night" in assistant_label:
        assistant = "research/reviewer" if "reviewer" in assistant_label else "research"
        prompt_name = f" {PRODUCT_NAME} "
    else:
        assistant = "daytime"
        prompt_name = f" {PRODUCT_NAME} "
    if steer_suffix:
        assistant = f"{assistant}/{steer_suffix}"

    prompt = Text()
    prompt.append(prompt_name, style=PROMPT_BRACKET)
    prompt.append("[ ", style=PROMPT_BRACKET)
    prompt.append(mode, style=PROMPT_VALUE)
    prompt.append(":", style=PROMPT_BRACKET)
    prompt.append(assistant, style=PROMPT_VALUE)
    prompt.append(" | ", style=PROMPT_BRACKET)
    prompt.append(job, style=PROMPT_VALUE)
    prompt.append(" | ", style=PROMPT_BRACKET)
    prompt.append(now_str, style=PROMPT_VALUE)
    prompt.append(" ] > ", style=PROMPT_BRACKET)
    return prompt.plain


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
        "나이트타임 / 나이트모드             Deep-research-first mode\n"
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
        "/research QUERY                    One-call staged deep research\n"
        "/research list                     List research runs in this job\n"
        "/research show NUMBER              Open a report and its study guide\n"
        "/translate PDF|PAPER_ID             Translate paper PDF to Korean Markdown\n"
        "\n"
        "── Personal study (secondary) ──\n"
        "/study start TOPIC                 Start a Socratic study session\n"
        "/study list|use NUMBER             List/resume study sessions\n"
        "/study note TEXT                   Save a learner-verified note\n"
        "/study status|close|review         Status, close, or due reviews\n"
        "/study review NUMBER              Start retrieval-first review\n"
        "/study review done                Complete the current due review\n"
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
