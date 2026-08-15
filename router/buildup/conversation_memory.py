"""Durable conversation memory for Build-up.

Session history is a short context window plus saved raw session logs.  This
module keeps a small, append-only memory of important user preferences,
project decisions, and Build-up configuration choices across sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from buildup.config import BuildupConfig


MAX_TEXT_CHARS = 900
MEMORY_CONTEXT_MAX_CHARS = 3_500


def _memory_dir(cfg: BuildupConfig) -> Path:
    path = cfg.buildup_data_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def memory_jsonl_path(cfg: BuildupConfig) -> Path:
    return _memory_dir(cfg) / "conversation_memory.jsonl"


def memory_markdown_path(cfg: BuildupConfig) -> Path:
    return _memory_dir(cfg) / "conversation_memory.md"


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _signature(user_text: str, assistant_text: str) -> str:
    raw = (user_text.strip() + "\n---\n" + assistant_text.strip()).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_entries(cfg: BuildupConfig) -> List[Dict[str, Any]]:
    path = memory_jsonl_path(cfg)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return entries


def _category(user_text: str, assistant_text: str) -> Optional[str]:
    text = f"{user_text}\n{assistant_text}".lower()
    if re.search(r"기억|remember|앞으로|항상|선호|prefs?|취향|하지\s*마|하지마", text, re.I):
        return "preference"
    if re.search(r"buildup_research_model|research_model|reviewer_model|qwen|gemma|deepseek|모델|리서치", text, re.I):
        return "model_config"
    if re.search(r"steer|steering|프로필|critical|research|builder", text, re.I):
        return "steering"
    if re.search(r"논문|paper|arxiv|pdf|abstract|초록|memory\.md|review\.md", text, re.I):
        return "research_workflow"
    if re.search(r"수정했어|고쳤어|바꿨어|추가했어|반영했어|설정|기본값|default", assistant_text, re.I):
        return "project_decision"
    return None


def should_capture_memory(user_text: str, assistant_text: str) -> bool:
    """Return True when a user/assistant pair likely contains durable memory."""
    if not user_text.strip() or not assistant_text.strip():
        return False
    if user_text.lstrip().startswith("/"):
        return False
    if "agent error" in assistant_text.lower():
        return False
    return _category(user_text, assistant_text) is not None


def _summary_for(category: str, user_text: str, assistant_text: str) -> str:
    user = _clip(user_text, 220)
    assistant = _clip(assistant_text, 260)
    labels = {
        "preference": "사용자 선호/지시",
        "model_config": "모델/라우팅 결정",
        "steering": "Steering 사용 방식",
        "research_workflow": "build-up/논문 워크플로",
        "project_decision": "프로젝트 구현 결정",
    }
    label = labels.get(category, "중요 대화")
    return f"{label}: 사용자: {user} / build-up: {assistant}"


def append_conversation_memory(
    cfg: BuildupConfig,
    user_text: str,
    assistant_text: str,
    *,
    session_id: str = "",
    job_id: Optional[str] = None,
    workspace_key: str = "",
    scope: str = "workspace",
    manual: bool = False,
) -> Optional[Dict[str, Any]]:
    """Append one durable memory entry and refresh the Markdown view."""
    if scope not in {"user", "workspace"}:
        raise ValueError("memory scope는 user 또는 workspace여야 합니다.")
    category = _category(user_text, assistant_text) or ("manual" if manual else None)
    if category is None:
        return None

    sig = _signature(user_text, assistant_text)
    entries = _load_entries(cfg)
    if any(entry.get("signature") == sig for entry in entries):
        return None

    entry: Dict[str, Any] = {
        "id": sig,
        "signature": sig,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "category": category,
        "session_id": session_id,
        "job_id": job_id,
        "workspace_key": workspace_key or (f"job:{job_id}" if job_id else ""),
        "scope": scope,
        "summary": _summary_for(category, user_text, assistant_text),
        "user": _clip(user_text),
        "assistant": _clip(assistant_text),
        "manual": manual,
    }

    path = memory_jsonl_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    refresh_memory_markdown(cfg)
    return entry


def refresh_memory_markdown(cfg: BuildupConfig) -> Path:
    """Write a readable Markdown projection of the JSONL memory."""
    entries = _load_entries(cfg)
    lines = [
        "# build-up Conversation Memory",
        "",
        "자동 저장된 장기 메모리입니다. 원문 세션 로그가 아니라 중요한 결정/선호만 모읍니다.",
        "",
    ]
    for entry in entries[-200:]:
        lines.append(f"## {entry.get('created_at', '')} [{entry.get('category', 'memory')}]")
        lines.append(f"- scope: `{entry.get('scope', 'legacy')}`")
        if entry.get("workspace_key"):
            lines.append(f"- workspace: `{entry['workspace_key']}`")
        if entry.get("job_id"):
            lines.append(f"- job: `{entry['job_id']}`")
        if entry.get("session_id"):
            lines.append(f"- session: `{entry['session_id']}`")
        lines.append(f"- summary: {entry.get('summary', '')}")
        lines.append("")

    target = memory_markdown_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".memory_", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def _entry_visible(entry: Dict[str, Any], workspace_key: str) -> bool:
    scope = entry.get("scope")
    if scope == "user":
        return True
    if scope == "workspace":
        return bool(workspace_key and entry.get("workspace_key") == workspace_key)
    # Legacy entries are never promoted to global memory implicitly. A legacy
    # job entry is visible only from the same job workspace.
    legacy_job = entry.get("job_id")
    return bool(legacy_job and workspace_key == f"job:{legacy_job}")


def load_conversation_memory(
    cfg: BuildupConfig,
    *,
    workspace_key: str = "",
    max_chars: int = MEMORY_CONTEXT_MAX_CHARS,
) -> str:
    """Return user + current-workspace memory for a session-start snapshot."""
    entries = [
        entry for entry in _load_entries(cfg)
        if _entry_visible(entry, workspace_key)
    ]
    if not entries:
        return ""
    lines = ["[build-up durable conversation memory]"]
    for entry in entries[-40:]:
        lines.append(f"- ({entry.get('category', 'memory')}) {entry.get('summary', '')}")
    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        text = text[-max_chars:].lstrip()
        text = "[build-up durable conversation memory]\n...(older memory truncated)\n" + text
    return text


def format_conversation_memory(
    cfg: BuildupConfig,
    *,
    limit: int = 12,
    workspace_key: str = "",
    include_all: bool = False,
) -> str:
    entries = _load_entries(cfg)
    if not include_all:
        entries = [entry for entry in entries if _entry_visible(entry, workspace_key)]
    if not entries:
        return "아직 저장된 conversation memory가 없어."
    lines = [
        f"path: {memory_markdown_path(cfg)}",
        "",
    ]
    for entry in entries[-limit:]:
        scope = entry.get("scope", "legacy")
        lines.append(
            f"- {entry.get('created_at', '')} [{scope}/{entry.get('category', 'memory')}] "
            f"{entry.get('summary', '')}"
        )
    return "\n".join(lines)
