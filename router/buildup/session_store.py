"""Durable, workspace-scoped session storage for build-up.

SQLite is the source of truth.  Existing ``sessions/*.session.json`` files are
imported once and intentionally kept as a rollback copy.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from buildup.config import BuildupConfig


SCHEMA_VERSION = 1


@dataclass
class SessionInfo:
    session_id: str
    created_at: str
    updated_at: str
    job_id: Optional[str]
    title: str
    turn_count: int
    messages: List[Dict[str, Any]]
    assistant_mode: str = "research"
    response_mode: str = "auto"
    active_paper_id: Optional[str] = None
    paper_reviewer_mode: bool = False
    steering_profile: str = "default"
    steering_directives: List[str] = field(default_factory=list)
    workspace_key: str = ""
    kind: str = "conversation"
    status: str = "active"
    parent_session_id: Optional[str] = None
    ended_at: Optional[str] = None
    archived: bool = False
    context_summary: str = ""
    active_study_id: Optional[str] = None
    system_prompt_snapshot: str = ""
    model_config: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionSearchHit:
    session: SessionInfo
    snippet: str
    match_role: str = ""


def new_session_id() -> str:
    return str(uuid.uuid4())[:12]


def workspace_key_for(
    cfg: BuildupConfig,
    job_id: Optional[str] = None,
    *,
    cwd: Optional[Path] = None,
) -> str:
    """Return a stable workspace identity (job first, then git root/cwd)."""
    if job_id:
        return f"job:{job_id}"
    current = (cwd or Path.cwd()).expanduser().resolve()
    root = current
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            root = candidate
            break
    return f"path:{root}"


def workspace_label(workspace_key: str) -> str:
    if workspace_key.startswith("job:"):
        return workspace_key[4:]
    if workspace_key.startswith("path:"):
        return Path(workspace_key[5:]).name or workspace_key[5:]
    return workspace_key or "-"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> List[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _connect(cfg: BuildupConfig) -> sqlite3.Connection:
    cfg.session_db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cfg.session_db_file), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    _ensure_schema(conn)
    _migrate_legacy_json(conn, cfg)
    return conn


@contextmanager
def _db(cfg: BuildupConfig) -> Iterator[sqlite3.Connection]:
    """Open one transaction and always release the SQLite connection."""
    conn = _connect(cfg)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            workspace_key TEXT NOT NULL,
            job_id TEXT,
            parent_session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
            kind TEXT NOT NULL DEFAULT 'conversation',
            status TEXT NOT NULL DEFAULT 'active',
            title TEXT NOT NULL DEFAULT '',
            title_locked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT,
            archived INTEGER NOT NULL DEFAULT 0,
            context_json TEXT NOT NULL DEFAULT '{}',
            system_prompt_snapshot TEXT NOT NULL DEFAULT '',
            model_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS sessions_workspace_updated_idx
            ON sessions(workspace_key, archived, updated_at DESC);
        CREATE INDEX IF NOT EXISTS sessions_parent_idx
            ON sessions(parent_session_id);

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_call_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(session_id, sequence)
        );

        CREATE INDEX IF NOT EXISTS messages_session_sequence_idx
            ON messages(session_id, sequence);
        """
    )
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
            "USING fts5(content, session_id UNINDEXED, role UNINDEXED)"
        )
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS messages_fts_insert
            AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, session_id, role)
                VALUES (new.id, new.content, new.session_id, new.role);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_delete
            AFTER DELETE ON messages BEGIN
                DELETE FROM messages_fts WHERE rowid = old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_update
            AFTER UPDATE ON messages BEGIN
                DELETE FROM messages_fts WHERE rowid = old.id;
                INSERT INTO messages_fts(rowid, content, session_id, role)
                VALUES (new.id, new.content, new.session_id, new.role);
            END;
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('fts5', '1')"
        )
    except sqlite3.OperationalError:
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('fts5', '0')"
        )
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _legacy_info(data: Dict[str, Any], cfg: BuildupConfig) -> SessionInfo:
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    directives = context.get("steering_directives", [])
    if not isinstance(directives, list):
        directives = []
    job_id = data.get("job_id")
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    return SessionInfo(
        session_id=str(data["session_id"]),
        created_at=str(data.get("created_at") or _now()),
        updated_at=str(data.get("updated_at") or data.get("created_at") or _now()),
        job_id=str(job_id) if job_id else None,
        title=str(data.get("title") or ""),
        turn_count=sum(1 for item in messages if isinstance(item, dict) and item.get("role") == "user"),
        messages=[dict(item) for item in messages if isinstance(item, dict)],
        assistant_mode=str(context.get("assistant_mode") or "research"),
        response_mode=str(context.get("response_mode") or "auto"),
        active_paper_id=context.get("active_paper_id"),
        paper_reviewer_mode=bool(context.get("paper_reviewer_mode", False)),
        steering_profile=str(context.get("steering_profile") or "default"),
        steering_directives=[str(item) for item in directives if str(item).strip()],
        workspace_key=str(data.get("workspace_key") or workspace_key_for(cfg, job_id)),
        kind=str(data.get("kind") or "conversation"),
        status=str(data.get("status") or "ended"),
        parent_session_id=data.get("parent_session_id"),
        ended_at=data.get("ended_at"),
        archived=bool(data.get("archived", False)),
        context_summary=str(context.get("context_summary") or ""),
        active_study_id=context.get("active_study_id"),
        system_prompt_snapshot=str(data.get("system_prompt_snapshot") or ""),
        model_config=_json_dict(data.get("model_config")),
    )


def _insert_info(conn: sqlite3.Connection, info: SessionInfo, *, title_locked: bool) -> None:
    context = {
        "assistant_mode": info.assistant_mode,
        "response_mode": info.response_mode,
        "active_paper_id": info.active_paper_id,
        "paper_reviewer_mode": info.paper_reviewer_mode,
        "steering_profile": info.steering_profile,
        "steering_directives": list(info.steering_directives),
        "context_summary": info.context_summary,
        "active_study_id": info.active_study_id,
    }
    conn.execute(
        """
        INSERT OR IGNORE INTO sessions(
            session_id, workspace_key, job_id, parent_session_id, kind, status,
            title, title_locked, created_at, updated_at, ended_at, archived,
            context_json, system_prompt_snapshot, model_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            info.session_id, info.workspace_key, info.job_id, info.parent_session_id,
            info.kind, info.status, info.title, int(title_locked), info.created_at,
            info.updated_at, info.ended_at, int(info.archived),
            json.dumps(context, ensure_ascii=False), info.system_prompt_snapshot,
            json.dumps(info.model_config, ensure_ascii=False),
        ),
    )
    exists = conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (info.session_id,)
    ).fetchone()
    if exists:
        return
    for sequence, message in enumerate(info.messages):
        role = str(message.get("role") or "assistant")
        content = str(message.get("content") or "")
        metadata = {
            key: value for key, value in message.items()
            if key not in {"role", "content", "tool_name", "tool_call_id", "created_at"}
        }
        conn.execute(
            """
            INSERT INTO messages(
                session_id, sequence, role, content, tool_name, tool_call_id,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                info.session_id, sequence, role, content,
                message.get("tool_name"), message.get("tool_call_id"),
                json.dumps(metadata, ensure_ascii=False),
                str(message.get("created_at") or info.updated_at),
            ),
        )


def _migrate_legacy_json(conn: sqlite3.Connection, cfg: BuildupConfig) -> None:
    marker = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'legacy_json_migration_v1'"
    ).fetchone()
    if marker:
        return
    legacy_dir = cfg.base_dir / "sessions"
    if legacy_dir.exists():
        for path in sorted(legacy_dir.glob("*.session.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                info = _legacy_info(data, cfg)
                _insert_info(conn, info, title_locked=bool(info.title))
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                continue
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('legacy_json_migration_v1', ?)",
        (_now(),),
    )
    conn.commit()


def _default_title(messages: Iterable[Dict[str, Any]]) -> str:
    first = next(
        (str(message.get("content") or "") for message in messages if message.get("role") == "user"),
        "",
    )
    clean = re.sub(r"\s+", " ", first).strip()
    clean = "".join(char for char in clean if char.isprintable())
    return clean[:80] or "(빈 대화)"


def save_session(
    session_id: str,
    messages: List[Dict[str, Any]],
    job_id: Optional[str],
    cfg: BuildupConfig,
    created_at: Optional[str] = None,
    *,
    assistant_mode: str = "research",
    response_mode: str = "auto",
    active_paper_id: Optional[str] = None,
    paper_reviewer_mode: bool = False,
    steering_profile: str = "default",
    steering_directives: Optional[List[str]] = None,
    workspace_key: Optional[str] = None,
    kind: str = "conversation",
    status: str = "active",
    parent_session_id: Optional[str] = None,
    context_summary: str = "",
    active_study_id: Optional[str] = None,
    system_prompt_snapshot: str = "",
    model_config: Optional[Dict[str, Any]] = None,
    persist_empty: bool = False,
) -> None:
    """Persist one complete transcript atomically.

    Empty sessions stay ephemeral, matching the previous user-facing behavior.
    """
    if not messages and not persist_empty:
        return
    now = _now()
    key = workspace_key or workspace_key_for(cfg, job_id)
    context = {
        "assistant_mode": assistant_mode,
        "response_mode": response_mode,
        "active_paper_id": active_paper_id,
        "paper_reviewer_mode": bool(paper_reviewer_mode),
        "steering_profile": steering_profile,
        "steering_directives": list(steering_directives or []),
        "context_summary": context_summary,
        "active_study_id": active_study_id,
    }
    with _db(cfg) as conn:
        previous = conn.execute(
            "SELECT title, title_locked, created_at, ended_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        title = str(previous["title"]) if previous and previous["title"] else _default_title(messages)
        locked = int(previous["title_locked"]) if previous else 0
        session_created = created_at or (str(previous["created_at"]) if previous else now)
        ended_at = str(previous["ended_at"]) if previous and previous["ended_at"] else None
        if status in {"ended", "interrupted", "failed"} and not ended_at:
            ended_at = now
        if status == "active":
            ended_at = None
        conn.execute(
            """
            INSERT INTO sessions(
                session_id, workspace_key, job_id, parent_session_id, kind, status,
                title, title_locked, created_at, updated_at, ended_at, archived,
                context_json, system_prompt_snapshot, model_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                workspace_key=excluded.workspace_key,
                job_id=excluded.job_id,
                parent_session_id=excluded.parent_session_id,
                kind=excluded.kind,
                status=excluded.status,
                updated_at=excluded.updated_at,
                ended_at=excluded.ended_at,
                context_json=excluded.context_json,
                system_prompt_snapshot=excluded.system_prompt_snapshot,
                model_json=excluded.model_json
            """,
            (
                session_id, key, job_id, parent_session_id, kind, status, title,
                locked, session_created, now, ended_at,
                json.dumps(context, ensure_ascii=False), system_prompt_snapshot,
                json.dumps(model_config or {}, ensure_ascii=False),
            ),
        )
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        for sequence, message in enumerate(messages):
            role = str(message.get("role") or "assistant")
            content = str(message.get("content") or "")
            metadata = {
                key: value for key, value in message.items()
                if key not in {"role", "content", "tool_name", "tool_call_id", "created_at"}
            }
            conn.execute(
                """
                INSERT INTO messages(
                    session_id, sequence, role, content, tool_name, tool_call_id,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, sequence, role, content,
                    message.get("tool_name"), message.get("tool_call_id"),
                    json.dumps(metadata, ensure_ascii=False),
                    str(message.get("created_at") or now),
                ),
            )


def _row_to_info(conn: sqlite3.Connection, row: sqlite3.Row) -> SessionInfo:
    context = _json_dict(row["context_json"])
    message_rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY sequence",
        (row["session_id"],),
    ).fetchall()
    messages: List[Dict[str, Any]] = []
    for message in message_rows:
        item: Dict[str, Any] = {
            "role": message["role"],
            "content": message["content"],
        }
        if message["tool_name"]:
            item["tool_name"] = message["tool_name"]
        if message["tool_call_id"]:
            item["tool_call_id"] = message["tool_call_id"]
        metadata = _json_dict(message["metadata_json"])
        item.update(metadata)
        if message["created_at"]:
            item["created_at"] = message["created_at"]
        messages.append(item)
    directives = context.get("steering_directives", [])
    if not isinstance(directives, list):
        directives = []
    return SessionInfo(
        session_id=row["session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        job_id=row["job_id"],
        title=row["title"],
        turn_count=sum(1 for item in messages if item.get("role") == "user"),
        messages=messages,
        assistant_mode=str(context.get("assistant_mode") or "research"),
        response_mode=str(context.get("response_mode") or "auto"),
        active_paper_id=context.get("active_paper_id"),
        paper_reviewer_mode=bool(context.get("paper_reviewer_mode", False)),
        steering_profile=str(context.get("steering_profile") or "default"),
        steering_directives=[str(item) for item in directives if str(item).strip()],
        workspace_key=row["workspace_key"],
        kind=row["kind"],
        status=row["status"],
        parent_session_id=row["parent_session_id"],
        ended_at=row["ended_at"],
        archived=bool(row["archived"]),
        context_summary=str(context.get("context_summary") or ""),
        active_study_id=context.get("active_study_id"),
        system_prompt_snapshot=row["system_prompt_snapshot"],
        model_config=_json_dict(row["model_json"]),
    )


def load_session(session_id: str, cfg: BuildupConfig) -> Optional[SessionInfo]:
    with _db(cfg) as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return _row_to_info(conn, row) if row else None


def list_sessions(
    cfg: BuildupConfig,
    limit: int = 30,
    *,
    workspace_key: Optional[str] = None,
    include_archived: bool = False,
    include_children: bool = False,
) -> List[SessionInfo]:
    clauses: List[str] = []
    params: List[Any] = []
    if workspace_key is not None:
        clauses.append("workspace_key = ?")
        params.append(workspace_key)
    if not include_archived:
        clauses.append("archived = 0")
    if not include_children:
        clauses.append("parent_session_id IS NULL")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, int(limit)))
    with _db(cfg) as conn:
        rows = conn.execute(
            f"SELECT * FROM sessions{where} ORDER BY updated_at DESC LIMIT ?", params
        ).fetchall()
        return [_row_to_info(conn, row) for row in rows]


def load_latest_session(
    cfg: BuildupConfig,
    *,
    workspace_key: Optional[str] = None,
) -> Optional[SessionInfo]:
    sessions = list_sessions(cfg, limit=1, workspace_key=workspace_key)
    return sessions[0] if sessions else None


def find_session_by_prefix(
    prefix: str,
    cfg: BuildupConfig,
    *,
    workspace_key: Optional[str] = None,
) -> Optional[SessionInfo]:
    clauses = ["session_id LIKE ?", "archived = 0"]
    params: List[Any] = [prefix + "%"]
    if workspace_key is not None:
        clauses.append("workspace_key = ?")
        params.append(workspace_key)
    with _db(cfg) as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT 2",
            params,
        ).fetchall()
        return _row_to_info(conn, rows[0]) if len(rows) == 1 else None


def resolve_session(
    selector: str,
    cfg: BuildupConfig,
    *,
    workspace_key: Optional[str] = None,
) -> Optional[SessionInfo]:
    value = selector.strip()
    if not value:
        return None
    if value.lower() in {"latest", "최근", "마지막"}:
        return load_latest_session(cfg, workspace_key=workspace_key)
    exact = load_session(value, cfg)
    if exact and not exact.archived and (workspace_key is None or exact.workspace_key == workspace_key):
        return exact
    if value.isdigit():
        sessions = list_sessions(cfg, workspace_key=workspace_key)
        index = int(value)
        return sessions[index - 1] if 1 <= index <= len(sessions) else None
    with _db(cfg) as conn:
        clauses = ["lower(title) = lower(?)", "archived = 0"]
        params: List[Any] = [value]
        if workspace_key is not None:
            clauses.append("workspace_key = ?")
            params.append(workspace_key)
        row = conn.execute(
            "SELECT * FROM sessions WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT 1",
            params,
        ).fetchone()
        if row:
            return _row_to_info(conn, row)
    prefix_hit = find_session_by_prefix(value, cfg, workspace_key=workspace_key)
    if prefix_hit is not None:
        return prefix_hit
    return _find_session_by_title_fragment(value, cfg, workspace_key=workspace_key)


def _find_session_by_title_fragment(
    fragment: str,
    cfg: BuildupConfig,
    *,
    workspace_key: Optional[str] = None,
) -> Optional[SessionInfo]:
    """Fall back to a fuzzy, unique 'contains' match on the title.

    Lets a user type a recognisable fragment ("논문 정리" instead of the full
    saved title) the way `/resume` already lets them pick from a list.
    """
    with _db(cfg) as conn:
        clauses = ["lower(title) LIKE '%' || lower(?) || '%'", "archived = 0"]
        params: List[Any] = [fragment]
        if workspace_key is not None:
            clauses.append("workspace_key = ?")
            params.append(workspace_key)
        rows = conn.execute(
            "SELECT * FROM sessions WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT 2",
            params,
        ).fetchall()
        return _row_to_info(conn, rows[0]) if len(rows) == 1 else None


def rename_session(session_id: str, new_title: str, cfg: BuildupConfig) -> bool:
    title = re.sub(r"\s+", " ", new_title).strip()
    title = "".join(char for char in title if char.isprintable())[:100]
    if not title:
        return False
    with _db(cfg) as conn:
        current = conn.execute(
            "SELECT workspace_key FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not current:
            return False
        duplicate = conn.execute(
            """
            SELECT 1 FROM sessions
            WHERE workspace_key = ? AND lower(title) = lower(?)
              AND session_id != ? AND archived = 0
            LIMIT 1
            """,
            (current["workspace_key"], title, session_id),
        ).fetchone()
        if duplicate:
            return False
        conn.execute(
            "UPDATE sessions SET title = ?, title_locked = 1, updated_at = ? WHERE session_id = ?",
            (title, _now(), session_id),
        )
        return True


def mark_session_status(session_id: str, status: str, cfg: BuildupConfig) -> bool:
    if status not in {"active", "ended", "interrupted", "failed"}:
        raise ValueError(f"지원하지 않는 세션 상태: {status}")
    now = _now()
    ended_at = None if status == "active" else now
    with _db(cfg) as conn:
        cursor = conn.execute(
            "UPDATE sessions SET status = ?, ended_at = ?, updated_at = ? WHERE session_id = ?",
            (status, ended_at, now, session_id),
        )
        return cursor.rowcount > 0


def archive_session(session_id: str, cfg: BuildupConfig, *, archived: bool = True) -> bool:
    with _db(cfg) as conn:
        cursor = conn.execute(
            "UPDATE sessions SET archived = ?, updated_at = ? WHERE session_id = ?",
            (int(archived), _now(), session_id),
        )
        return cursor.rowcount > 0


def delete_session(session_id: str, cfg: BuildupConfig) -> bool:
    """Permanently delete a session. UI callers should prefer archive_session."""
    with _db(cfg) as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        return cursor.rowcount > 0


def _safe_fts_query(query: str) -> str:
    tokens = re.findall(r"[0-9A-Za-z_가-힣]+", query)
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens[:12])


def search_sessions(
    query: str,
    cfg: BuildupConfig,
    *,
    workspace_key: Optional[str] = None,
    limit: int = 10,
    include_archived: bool = False,
) -> List[SessionSearchHit]:
    text = query.strip()
    if not text:
        return []
    with _db(cfg) as conn:
        fts = conn.execute("SELECT value FROM schema_meta WHERE key = 'fts5'").fetchone()
        rows: List[sqlite3.Row] = []
        fts_query = _safe_fts_query(text)
        if fts and fts["value"] == "1" and fts_query:
            clauses = ["messages_fts MATCH ?"]
            params: List[Any] = [fts_query]
            if workspace_key is not None:
                clauses.append("s.workspace_key = ?")
                params.append(workspace_key)
            if not include_archived:
                clauses.append("s.archived = 0")
            params.append(max(1, limit * 5))
            try:
                rows = conn.execute(
                    """
                    SELECT s.*, messages_fts.role AS match_role,
                           snippet(messages_fts, 0, '[', ']', '…', 18) AS match_snippet
                    FROM messages_fts
                    JOIN sessions s ON s.session_id = messages_fts.session_id
                    WHERE """ + " AND ".join(clauses) +
                    " ORDER BY bm25(messages_fts), s.updated_at DESC LIMIT ?",
                    params,
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            clauses = ["(lower(m.content) LIKE lower(?) OR lower(s.title) LIKE lower(?))"]
            like = f"%{text}%"
            params = [like, like]
            if workspace_key is not None:
                clauses.append("s.workspace_key = ?")
                params.append(workspace_key)
            if not include_archived:
                clauses.append("s.archived = 0")
            params.append(max(1, limit * 5))
            rows = conn.execute(
                """
                SELECT s.*, m.role AS match_role, substr(m.content, 1, 240) AS match_snippet
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.session_id
                WHERE """ + " AND ".join(clauses) +
                " ORDER BY s.updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        hits: List[SessionSearchHit] = []
        seen: set[str] = set()
        for row in rows:
            session_id = str(row["session_id"])
            if session_id in seen:
                continue
            seen.add(session_id)
            hits.append(SessionSearchHit(
                session=_row_to_info(conn, row),
                snippet=str(row["match_snippet"] or row["title"]),
                match_role=str(row["match_role"] or ""),
            ))
            if len(hits) >= limit:
                break
        return hits


def export_session(
    session_id: str,
    cfg: BuildupConfig,
    *,
    fmt: str = "md",
    destination: Optional[Path] = None,
) -> Path:
    info = load_session(session_id, cfg)
    if info is None:
        raise ValueError(f"세션을 찾지 못했습니다: {session_id}")
    fmt = fmt.lower()
    if fmt not in {"md", "json"}:
        raise ValueError("세션 export 형식은 md 또는 json이어야 합니다.")
    cfg.export_dir.mkdir(parents=True, exist_ok=True)
    target = destination or (cfg.export_dir / f"session-{session_id}.{fmt}")
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        payload = {
            "session_id": info.session_id,
            "workspace_key": info.workspace_key,
            "job_id": info.job_id,
            "title": info.title,
            "kind": info.kind,
            "status": info.status,
            "created_at": info.created_at,
            "updated_at": info.updated_at,
            "context_summary": info.context_summary,
            "messages": info.messages,
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        lines = [
            f"# {info.title}", "",
            f"- session: `{info.session_id}`",
            f"- workspace: `{info.workspace_key}`",
            f"- updated: {info.updated_at}", "",
        ]
        if info.context_summary:
            lines.extend(["## Context summary", "", info.context_summary, ""])
        lines.extend(["## Conversation", ""])
        labels = {"user": "User", "assistant": "build-up", "tool": "Tool", "system": "System"}
        for message in info.messages:
            role = str(message.get("role") or "assistant")
            lines.extend([
                f"### {labels.get(role, role.title())}", "",
                str(message.get("content") or ""), "",
            ])
        body = "\n".join(lines).rstrip() + "\n"
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    temp.write_text(body, encoding="utf-8")
    os.replace(temp, target)
    return target


class SessionLease:
    """Best-effort process lock preventing two shells from writing one session."""

    def __init__(self, cfg: BuildupConfig, session_id: str):
        self.cfg = cfg
        self.session_id = session_id
        self._handle: Any = None

    def acquire(self) -> bool:
        lock_dir = self.cfg.buildup_data_dir / "session-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_id = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()[:32]
        handle = (lock_dir / f"{lock_id}.lock").open("a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError, OSError):
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "SessionLease":
        if not self.acquire():
            raise RuntimeError(f"이미 다른 build-up 프로세스가 사용 중인 세션입니다: {self.session_id}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()
