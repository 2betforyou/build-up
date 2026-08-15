"""Path validation, file I/O, and sandbox utilities."""

from __future__ import annotations

import difflib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from buildup.config import BuildupConfig


def slugify(text: str) -> str:
    """Convert text to a safe slug for job IDs."""
    text = text.strip().lower()[:80]
    text = re.sub(r"[^a-z0-9가-힣._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "job"


def new_job_id(label: Optional[str] = None) -> str:
    """Generate a timestamped job ID with optional label."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = slugify(label) if label else "job"
    return f"{ts}-{suffix}"


def resolve_path(path_str: str) -> Path:
    """Expand ~ and resolve to absolute path."""
    return Path(path_str).expanduser().resolve()


def ensure_within(path: Path, root: Path) -> Path:
    """Ensure *path* is inside *root*.  Raises ValueError otherwise."""
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        common = Path(os.path.commonpath([resolved, root_resolved]))
    except ValueError:
        raise ValueError(f"허용된 경로 밖입니다: {resolved}")
    if common != root_resolved:
        raise ValueError(f"허용된 경로 밖입니다: {resolved}")
    return resolved


def reject_symlink(path: Path) -> None:
    """Reject symlinks for security."""
    if path.is_symlink():
        raise ValueError(f"심볼릭 링크는 허용하지 않습니다: {path}")


def validate_job_id(job_id: str) -> str:
    """Validate and sanitise a job ID string."""
    if not isinstance(job_id, str):
        raise ValueError("job ID는 문자열이어야 합니다.")
    job_id = job_id.strip()
    if not job_id:
        raise ValueError("job ID가 비어 있습니다.")
    if len(job_id) > 120:
        raise ValueError("job ID가 너무 깁니다 (최대 120자).")
    if not re.fullmatch(
        r"[A-Za-z0-9가-힣](?:[A-Za-z0-9가-힣._-]{0,118}[A-Za-z0-9가-힣])?",
        job_id,
    ):
        raise ValueError(
            "job ID는 영문·숫자·한글로 시작하고 끝나야 하며, "
            "중간에는 점·밑줄·하이픈만 사용할 수 있습니다."
        )
    return job_id


def is_text_file(path: Path, cfg: BuildupConfig) -> bool:
    return path.suffix.lower() in cfg.text_extensions


def is_pdf_file(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def read_text_file(path: Path, cfg: BuildupConfig) -> str:
    """Read a text file with size guard."""
    size = path.stat().st_size
    if size > cfg.max_text_bytes:
        raise ValueError(
            f"파일이 너무 큽니다 ({size:,} bytes, 상한 {cfg.max_text_bytes:,})."
        )
    return path.read_text(encoding="utf-8")


def read_pdf_file(path: Path, cfg: BuildupConfig) -> str:
    """Extract text from PDF using pdfminer.six (optional dependency)."""
    try:
        from pdfminer.high_level import extract_text  # type: ignore
    except ImportError:
        raise ImportError(
            "PDF 읽기에는 pdfminer.six가 필요합니다.  pip install pdfminer.six"
        )
    size = path.stat().st_size
    if size > cfg.max_text_bytes * 5:  # PDFs can be larger
        raise ValueError(f"PDF가 너무 큽니다 ({size:,} bytes).")
    text = extract_text(str(path))
    if not text or not text.strip():
        raise ValueError("PDF에서 텍스트를 추출할 수 없습니다 (스캔 이미지일 수 있습니다).")
    return text


def check_disk_space(target_dir: Path, cfg: BuildupConfig) -> None:
    """Raise if free disk space is below threshold."""
    try:
        usage = shutil.disk_usage(target_dir)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < cfg.min_free_disk_mb:
            raise ValueError(
                f"디스크 여유 공간 부족: {free_mb:.0f} MB 남음 (최소 {cfg.min_free_disk_mb} MB 필요)"
            )
    except OSError:
        pass


def copy_into_workspace(src: Path, dst_dir: Path, cfg: BuildupConfig) -> Path:
    """Copy a file or directory into a workspace folder.

    Enforces sandbox: source is read-validated, destination must be
    within a job workspace.
    """
    from buildup.sandbox import require_read

    # Validate source is readable (exists, not symlink)
    require_read(src, cfg, context="copy source")

    check_disk_space(dst_dir, cfg)
    dst = dst_dir / src.name
    if dst.exists():
        raise ValueError(f"대상이 이미 존재합니다: {dst}")
    try:
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=False)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    except Exception:
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
            else:
                dst.unlink(missing_ok=True)
        raise
    return dst


def move_to_trash(path: Path, job_id: str, cfg: BuildupConfig) -> Path:
    """Move a file to the job-specific trash folder.

    Enforces sandbox: source must be inside workspace/{job_id}/.
    Direct deletion is never performed.
    """
    from buildup.sandbox import require_trash

    # Validate: must be inside the job workspace
    require_trash(path, job_id, cfg, context="move_to_trash")

    trash_job = cfg.trash_dir / job_id
    trash_job.mkdir(parents=True, exist_ok=True)
    target = trash_job / path.name
    if target.exists():
        target = trash_job / f"{datetime.now().strftime('%H%M%S')}-{path.name}"
    shutil.move(str(path), str(target))
    return target


def export_job(job_id: str, cfg: BuildupConfig, dest: Optional[Path] = None) -> Path:
    """Export (copy) an entire job directory.

    Enforces sandbox: export destination is restricted to
    Build-up data root의 export/만 허용합니다. 임의 경로는 거부됩니다.
    """
    from buildup.sandbox import require_export

    src = cfg.workspace_dir / validate_job_id(job_id)
    if not src.exists():
        raise ValueError(f"job 디렉터리가 존재하지 않습니다: {src}")

    # Determine target — always inside export/
    if dest:
        # User specified a path: must be inside export/
        target = require_export(dest.resolve(), cfg, context="export_job")
    else:
        target = cfg.export_dir / job_id
        # Default path is always valid, but still validate for consistency
        require_export(target, cfg, context="export_job default")

    if target.exists():
        raise ValueError(f"내보낼 대상이 이미 존재합니다: {target}")
    check_disk_space(target.parent, cfg)
    shutil.copytree(src, target, symlinks=False)
    return target


def list_files(base: Path) -> List[str]:
    """List all files/dirs under *base*, excluding symlinks."""
    if not base.exists():
        return []
    results: List[str] = []
    for p in sorted(base.rglob("*")):
        if p.is_symlink():
            continue
        rel = p.relative_to(base)
        marker = "/" if p.is_dir() else ""
        results.append(str(rel) + marker)
    return results


def next_output_name(src: Path) -> str:
    """Generate the default rewrite output filename."""
    return f"{src.stem}.buildup{src.suffix}"


def glob_job(base: Path, pattern: str, max_results: int = 200) -> List[str]:
    """Glob files inside *base* matching *pattern*.

    Returns relative path strings sorted by name.
    Symlinks are excluded. Results capped at *max_results*.
    """
    base = base.resolve()
    results: List[str] = []
    try:
        for p in sorted(base.glob(pattern)):
            if p.is_symlink():
                continue
            try:
                rel = str(p.relative_to(base))
            except ValueError:
                continue
            results.append(rel)
            if len(results) >= max_results:
                break
    except Exception:
        pass
    return results


def grep_job(
    base: Path,
    pattern: str,
    path_glob: str = "**/*",
    case_insensitive: bool = False,
    max_matches: int = 100,
) -> List[str]:
    """Search file contents inside *base* for *pattern* (regex).

    Returns "relpath:lineno: line" strings, capped at *max_matches*.
    Binary files and symlinks are skipped.
    """
    base = base.resolve()
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"잘못된 정규식: {exc}") from exc

    results: List[str] = []
    for p in sorted(base.glob(path_glob)):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if compiled.search(line):
                rel = str(p.relative_to(base))
                results.append(f"{rel}:{lineno}: {line.rstrip()}")
                if len(results) >= max_matches:
                    return results
    return results


def compute_diff(original: str, modified: str, filename: str = "file") -> str:
    """Compute a unified diff string."""
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines, mod_lines,
        fromfile=f"a/{filename}", tofile=f"b/{filename}",
        lineterm="",
    )
    return "".join(diff)
