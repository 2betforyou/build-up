"""Centralized sandbox policy for Friday file operations.

All file I/O in Friday MUST go through this module's gate functions.
This enforces a simple, auditable permission model:

    ┌─────────────────────────────────────────────────────────┐
    │                  Friday Sandbox Policy                   │
    ├──────────┬──────────────────────────────────────────────┤
    │  WRITE   │ workspace/{job_id}/ only                     │
    │  LIBRARY │ library/papers/ only                         │
    │  READ    │ anywhere (for import, reference)             │
    │  DELETE  │ never — trash-move only, within job          │
    │  EXPORT  │ FridayLocal/export/ only                     │
    │  TRASH   │ workspace/{job}/ → FridayLocal/trash/{job}/  │
    └──────────┴──────────────────────────────────────────────┘

Violations raise ``SandboxViolation`` (a ValueError subclass) with a
clear message explaining what was blocked and why.

Usage in other modules::

    from friday.sandbox import (
        require_read,
        require_write,
        require_export,
        require_library_write,
        require_trash,
        SandboxViolation,
    )
"""

from __future__ import annotations

import logging
from pathlib import Path

from friday.config import FridayConfig

logger = logging.getLogger("friday.sandbox")


# ================================================================
# Exception
# ================================================================

class SandboxViolation(ValueError):
    """Raised when a file operation violates the sandbox policy."""

    def __init__(self, operation: str, path: Path, reason: str):
        self.operation = operation
        self.blocked_path = path
        self.reason = reason
        super().__init__(
            f"[Sandbox] {operation} 차단: {path}\n"
            f"  이유: {reason}"
        )


# ================================================================
# Internal helpers
# ================================================================

def _resolve(path: Path) -> Path:
    """Resolve without requiring existence (strict=False)."""
    return path.expanduser().resolve()


def _is_within(child: Path, parent: Path) -> bool:
    """Check if *child* is inside or equal to *parent*."""
    child_r = _resolve(child)
    parent_r = _resolve(parent)
    return child_r == parent_r or parent_r in child_r.parents


def _is_symlink_safe(path: Path) -> bool:
    """Return False if *path* is a symlink."""
    return not path.is_symlink()


# ================================================================
# Public gate functions
# ================================================================

def require_read(
    path: Path,
    cfg: FridayConfig,
    *,
    context: str = "",
) -> Path:
    """Validate a path for READ access.

    READ is allowed **anywhere** on the filesystem.
    We still reject symlinks and validate existence.

    Returns the resolved, validated path.
    """
    resolved = _resolve(path)

    if not _is_symlink_safe(path):
        raise SandboxViolation("READ", resolved, "심볼릭 링크는 허용하지 않습니다.")

    if not resolved.exists():
        raise SandboxViolation("READ", resolved, "파일이 존재하지 않습니다.")

    logger.debug("READ allowed: %s%s", resolved, f" ({context})" if context else "")
    return resolved


def require_write(
    path: Path,
    job_id: str,
    cfg: FridayConfig,
    *,
    context: str = "",
) -> Path:
    """Validate a path for WRITE access.

    WRITE is allowed **only** inside ``workspace/{job_id}/``.

    Returns the resolved, validated path.
    """
    resolved = _resolve(path)
    job_base = _resolve(cfg.workspace_dir / job_id)

    if not _is_within(resolved, job_base):
        raise SandboxViolation(
            "WRITE", resolved,
            f"쓰기는 현재 job 폴더 내부에서만 허용됩니다.\n"
            f"  허용 범위: {job_base}\n"
            f"  요청 경로: {resolved}",
        )

    if resolved.exists() and not _is_symlink_safe(path):
        raise SandboxViolation("WRITE", resolved, "심볼릭 링크는 허용하지 않습니다.")

    logger.debug("WRITE allowed: %s%s", resolved, f" ({context})" if context else "")
    return resolved


def require_library_write(
    path: Path,
    cfg: FridayConfig,
    *,
    context: str = "",
) -> Path:
    """Validate a write path inside Friday's paper library.

    This is intentionally narrower than general WRITE access.  It exists so
    build-up paper-reading can keep a durable paper library without opening
    arbitrary filesystem writes.
    """
    resolved = _resolve(path)
    library_base = _resolve(cfg.paper_library_dir)

    if not _is_within(resolved, library_base):
        raise SandboxViolation(
            "LIBRARY_WRITE", resolved,
            f"논문 라이브러리 쓰기는 library/papers 내부에서만 허용됩니다.\n"
            f"  허용 범위: {library_base}\n"
            f"  요청 경로: {resolved}",
        )

    if resolved.exists() and not _is_symlink_safe(path):
        raise SandboxViolation("LIBRARY_WRITE", resolved, "심볼릭 링크는 허용하지 않습니다.")

    logger.debug("LIBRARY_WRITE allowed: %s%s", resolved, f" ({context})" if context else "")
    return resolved


def require_library_index_write(
    path: Path,
    cfg: FridayConfig,
    *,
    context: str = "",
) -> Path:
    """Validate a write path inside Friday's paper index views."""
    resolved = _resolve(path)
    index_base = _resolve(cfg.paper_index_dir)

    if not _is_within(resolved, index_base):
        raise SandboxViolation(
            "LIBRARY_INDEX_WRITE", resolved,
            f"논문 인덱스 쓰기는 library/index 내부에서만 허용됩니다.\n"
            f"  허용 범위: {index_base}\n"
            f"  요청 경로: {resolved}",
        )
    if resolved.exists() and not _is_symlink_safe(path):
        raise SandboxViolation("LIBRARY_INDEX_WRITE", resolved, "심볼릭 링크는 허용하지 않습니다.")
    logger.debug("LIBRARY_INDEX_WRITE allowed: %s%s", resolved, f" ({context})" if context else "")
    return resolved


def require_create_dir(
    path: Path,
    job_id: str,
    cfg: FridayConfig,
    *,
    context: str = "",
) -> Path:
    """Validate a directory creation path. Same rules as WRITE."""
    return require_write(path, job_id, cfg, context=context or "mkdir")


def require_export(
    dest: Path,
    cfg: FridayConfig,
    *,
    context: str = "",
) -> Path:
    """Validate a path for EXPORT (copy-out) access.

    EXPORT is allowed **only** to ``FridayLocal/export/``.
    Users cannot export to arbitrary filesystem locations.

    Returns the resolved, validated path.
    """
    resolved = _resolve(dest)
    export_base = _resolve(cfg.export_dir)

    if not _is_within(resolved, export_base):
        raise SandboxViolation(
            "EXPORT", resolved,
            f"내보내기는 export 폴더로만 허용됩니다.\n"
            f"  허용 범위: {export_base}\n"
            f"  요청 경로: {resolved}\n"
            f"  팁: /export 명령을 인자 없이 실행하면 기본 export 폴더로 내보냅니다.",
        )

    logger.debug("EXPORT allowed: %s%s", resolved, f" ({context})" if context else "")
    return resolved


def require_trash(
    path: Path,
    job_id: str,
    cfg: FridayConfig,
    *,
    context: str = "",
) -> Path:
    """Validate a path for TRASH (move-to-trash) access.

    The source must be inside ``workspace/{job_id}/``.
    The destination is always ``FridayLocal/trash/{job_id}/``.
    Direct deletion is never allowed.

    Returns the resolved source path.
    """
    resolved = _resolve(path)
    job_base = _resolve(cfg.workspace_dir / job_id)

    if not _is_within(resolved, job_base):
        raise SandboxViolation(
            "TRASH", resolved,
            f"trash 이동은 현재 job 폴더 내부 파일만 가능합니다.\n"
            f"  허용 범위: {job_base}\n"
            f"  요청 경로: {resolved}",
        )

    if not _is_symlink_safe(path):
        raise SandboxViolation("TRASH", resolved, "심볼릭 링크는 허용하지 않습니다.")

    if not resolved.exists():
        raise SandboxViolation("TRASH", resolved, "파일이 존재하지 않습니다.")

    logger.debug("TRASH allowed: %s%s", resolved, f" ({context})" if context else "")
    return resolved


def require_import_source(
    src: Path,
    cfg: FridayConfig,
    *,
    context: str = "",
) -> Path:
    """Validate a source path for IMPORT (copy-in).

    Import SOURCE can be anywhere (read-only access).
    The destination is always inside workspace/{job_id}/ (enforced by
    ``require_write`` at the call site).

    Returns the resolved, validated source path.
    """
    return require_read(src, cfg, context=context or "import source")


# ================================================================
# Convenience: check without raising
# ================================================================

def can_write(path: Path, job_id: str, cfg: FridayConfig) -> bool:
    """Return True if *path* passes the WRITE gate."""
    try:
        require_write(path, job_id, cfg)
        return True
    except SandboxViolation:
        return False


def can_export(dest: Path, cfg: FridayConfig) -> bool:
    """Return True if *dest* passes the EXPORT gate."""
    try:
        require_export(dest, cfg)
        return True
    except SandboxViolation:
        return False


# ================================================================
# Audit helper
# ================================================================

def describe_policy() -> str:
    """Return a human-readable description of the sandbox policy."""
    return """build-up 샌드박스 정책:

  WRITE (쓰기/생성/수정)
    → workspace/{현재 job}/ 내부에서만 허용
    → 원본 파일은 직접 수정하지 않음 (복사본 생성)

  READ (읽기)
    → 어디서든 허용 (import, 참조 용도)

  DELETE (삭제)
    → 직접 삭제 불가
    → trash/ 폴더로 이동만 가능

  EXPORT (내보내기)
    → FridayLocal/export/ 폴더로만 허용
    → 임의 외부 경로 차단

  IMPORT (가져오기)
    → 외부 파일을 workspace/{job}/ 으로 복사
    → 원본은 변경되지 않음"""
