"""Atomic state persistence (state.json) and job directory helpers."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from friday.config import FridayConfig
from friday.paths import validate_job_id


def load_state(cfg: FridayConfig) -> Dict[str, Any]:
    """Load state.json, creating it and all directories if needed."""
    for d in cfg.all_dirs:
        d.mkdir(parents=True, exist_ok=True)
    if not cfg.state_file.exists():
        state: Dict[str, Any] = {"current_job": None}
        save_state(state, cfg)
        return state
    try:
        raw = cfg.state_file.read_text(encoding="utf-8")
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError("state.json 형식 오류")
        return state
    except (json.JSONDecodeError, ValueError) as exc:
        logging.getLogger("friday").warning("state.json 손상 — 초기화: %s", exc)
        state = {"current_job": None}
        save_state(state, cfg)
        return state


def save_state(state: Dict[str, Any], cfg: FridayConfig) -> None:
    """Atomically write state.json (tmpfile → rename)."""
    cfg.base_dir.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(cfg.base_dir), prefix=".state_", suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(cfg.state_file))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_current_job(cfg: FridayConfig, required: bool = True) -> Optional[str]:
    """Return current active job ID, optionally raising if none."""
    state = load_state(cfg)
    job = state.get("current_job")
    if required and not job:
        raise ValueError(
            "현재 활성 job이 없습니다. 'friday job new' 또는 '/job new'를 실행하십시오."
        )
    return job


def set_current_job(job_id: str, cfg: FridayConfig) -> None:
    """Set the active job in state.json."""
    state = load_state(cfg)
    state["current_job"] = validate_job_id(job_id)
    save_state(state, cfg)


def job_dir(job_id: str, cfg: FridayConfig) -> Path:
    """Return (and create) the workspace directory for a job."""
    path = cfg.workspace_dir / validate_job_id(job_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
