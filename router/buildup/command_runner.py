"""Bounded direct-process execution for Build-up power-user commands.

This module deliberately does not invoke a command shell.  `/shell` is kept as
an explicit power-user escape hatch, while agent-originated calls are gated by
the tool registry before they reach this runner.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence


class UnsafeCommandError(ValueError):
    """Raised when a command cannot be represented as one approved process."""


@dataclass(frozen=True)
class CommandResult:
    output: str
    returncode: int
    output_limit_reached: bool = False


ALLOWED_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "grep", "find", "wc", "sort", "uniq",
    "echo", "pwd", "date", "which", "file", "stat",
    "python", "python3", "pytest", "pip", "pip3",
    "npm", "node", "npx", "yarn", "cargo", "rustc", "go",
    "make", "cmake", "curl", "wget",
    "cp", "mv", "mkdir", "touch", "diff", "patch", "zip", "unzip", "tar",
    "ffmpeg", "convert",
})

_BLOCKED_PATTERNS = (
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\bchmod\s+[0-7]*7[0-7][0-7]\b"),
    re.compile(r"\bkill\s+-9\b", re.I),
    re.compile(r"\bshutdown\b|\breboot\b", re.I),
    re.compile(r"\b(mkfs|fdisk|parted)\b", re.I),
)
_SHELL_PUNCTUATION = "|&;<>()"
_MAX_COMMAND_CHARS = 8_192
_MAX_OUTPUT_BYTES = 1_000_000


def _operator_tokens(command: str) -> list[str]:
    """Return shell-control tokens that occur outside ordinary quoted text."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return [
        token
        for token in lexer
        if token and all(char in _SHELL_PUNCTUATION for char in token)
    ]


def parse_command(
    command: str,
    *,
    allowed_commands: frozenset[str] = ALLOWED_COMMANDS,
) -> list[str]:
    """Parse one direct command, rejecting shell syntax and disguised binaries."""
    if not isinstance(command, str):
        raise UnsafeCommandError("command must be a string")
    clean = command.strip()
    if not clean:
        raise UnsafeCommandError("command is empty")
    if len(clean) > _MAX_COMMAND_CHARS:
        raise UnsafeCommandError(f"command is longer than {_MAX_COMMAND_CHARS} characters")
    if any(char in clean for char in ("\x00", "\r", "\n")):
        raise UnsafeCommandError("control characters and multi-line commands are not allowed")
    if "`" in clean:
        raise UnsafeCommandError("command substitution is not allowed")
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(clean):
            raise UnsafeCommandError(f"blocked command pattern: {clean}")
    try:
        operators = _operator_tokens(clean)
        tokens = shlex.split(clean, posix=True)
    except ValueError as exc:
        raise UnsafeCommandError(f"command parse error: {exc}") from exc
    if operators:
        raise UnsafeCommandError(
            "shell operators are not allowed: " + ", ".join(sorted(set(operators)))
        )
    if not tokens:
        raise UnsafeCommandError("command is empty")
    executable = tokens[0]
    if "/" in executable or "\\" in executable:
        raise UnsafeCommandError("executable paths are not allowed; use an approved command name")
    if executable not in allowed_commands:
        raise UnsafeCommandError(
            f"command is not allowed: {executable}. "
            f"Allowed: {', '.join(sorted(allowed_commands))}"
        )
    return tokens


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def run_argv(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout: float = 60,
    max_output_bytes: int = _MAX_OUTPUT_BYTES,
    env: Optional[Mapping[str, str]] = None,
) -> CommandResult:
    """Run an argv vector with bounded output, time, and child-process cleanup."""
    if not argv or not all(isinstance(value, str) and "\x00" not in value for value in argv):
        raise UnsafeCommandError("argv must contain non-NUL string values")
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("timeout and max_output_bytes must be positive")
    run_dir = Path(cwd).expanduser().resolve(strict=True)
    if not run_dir.is_dir():
        raise NotADirectoryError(str(run_dir))

    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(run_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"executable not found: {argv[0]}") from exc

    output = bytearray()
    output_limit_reached = threading.Event()
    reader_error: list[BaseException] = []

    def _drain() -> None:
        assert proc.stdout is not None
        try:
            while True:
                chunk = proc.stdout.read(16_384)
                if not chunk:
                    break
                remaining = max_output_bytes - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_limit_reached.set()
                    _terminate_process_group(proc)
                    break
        except BaseException as exc:  # pragma: no cover - defensive pipe failure
            reader_error.append(exc)
            _terminate_process_group(proc)
        finally:
            proc.stdout.close()

    reader = threading.Thread(target=_drain, name="buildup-command-output", daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(proc)
        proc.wait(timeout=5)
        reader.join(timeout=5)
        raise RuntimeError(f"command timed out after {timeout:g} seconds") from exc
    reader.join(timeout=5)
    if reader.is_alive():  # pragma: no cover - OS-level pipe anomaly
        _terminate_process_group(proc)
        raise RuntimeError("command output reader did not terminate")
    if reader_error:
        raise RuntimeError(f"failed to read command output: {reader_error[0]}")

    decoded = bytes(output).decode("utf-8", errors="replace").strip()
    if output_limit_reached.is_set():
        marker = f"[output limit reached at {max_output_bytes:,} bytes; process terminated]"
        decoded = f"{decoded}\n{marker}" if decoded else marker
    return CommandResult(
        output=decoded,
        returncode=int(proc.returncode or 0),
        output_limit_reached=output_limit_reached.is_set(),
    )


def run_command(
    command: str,
    cwd: Path,
    *,
    timeout: float = 60,
    max_output_bytes: int = _MAX_OUTPUT_BYTES,
) -> CommandResult:
    """Validate and execute a single allowlisted command without a shell."""
    return run_argv(
        parse_command(command),
        cwd,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )
