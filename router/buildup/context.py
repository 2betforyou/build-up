"""Shared context builder for Build-up's agent and planner.

Both the legacy ReAct agent (agent.py) and the constrained planner
(planner.py) need the same runtime context snapshot: current date,
current job, and file list.

Keeping this in one place avoids the circular import that would occur
if planner.py imported directly from agent.py.
"""

from __future__ import annotations

from typing import Dict

from buildup.config import BuildupConfig
from pathlib import Path


def load_user_prefs(cfg: BuildupConfig) -> str:
    """Load ~/.buildup_prefs.md — user-level personalization.

    Returns empty string if file does not exist.
    """
    path = Path.home() / ".buildup_prefs.md"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


def load_job_prefs(cfg: BuildupConfig) -> str:
    """Load .buildup.md from the current job folder — project-level context.

    Returns empty string if no job is active or file does not exist.
    """
    from buildup.state import get_current_job, job_dir as _job_dir
    job_id = get_current_job(cfg, required=False)
    if not job_id:
        return ""
    path = _job_dir(job_id, cfg) / ".buildup.md"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


def build_context(cfg: BuildupConfig) -> Dict[str, str]:
    """Return a dict with runtime context fields used in agent/planner prompts.

    Keys: date_ctx, current_job, file_list, exemplar_context,
          tool_descriptions.

    ``exemplar_context`` and ``tool_descriptions`` are set to empty strings
    here; callers are expected to fill them in before building the final
    prompt string.
    """
    from buildup.paths import list_files
    from buildup.prompts import date_context
    from buildup.state import get_current_job

    current_job = get_current_job(cfg, required=False) or "(없음)"

    file_list = "(없음)"
    if current_job != "(없음)":
        try:
            files = list_files(cfg.workspace_dir / current_job)
            file_list = ", ".join(files[:20]) if files else "(비어 있음)"
        except Exception:
            file_list = "(알 수 없음)"

    job_prefs = load_job_prefs(cfg)

    return {
        "date_ctx": date_context(),
        "current_job": current_job,
        "file_list": file_list,
        "job_prefs": job_prefs,    # from .buildup.md in current job folder
        "exemplar_context": "",    # filled in by caller if exemplar index available
        "tool_descriptions": "",   # filled in by caller with TOOL_DESCRIPTIONS constant
    }
