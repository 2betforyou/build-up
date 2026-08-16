"""Job management: CRUD, action log, and project templates."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from buildup.config import BuildupConfig
from buildup.paths import (
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
from buildup.state import job_dir, set_current_job


_JOB_METADATA_FILE = ".buildup-job.json"


def _normalize_job_display_name(name: str) -> str:
    display_name = re.sub(r"\s+", " ", name).strip()
    if not display_name:
        raise ValueError("새 job 이름을 입력해 주세요.")
    if len(display_name) > 80:
        raise ValueError("job 이름은 80자 이하여야 합니다.")
    return display_name


def _read_job_metadata(base: Path) -> Dict[str, Any]:
    metadata_path = base / _JOB_METADATA_FILE
    if not metadata_path.is_file() or metadata_path.is_symlink():
        return {}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _write_job_metadata(base: Path, updates: Dict[str, Any]) -> None:
    """Merge *updates* into the job metadata file atomically."""
    metadata_path = base / _JOB_METADATA_FILE
    if metadata_path.is_symlink():
        raise ValueError(f"job metadata가 symlink입니다: {metadata_path}")
    temporary_path = base / f"{_JOB_METADATA_FILE}.tmp"
    if temporary_path.is_symlink():
        raise ValueError(f"job metadata 임시 파일이 symlink입니다: {temporary_path}")
    metadata = _read_job_metadata(base)
    metadata.update(updates)
    temporary_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temporary_path.replace(metadata_path)


def _write_job_display_name(base: Path, display_name: str) -> None:
    _write_job_metadata(base, {"display_name": display_name})


def job_display_name(job_id: str, cfg: BuildupConfig) -> str:
    """Return a persisted display name without changing the stable job ID."""
    job_id = validate_job_id(job_id)
    metadata_path = cfg.workspace_dir / job_id / _JOB_METADATA_FILE
    if metadata_path.is_file() and not metadata_path.is_symlink():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(metadata, dict) and isinstance(metadata.get("display_name"), str):
                return _normalize_job_display_name(metadata["display_name"])
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return re.sub(r"^\d{8}-\d{6}-", "", job_id)


# ============================================================
# Bound directories
# ============================================================
#
# A job normally reaches only workspace/{job_id}/.  A bind widens that reach to
# one real directory the user names explicitly, so paper folders, manuscripts,
# and source trees can be worked on in place instead of being copied in.
#
# Binds are read-only unless the user passes --write, and the guards below keep
# a bind from ever covering Build-up's own data root.


@dataclass(frozen=True)
class JobBind:
    path: Path
    writable: bool

    def as_dict(self) -> Dict[str, Any]:
        return {"path": str(self.path), "writable": self.writable}


def _validate_bind_target(raw_path: str, cfg: BuildupConfig) -> Path:
    candidate = resolve_path(raw_path)
    if candidate.is_symlink():
        raise ValueError(f"심볼릭 링크는 bind할 수 없습니다: {candidate}")
    if not candidate.is_dir():
        raise ValueError(f"디렉터리가 아닙니다: {candidate}")
    if candidate == candidate.parent:
        raise ValueError("파일시스템 루트는 bind할 수 없습니다.")
    if candidate == Path.home().resolve():
        raise ValueError("홈 디렉터리 전체는 bind할 수 없습니다. 더 좁은 경로를 지정하세요.")

    base_dir = cfg.base_dir.expanduser().resolve()
    # Binding an ancestor of the data root would expose state.json, the session
    # database, and credentials cached under it.
    if candidate == base_dir or candidate in base_dir.parents:
        raise ValueError(
            f"Build-up 데이터 루트를 포함하는 경로는 bind할 수 없습니다: {candidate}"
        )
    return candidate


def job_binds(job_id: str, cfg: BuildupConfig) -> List[JobBind]:
    """Return the directories bound to *job_id*, dropping stale entries."""
    job_id = validate_job_id(job_id)
    entries = _read_job_metadata(cfg.workspace_dir / job_id).get("binds", [])
    binds: List[JobBind] = []
    if not isinstance(entries, list):
        return binds
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not isinstance(raw, str):
            continue
        path = Path(raw).expanduser()
        if not path.is_dir() or path.is_symlink():
            continue
        binds.append(JobBind(path=path.resolve(), writable=bool(entry.get("writable"))))
    return binds


def bind_job_path(
    job_id: str, raw_path: str, cfg: BuildupConfig, *, writable: bool = False,
) -> JobBind:
    """Bind one real directory to *job_id*.  Re-binding updates the write flag."""
    job_id = validate_job_id(job_id)
    target = _validate_bind_target(raw_path, cfg)
    base = cfg.workspace_dir / job_id
    if not base.is_dir():
        raise ValueError(f"job 폴더가 없습니다: {base}")
    kept = [bind for bind in job_binds(job_id, cfg) if bind.path != target]
    bind = JobBind(path=target, writable=writable)
    _write_job_metadata(base, {"binds": [item.as_dict() for item in kept + [bind]]})
    append_action_log(
        job_id, cfg, "job bind", f"{target} ({'rw' if writable else 'ro'})",
    )
    return bind


def unbind_job_path(job_id: str, raw_path: str, cfg: BuildupConfig) -> bool:
    """Remove a bind.  Returns False when the path was not bound."""
    job_id = validate_job_id(job_id)
    target = resolve_path(raw_path)
    existing = job_binds(job_id, cfg)
    kept = [bind for bind in existing if bind.path != target]
    if len(kept) == len(existing):
        return False
    _write_job_metadata(
        cfg.workspace_dir / job_id, {"binds": [item.as_dict() for item in kept]},
    )
    append_action_log(job_id, cfg, "job unbind", str(target))
    return True


def format_job_binds(job_id: str, cfg: BuildupConfig) -> str:
    binds = job_binds(job_id, cfg)
    if not binds:
        return "(bind된 디렉터리 없음)"
    return "\n".join(
        f"{'rw' if bind.writable else 'ro'}  {bind.path}" for bind in binds
    )


def format_job_label(job_id: str, cfg: BuildupConfig) -> str:
    """Show a readable name while retaining the stable ID needed by commands."""
    display_name = job_display_name(job_id, cfg)
    return f"{display_name}  [{job_id}]" if display_name != job_id else job_id


# ============================================================
# Action log (per-job)
# ============================================================

def append_action_log(
    job_id: str, cfg: BuildupConfig, action: str, detail: str = "",
) -> None:
    """Append a timestamped entry to the job's action log."""
    base = job_dir(job_id, cfg)
    log_file = base / ".buildup_actions.jsonl"
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "detail": detail[:500],
    }
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_action_log(job_id: str, cfg: BuildupConfig) -> List[Dict[str, str]]:
    """Read all entries from the job's action log."""
    base = job_dir(job_id, cfg)
    log_file = base / ".buildup_actions.jsonl"
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


def list_templates(cfg: BuildupConfig) -> Dict[str, str]:
    """Return {name: description} for built-in + user templates."""
    result = {k: v["description"] for k, v in BUILTIN_TEMPLATES.items()}
    if cfg.templates_dir.exists():
        for p in cfg.templates_dir.iterdir():
            if p.is_dir() and not p.is_symlink():
                desc_file = p / ".description"
                desc = desc_file.read_text(encoding="utf-8").strip() if desc_file.exists() else "사용자 템플릿"
                result[p.name] = desc
    return result


def apply_template(template_name: str, target_dir: Path, cfg: BuildupConfig) -> List[str]:
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
    label: Optional[str], cfg: BuildupConfig, template: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Create a new job, optionally applying a template."""
    job_id = new_job_id(label)
    base = job_dir(job_id, cfg)
    if label:
        _write_job_display_name(base, _normalize_job_display_name(label))
    set_current_job(job_id, cfg)
    created: List[str] = []
    if template:
        created = apply_template(template, base, cfg)
        append_action_log(job_id, cfg, "job_new", f"template={template}")
    else:
        append_action_log(job_id, cfg, "job_new", "")
    return job_id, created


def cmd_job_use(job_id: str, cfg: BuildupConfig) -> Path:
    """Switch to an existing job."""
    job_id = validate_job_id(job_id)
    path = cfg.workspace_dir / job_id
    if not path.exists():
        raise ValueError(f"job 디렉터리가 존재하지 않습니다: {path}")
    set_current_job(job_id, cfg)
    return path


def resolve_job_selector(selector: str, cfg: BuildupConfig) -> Optional[str]:
    """Resolve a job id, 1-based list index, or a fuzzy display-name fragment.

    Lets a user type a recognisable fragment ("report" instead of the full
    timestamped id) the way session/file pickers elsewhere in build-up
    already allow, without requiring an exact id.
    """
    value = selector.strip()
    if not value:
        return None
    jobs = cmd_job_list(cfg)
    if value in jobs:
        return value
    if value.isdigit():
        index = int(value)
        return jobs[index - 1] if 1 <= index <= len(jobs) else None
    needle = value.lower()
    labeled = [(jid, job_display_name(jid, cfg)) for jid in jobs]
    exact = [jid for jid, name in labeled if name.lower() == needle]
    if len(exact) == 1:
        return exact[0]
    contains = [jid for jid, name in labeled if needle in name.lower() or needle in jid.lower()]
    return contains[0] if len(contains) == 1 else None


def cmd_job_rename(job_id: str, new_name: str, cfg: BuildupConfig) -> str:
    """Rename a job for display while preserving its directory and stable ID."""
    job_id = validate_job_id(job_id)
    base = cfg.workspace_dir / job_id
    if not base.is_dir() or base.is_symlink():
        raise ValueError(f"job 디렉터리가 존재하지 않습니다: {base}")
    display_name = _normalize_job_display_name(new_name)
    _write_job_display_name(base, display_name)
    append_action_log(job_id, cfg, "job_rename", display_name)
    return display_name


def cmd_job_list(cfg: BuildupConfig) -> List[str]:
    """List all job directories."""
    if not cfg.workspace_dir.exists():
        return []
    return sorted(
        d.name for d in cfg.workspace_dir.iterdir()
        if d.is_dir() and not d.is_symlink()
    )


def cmd_import(src_path: str, job_id: Optional[str], cfg: BuildupConfig) -> Tuple[Path, str]:
    """Import a file/dir into the current (or new) job.

    Enforces sandbox: source is read-only validated, destination
    is always inside workspace/{job_id}/.
    """
    from buildup.sandbox import require_import_source

    if not job_id:
        job_id, _ = cmd_job_new(None, cfg)
    src = resolve_path(src_path)

    # Validate: source must exist and not be a symlink
    require_import_source(src, cfg, context=f"import {src.name}")

    dst = copy_into_workspace(src, job_dir(job_id, cfg), cfg)
    append_action_log(job_id, cfg, "import", str(src.name))
    return dst, job_id


def resolve_job_file(
    job_id: str, relpath: str, cfg: BuildupConfig, *, for_write: bool = False,
) -> Path:
    """Resolve *relpath* against the job workspace, then any bound directory.

    Absolute paths are accepted only when they land inside an allowed root, so a
    bind widens reach without reopening the whole filesystem.  A path that does
    not exist anywhere resolves into the workspace, keeping new files local.
    """
    from buildup.sandbox import job_read_roots, job_write_roots

    if not relpath:
        raise ValueError("파일 경로(relpath)가 지정되지 않았습니다.")
    roots = job_write_roots(job_id, cfg) if for_write else job_read_roots(job_id, cfg)

    candidate = Path(relpath).expanduser()
    if candidate.is_absolute():
        for root in roots:
            try:
                return ensure_within(candidate, root)
            except ValueError:
                continue
        if for_write:
            # Distinguish "never bound" from "bound but read-only", because the
            # second case looks like a bug to someone who just bound the path.
            for bind in job_binds(job_id, cfg):
                try:
                    ensure_within(candidate, bind.path)
                except ValueError:
                    continue
                raise ValueError(
                    f"읽기 전용으로 bind된 경로입니다: {bind.path}\n"
                    f"  쓰기도 열려면: buildup job bind {bind.path} --write"
                )
        allowed = "\n".join(f"    {root}" for root in roots)
        raise ValueError(f"허용된 경로 밖입니다: {candidate}\n  허용 범위:\n{allowed}")

    for root in roots:
        try:
            path = ensure_within(root / relpath, root)
        except ValueError:
            continue
        if path.exists():
            return path
    return ensure_within(roots[0] / relpath, roots[0])


def cmd_files(job_id: str, cfg: BuildupConfig) -> List[str]:
    """List files in the job workspace and every bound directory."""
    from buildup.paths import list_files
    from buildup.sandbox import job_read_roots

    roots = job_read_roots(job_id, cfg)
    entries = list(list_files(roots[0]))
    for root in roots[1:]:
        entries.extend(f"{root}/{name}" for name in list_files(root))
    return entries


def cmd_write(job_id: str, relpath: str, content: str, cfg: BuildupConfig) -> Path:
    """Create or overwrite a text file inside the job workspace.

    Enforces sandbox: path must be inside workspace/{job_id}/.
    """
    from buildup.sandbox import require_write

    if not relpath:
        raise ValueError("파일 경로(relpath)가 지정되지 않았습니다.")

    path = resolve_job_file(job_id, relpath, cfg, for_write=True)

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
    cfg: BuildupConfig,
) -> str:
    """Replace *old_string* with *new_string* inside a file (exact match).

    Enforces sandbox.  Returns a unified diff of the change.
    Raises ValueError if old_string is not found or is ambiguous (multiple matches).
    """
    from buildup.paths import compute_diff
    from buildup.sandbox import require_read, require_write

    if not relpath:
        raise ValueError("파일 경로(relpath)가 지정되지 않았습니다.")

    path = resolve_job_file(job_id, relpath, cfg, for_write=True)

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


def cmd_read(job_id: str, relpath: str, cfg: BuildupConfig) -> str:
    """Read a text or PDF file from the job workspace.

    Enforces sandbox: path must be inside workspace/{job_id}/.
    """
    from buildup.sandbox import require_read

    path = resolve_job_file(job_id, relpath, cfg)

    # Validate through sandbox
    require_read(path, cfg, context=f"read {relpath}")

    if is_pdf_file(path):
        return read_pdf_file(path, cfg)
    if not is_text_file(path, cfg):
        raise ValueError(f"지원하지 않는 파일 형식: {path.suffix}\n텍스트 또는 PDF만 읽기 가능합니다.")
    return read_text_file(path, cfg)
