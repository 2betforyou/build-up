"""Job management: CRUD, action log, and project templates."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from friday.config import FridayConfig
from friday.paths import (
    copy_into_workspace,
    ensure_within,
    is_pdf_file,
    is_text_file,
    new_job_id,
    read_pdf_file,
    read_text_file,
    resolve_path,
    validate_job_id,
)
from friday.state import job_dir, set_current_job


# ============================================================
# Action log (per-job)
# ============================================================

def append_action_log(
    job_id: str, cfg: FridayConfig, action: str, detail: str = "",
) -> None:
    """Append a timestamped entry to the job's action log."""
    base = job_dir(job_id, cfg)
    log_file = base / ".friday_actions.jsonl"
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "detail": detail[:500],
    }
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_action_log(job_id: str, cfg: FridayConfig) -> List[Dict[str, str]]:
    """Read all entries from the job's action log."""
    base = job_dir(job_id, cfg)
    log_file = base / ".friday_actions.jsonl"
    if not log_file.exists():
        return []
    entries: List[Dict[str, str]] = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


# ============================================================
# Project templates
# ============================================================

BUILTIN_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "paper": {
        "description": "학술 논문 작업용",
        "dirs": ["drafts", "figures", "references", "notes"],
        "files": {
            "README.md": "# Paper Project\n\n작업 메모를 여기에 기록하세요.\n",
            "notes/todo.md": "# TODO\n\n- [ ] 초안 작성\n- [ ] 리뷰어 코멘트 반영\n- [ ] 최종 제출\n",
        },
    },
    "code": {
        "description": "코드 프로젝트 작업용",
        "dirs": ["src", "tests", "docs"],
        "files": {
            "README.md": "# Code Project\n\n프로젝트 설명을 여기에 작성하세요.\n",
            "docs/notes.md": "# 개발 노트\n\n",
        },
    },
    "report": {
        "description": "보고서/문서 작업용",
        "dirs": ["drafts", "data", "assets"],
        "files": {
            "README.md": "# Report Project\n\n보고서 개요를 작성하세요.\n",
            "drafts/outline.md": "# 개요\n\n1. 서론\n2. 본론\n3. 결론\n",
        },
    },
    "research": {
        "description": "build-up 딥 리서치 작업용",
        "dirs": ["deep-research", "study", "attachments"],
        "files": {
            "README.md": "# Research Workspace\n\n딥 리서치 실행, 근거 자료, 학습 기록을 함께 관리합니다.\n",
        },
    },
    "study": {
        "description": "개인 공부·복습 작업용",
        "dirs": ["study", "references"],
        "files": {
            "README.md": "# Study Workspace\n\n설명 → 예제 → 반례 → 복습 순서로 학습합니다.\n",
        },
    },
    "ops": {
        "description": "운영/관리 작업용",
        "dirs": ["scripts", "configs", "logs"],
        "files": {
            "README.md": "# Ops Project\n\n운영 작업을 기록하세요.\n",
        },
    },
}


def list_templates(cfg: FridayConfig) -> Dict[str, str]:
    """Return {name: description} for built-in + user templates."""
    result = {k: v["description"] for k, v in BUILTIN_TEMPLATES.items()}
    if cfg.templates_dir.exists():
        for p in cfg.templates_dir.iterdir():
            if p.is_dir() and not p.is_symlink():
                desc_file = p / ".description"
                desc = desc_file.read_text(encoding="utf-8").strip() if desc_file.exists() else "사용자 템플릿"
                result[p.name] = desc
    return result


def apply_template(template_name: str, target_dir: Path, cfg: FridayConfig) -> List[str]:
    """Apply a template to target_dir.  Returns list of created paths."""
    created: List[str] = []

    # Check user templates first
    user_tpl = cfg.templates_dir / template_name
    if user_tpl.exists() and user_tpl.is_dir():
        for item in sorted(user_tpl.rglob("*")):
            if item.is_symlink():
                continue
            rel = item.relative_to(user_tpl)
            dst = target_dir / rel
            if item.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dst, follow_symlinks=False)
            created.append(str(rel))
        return created

    # Built-in templates
    tpl = BUILTIN_TEMPLATES.get(template_name)
    if not tpl:
        available = ", ".join(list_templates(cfg).keys())
        raise ValueError(f"알 수 없는 템플릿: {template_name}\n사용 가능: {available}")

    for d in tpl.get("dirs", []):
        (target_dir / d).mkdir(parents=True, exist_ok=True)
        created.append(d + "/")

    for filepath, content in tpl.get("files", {}).items():
        dst = target_dir / filepath
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        created.append(filepath)

    return created


# ============================================================
# Command-level helpers
# ============================================================

def cmd_job_new(
    label: Optional[str], cfg: FridayConfig, template: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Create a new job, optionally applying a template."""
    job_id = new_job_id(label)
    base = job_dir(job_id, cfg)
    set_current_job(job_id, cfg)
    created: List[str] = []
    if template:
        created = apply_template(template, base, cfg)
        append_action_log(job_id, cfg, "job_new", f"template={template}")
    else:
        append_action_log(job_id, cfg, "job_new", "")
    return job_id, created


def cmd_job_use(job_id: str, cfg: FridayConfig) -> Path:
    """Switch to an existing job."""
    job_id = validate_job_id(job_id)
    path = cfg.workspace_dir / job_id
    if not path.exists():
        raise ValueError(f"job 디렉터리가 존재하지 않습니다: {path}")
    set_current_job(job_id, cfg)
    return path


def cmd_job_list(cfg: FridayConfig) -> List[str]:
    """List all job directories."""
    if not cfg.workspace_dir.exists():
        return []
    return sorted(
        d.name for d in cfg.workspace_dir.iterdir()
        if d.is_dir() and not d.is_symlink()
    )


def cmd_import(src_path: str, job_id: Optional[str], cfg: FridayConfig) -> Tuple[Path, str]:
    """Import a file/dir into the current (or new) job.

    Enforces sandbox: source is read-only validated, destination
    is always inside workspace/{job_id}/.
    """
    from friday.sandbox import require_import_source

    if not job_id:
        job_id, _ = cmd_job_new(None, cfg)
    src = resolve_path(src_path)

    # Validate: source must exist and not be a symlink
    require_import_source(src, cfg, context=f"import {src.name}")

    dst = copy_into_workspace(src, job_dir(job_id, cfg), cfg)
    append_action_log(job_id, cfg, "import", str(src.name))
    return dst, job_id


def cmd_files(job_id: str, cfg: FridayConfig) -> List[str]:
    """List files in a job directory."""
    from friday.paths import list_files
    return list_files(job_dir(job_id, cfg))


def cmd_write(job_id: str, relpath: str, content: str, cfg: FridayConfig) -> Path:
    """Create or overwrite a text file inside the job workspace.

    Enforces sandbox: path must be inside workspace/{job_id}/.
    """
    from friday.sandbox import require_write

    if not relpath:
        raise ValueError("파일 경로(relpath)가 지정되지 않았습니다.")

    base = job_dir(job_id, cfg)
    path = ensure_within(base / relpath, base)

    require_write(path, job_id, cfg, context=f"write {relpath}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    append_action_log(job_id, cfg, "write", relpath)
    return path


def cmd_edit(
    job_id: str,
    relpath: str,
    old_string: str,
    new_string: str,
    cfg: FridayConfig,
) -> str:
    """Replace *old_string* with *new_string* inside a file (exact match).

    Enforces sandbox.  Returns a unified diff of the change.
    Raises ValueError if old_string is not found or is ambiguous (multiple matches).
    """
    from friday.paths import compute_diff
    from friday.sandbox import require_read, require_write

    if not relpath:
        raise ValueError("파일 경로(relpath)가 지정되지 않았습니다.")

    base = job_dir(job_id, cfg)
    path = ensure_within(base / relpath, base)

    require_read(path, cfg, context=f"edit {relpath}")
    require_write(path, job_id, cfg, context=f"edit {relpath}")

    if not path.is_file():
        raise ValueError(f"파일을 찾을 수 없습니다: {relpath}")

    original = path.read_text(encoding="utf-8")

    count = original.count(old_string)
    if count == 0:
        raise ValueError(
            "old_string을 파일에서 찾을 수 없습니다.\n"
            "정확한 내용(공백·줄바꿈 포함)을 확인하세요."
        )
    if count > 1:
        raise ValueError(
            f"old_string이 파일에 {count}곳에 존재합니다. "
            f"더 넓은 컨텍스트를 포함해서 유일하게 지정하세요."
        )

    modified = original.replace(old_string, new_string, 1)
    path.write_text(modified, encoding="utf-8")
    append_action_log(job_id, cfg, "edit", f"{relpath}: {old_string[:60]!r} → {new_string[:60]!r}")

    return compute_diff(original, modified, filename=relpath)


def cmd_read(job_id: str, relpath: str, cfg: FridayConfig) -> str:
    """Read a text or PDF file from the job workspace.

    Enforces sandbox: path must be inside workspace/{job_id}/.
    """
    from friday.sandbox import require_read

    base = job_dir(job_id, cfg)
    path = ensure_within(base / relpath, base)

    # Validate through sandbox
    require_read(path, cfg, context=f"read {relpath}")

    if is_pdf_file(path):
        return read_pdf_file(path, cfg)
    if not is_text_file(path, cfg):
        raise ValueError(f"지원하지 않는 파일 형식: {path.suffix}\n텍스트 또는 PDF만 읽기 가능합니다.")
    return read_text_file(path, cfg)
