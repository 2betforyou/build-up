"""Arrow-key list pickers for interactive navigation (Claude Code / Codex CLI style).

Used where a user would otherwise have to run a list command, remember an id or
number, then re-type it into a second command. Falls back to ``None`` (caller
prints the plain list instead) whenever a picker cannot be shown: no TTY,
piped input, or prompt_toolkit unavailable.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence, Tuple


def is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def pick_one(title: str, choices: Sequence[Tuple[str, str]], *, text: str = "") -> Optional[str]:
    """Show an arrow-key single-select list; return the chosen value or None.

    ``choices`` is a sequence of ``(value, label)`` pairs, most relevant first.
    Returns ``None`` if the user cancels (Esc), there is nothing to pick from,
    or the terminal cannot host an interactive dialog.
    """
    if not choices or not is_interactive_tty():
        return None
    try:
        from prompt_toolkit.shortcuts import radiolist_dialog
    except ImportError:
        return None
    try:
        return radiolist_dialog(
            title=title,
            text=text or "↑↓ 이동 · Enter 선택 · Esc 취소",
            values=list(choices),
        ).run()
    except Exception:
        # Any terminal/runtime quirk (e.g. dumb terminal, resize race) should
        # degrade to the plain-list fallback rather than crash the shell.
        return None
