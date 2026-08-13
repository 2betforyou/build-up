"""Open WebUI Knowledge Base integration: upload, sync, and query."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from uuid import uuid4

import requests

from friday.config import FridayConfig
from friday.state import job_dir


def owui_headers(cfg: FridayConfig) -> Dict[str, str]:
    """Build authorisation headers for Open WebUI API."""
    if not cfg.openwebui_token:
        raise ValueError("OPENWEBUI_TOKEN 환경변수가 설정되지 않았습니다.")
    return {"Authorization": f"Bearer {cfg.openwebui_token}"}


def owui_upload_file(path: Path, session: requests.Session, cfg: FridayConfig) -> str:
    """Upload a single file to Open WebUI.  Returns file_id."""
    url = f"{cfg.openwebui_base_url}/api/v1/files/"
    with path.open("rb") as f:
        resp = session.post(
            url, headers={**owui_headers(cfg), "Accept": "application/json"},
            files={"file": f}, timeout=cfg.openwebui_timeout,
        )
    resp.raise_for_status()
    data = resp.json()
    file_id = data.get("id") or data.get("file", {}).get("id")
    if not file_id:
        raise ValueError(f"업로드 응답에서 file id를 찾을 수 없습니다: {data}")
    return file_id


def owui_wait_for_file_processed(
    file_id: str, session: requests.Session, cfg: FridayConfig,
) -> None:
    """Poll until the uploaded file is processed (or timeout)."""
    url = f"{cfg.openwebui_base_url}/api/v1/files/{file_id}/process/status"
    start = time.time()
    last_payload = None
    while time.time() - start < cfg.openwebui_process_timeout:
        try:
            resp = session.get(url, headers=owui_headers(cfg), timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            logging.getLogger("friday").warning("OWUI status check failed: %s", exc)
            time.sleep(cfg.openwebui_poll_interval)
            continue
        last_payload = payload
        status = str(payload.get("status") or payload.get("state") or "").lower()
        if status in {"processed", "completed", "done", "success"}:
            return
        if status in {"failed", "error"}:
            raise ValueError(f"Open WebUI 파일 처리 실패: {payload}")
        time.sleep(cfg.openwebui_poll_interval)
    raise TimeoutError(f"Open WebUI 파일 처리 대기 시간 초과: {last_payload}")


def owui_attach_file_to_kb(
    kb_id: str, file_id: str, session: requests.Session, cfg: FridayConfig,
) -> Dict:
    """Attach an uploaded file to a Knowledge Base."""
    url = f"{cfg.openwebui_base_url}/api/v1/knowledge/{kb_id}/file/add"
    resp = session.post(url, headers=owui_headers(cfg), json={"file_id": file_id}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def owui_sync_file_to_kb(
    path: Path, kb_id: str, session: requests.Session, cfg: FridayConfig,
) -> str:
    """Upload → wait → attach a file to a KB.  Returns file_id."""
    file_id = owui_upload_file(path, session, cfg)
    owui_wait_for_file_processed(file_id, session, cfg)
    owui_attach_file_to_kb(kb_id, file_id, session, cfg)
    return file_id


def sync_job_to_openwebui(
    job_id: str, kb_key: str, session: requests.Session, cfg: FridayConfig,
) -> List[Tuple[str, str]]:
    """Sync all files in a job directory to an Open WebUI KB."""
    kb_id = cfg.openwebui_kb_map.get(kb_key, "")
    if not kb_id:
        raise ValueError(f"OPENWEBUI_KB_{kb_key.upper()} 환경변수를 설정하십시오.")
    base = job_dir(job_id, cfg)
    synced: List[Tuple[str, str]] = []
    for fp in sorted(base.rglob("*")):
        if fp.is_symlink() or fp.is_dir():
            continue
        fid = owui_sync_file_to_kb(fp, kb_id, session, cfg)
        synced.append((str(fp.relative_to(base)), fid))
    return synced


def ask_openwebui_with_kb(
    question: str, model: str, kb_id: str,
    session: requests.Session, cfg: FridayConfig,
) -> requests.Response:
    """Ask a question using an Open WebUI Knowledge Base."""
    url = f"{cfg.openwebui_base_url}/api/chat/completions"
    payload = {
        "chat_id": str(uuid4()), "id": str(uuid4()),
        "messages": [{"role": "user", "content": question}],
        "model": model, "stream": False,
        "files": [{"id": kb_id, "type": "collection", "status": "processed"}],
        "features": {"code_interpreter": False, "web_search": False,
                      "image_generation": False, "memory": False},
        "background_tasks": {"title_generation": False, "tags_generation": False,
                              "follow_up_generation": False},
        "session_id": str(uuid4()),
    }
    resp = session.post(
        url, headers={**owui_headers(cfg), "Content-Type": "application/json"},
        json=payload, timeout=cfg.openwebui_timeout,
    )
    resp.raise_for_status()
    return resp


def extract_chat_completion_text(payload: Dict[str, Any]) -> str:
    """Extract text from an Open WebUI chat completion response."""
    if isinstance(payload.get("choices"), list) and payload["choices"]:
        choice = payload["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                item.get("text", "") for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
    if isinstance(payload.get("message"), dict):
        content = payload["message"].get("content")
        if isinstance(content, str):
            return content
    return json.dumps(payload, ensure_ascii=False, indent=2)
