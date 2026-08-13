"""Git integration for Friday.

Runs git commands inside the current job directory.
Destructive operations (push --force, reset --hard, checkout .)
raise RequiresConfirmation so the shell can prompt the user.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple


class RequiresConfirmation(Exception):
    """Raised when a git command needs explicit user approval."""


# Operations that must be confirmed before running.
_DESTRUCTIVE_PATTERNS = (
    re.compile(r"\bpush\b.*--force\b", re.I),
    re.compile(r"\bpush\b.*-f\b", re.I),
    re.compile(r"\breset\b.*--hard\b", re.I),
    re.compile(r"\bcheckout\b.*\.\b", re.I),
    re.compile(r"\bclean\b.*-f\b", re.I),
    re.compile(r"\bclean\b.*--force\b", re.I),
    re.compile(r"\bstash\s+drop\b", re.I),
    re.compile(r"\bbranch\b.*-[Dd]\b", re.I),
)

_MAX_OUTPUT = 200   # max lines of stdout/stderr to return


def is_git_repo(path: Path) -> bool:
    """Return True if *path* (or any parent) is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_destructive(args: str) -> Optional[str]:
    """Return a human-readable description if *args* matches a destructive pattern."""
    for pat in _DESTRUCTIVE_PATTERNS:
        if pat.search(args):
            return f"`git {args.strip()}` 는 되돌리기 어려운 작업입니다."
    return None


def git_run(
    args: str,
    cwd: Path,
    *,
    confirm_destructive: bool = False,
) -> Tuple[str, int]:
    """Run `git <args>` in *cwd* and return (output, exit_code).

    Parameters
    ──────────
    args                Shell-style git sub-command string, e.g. "status" or
                        "commit -m 'message'".
    cwd                 Working directory (job base or a git repo root within it).
    confirm_destructive If True, skip the destructive-command guard.
                        Set by the shell after the user confirms.

    Raises
    ──────
    RequiresConfirmation  if the command looks destructive and confirm_destructive
                          is False.
    RuntimeError          if git is not found or the repo check fails.
    """
    if not confirm_destructive:
        msg = _is_destructive(args)
        if msg:
            raise RequiresConfirmation(msg)

    if not is_git_repo(cwd):
        raise RuntimeError(
            f"현재 job 폴더가 git 저장소가 아닙니다: {cwd}\n"
            "`git init` 또는 `git clone`을 먼저 실행하거나, git 저장소 안에 있는 job을 선택하세요."
        )

    # Split args safely — avoid shell=True for security
    import shlex
    try:
        tokens = shlex.split(args)
    except ValueError as exc:
        raise RuntimeError(f"git 인수 파싱 오류: {exc}") from exc

    try:
        proc = subprocess.run(
            ["git"] + tokens,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError("git 바이너리를 찾을 수 없습니다. git을 설치하십시오.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("git 명령 시간 초과 (60초).")

    output_lines = (proc.stdout + proc.stderr).splitlines()
    if len(output_lines) > _MAX_OUTPUT:
        output_lines = output_lines[-_MAX_OUTPUT:]
        output_lines.insert(0, f"(출력이 길어 마지막 {_MAX_OUTPUT}줄만 표시)")

    return "\n".join(output_lines), proc.returncode
