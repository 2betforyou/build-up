"""Git integration for Build-up.

Runs git commands inside the current job directory.
Destructive operations (push --force, reset --hard, checkout .)
raise RequiresConfirmation so the shell can prompt the user.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from buildup.command_runner import run_argv


class RequiresConfirmation(Exception):
    """Raised when a git command needs explicit user approval."""


class UnsafeGitCommand(RuntimeError):
    """Raised when git arguments could escape the managed execution boundary."""


# Operations that must be confirmed before running.
_DESTRUCTIVE_PATTERNS = (
    re.compile(r"^push\b", re.I),
    re.compile(r"^pull\b", re.I),
    re.compile(r"^reset\b", re.I),
    re.compile(r"^restore\b", re.I),
    re.compile(r"^checkout\b", re.I),
    re.compile(r"^clean\b", re.I),
    re.compile(r"^rebase\b", re.I),
    re.compile(r"^merge\b", re.I),
    re.compile(r"^cherry-pick\b", re.I),
    re.compile(r"^revert\b", re.I),
    re.compile(r"^am\b", re.I),
    re.compile(r"^apply\b", re.I),
    re.compile(r"^bisect\b", re.I),
    re.compile(r"^stash\s+(?:drop|clear|pop)\b", re.I),
    re.compile(r"^branch\b.*(?:-[dD]\b|--delete\b)", re.I),
    re.compile(r"^tag\b.*(?:-d\b|--delete\b)", re.I),
    re.compile(r"^commit\b.*--amend\b", re.I),
)

_ALLOWED_SUBCOMMANDS = frozenset({
    "add", "am", "apply", "bisect", "blame", "branch", "cat-file",
    "checkout", "cherry-pick", "clean", "commit", "count-objects",
    "describe", "diff", "diff-files", "diff-index", "diff-tree", "fetch",
    "for-each-ref", "fsck", "grep", "init", "log", "ls-files", "ls-tree",
    "merge", "merge-base", "mv", "name-rev", "notes", "pull", "push",
    "rebase", "reflog", "remote", "reset", "restore", "rev-parse",
    "revert", "shortlog", "show", "show-ref", "stash", "status", "switch",
    "symbolic-ref", "tag",
})

_FORBIDDEN_OPTION_PREFIXES = (
    "--exec-path", "--git-dir", "--work-tree", "--namespace", "--config-env",
    "--upload-pack", "--receive-pack", "--ext-diff", "--textconv",
    "--open-files-in-pager", "--no-index", "--output", "--template",
    "--separate-git-dir", "--strategy", "--strategy-option",
)
_MAX_ARGS_CHARS = 8_192

_MAX_OUTPUT = 200   # max lines of stdout/stderr to return


def is_git_repo(path: Path) -> bool:
    """Return True if *path* (or any parent) is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
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


def _parse_git_args(args: str) -> list[str]:
    if not isinstance(args, str):
        raise UnsafeGitCommand("git arguments must be a string")
    clean = args.strip()
    if not clean:
        raise UnsafeGitCommand("git arguments are empty")
    if len(clean) > _MAX_ARGS_CHARS:
        raise UnsafeGitCommand(f"git arguments exceed {_MAX_ARGS_CHARS} characters")
    if any(char in clean for char in ("\x00", "\r", "\n", "`")):
        raise UnsafeGitCommand("git arguments contain forbidden control or substitution syntax")
    try:
        tokens = shlex.split(clean, posix=True)
    except ValueError as exc:
        raise UnsafeGitCommand(f"git argument parse error: {exc}") from exc
    if not tokens:
        raise UnsafeGitCommand("git arguments are empty")
    subcommand = tokens[0]
    if subcommand.startswith("-") or "/" in subcommand or "\\" in subcommand:
        raise UnsafeGitCommand("git global options and executable paths are not allowed")
    if subcommand not in _ALLOWED_SUBCOMMANDS:
        raise UnsafeGitCommand(f"git subcommand is not allowed: {subcommand}")

    for token in tokens[1:]:
        lowered = token.lower()
        if lowered == "!" or lowered.startswith("ext::"):
            raise UnsafeGitCommand("external git helpers are not allowed")
        if lowered.startswith("/") or lowered == ".." or lowered.startswith("../") or "/../" in lowered:
            raise UnsafeGitCommand("absolute and parent-traversal paths are not allowed")
        if any(
            lowered == prefix or lowered.startswith(prefix + "=")
            for prefix in _FORBIDDEN_OPTION_PREFIXES
        ):
            raise UnsafeGitCommand(f"git option is not allowed: {token}")

    if subcommand == "bisect" and len(tokens) > 1 and tokens[1].lower() == "run":
        raise UnsafeGitCommand("git bisect run may execute arbitrary programs")
    if subcommand == "init":
        positional = [token for token in tokens[1:] if not token.startswith("-")]
        if positional not in ([], ["."]):
            raise UnsafeGitCommand("git init may only initialize the current job directory")
    return tokens


def _safe_git_environment() -> dict[str, str]:
    env = dict(os.environ)
    for name in tuple(env):
        if name in {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_NAMESPACE", "GIT_EXEC_PATH", "GIT_CONFIG_PARAMETERS",
            "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_SSH", "GIT_SSH_COMMAND",
            "GIT_ASKPASS", "SSH_ASKPASS",
        } or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(name, None)
    env.update({
        "GIT_CONFIG_COUNT": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true",
        "GIT_ALLOW_PROTOCOL": "file:http:https:ssh:git",
    })
    return env


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
    tokens = _parse_git_args(args)
    normalized_args = shlex.join(tokens)
    if not confirm_destructive:
        msg = _is_destructive(normalized_args)
        if msg:
            raise RequiresConfirmation(msg)

    run_dir = Path(cwd).expanduser().resolve(strict=True)
    if not run_dir.is_dir():
        raise RuntimeError(f"git 실행 경로가 디렉터리가 아닙니다: {run_dir}")
    if tokens[0] != "init" and not is_git_repo(run_dir):
        raise RuntimeError(
            f"현재 job 폴더가 git 저장소가 아닙니다: {run_dir}\n"
            "`git init`을 먼저 실행하거나 git 저장소인 job을 선택하세요."
        )

    completed = run_argv(
        [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "core.pager=cat",
            "-c", "protocol.ext.allow=never",
            *tokens,
        ],
        run_dir,
        timeout=60,
        env=_safe_git_environment(),
    )

    output_lines = completed.output.splitlines()
    if len(output_lines) > _MAX_OUTPUT:
        output_lines = output_lines[-_MAX_OUTPUT:]
        output_lines.insert(0, f"(출력이 길어 마지막 {_MAX_OUTPUT}줄만 표시)")

    return "\n".join(output_lines), completed.returncode
