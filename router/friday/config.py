"""Immutable runtime configuration for Friday."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class FridayConfig:
    """Immutable runtime configuration.  Priority: env var > defaults."""

    base_dir: Path = Path.home() / "FridayLocal"

    # Ollama
    ollama_url: str = "http://localhost:11434/api/chat"
    ollama_embed_url: str = "http://localhost:11434/api/embed"
    fast_model: str = "gemma4:e4b"
    main_model: str = "gemma4:31b"
    night_model: str = "qwen3.6:27b"
    reviewer_model: str = "deepseek-r1:32b"
    coder_model: str = "qwen3-coder-30b-instruct"
    embed_model: str = "nomic-embed-text"
    # Lightweight model for intent structuring (5.2 layer).
    # Replace with a fine-tuned intent classifier as traces accumulate.
    struct_model: str = "qwen2.5:1.5b"
    ollama_timeout: int = 600
    ollama_connect_timeout: int = 10  # TCP connect timeout (separate from read)
    ollama_health_url: str = "http://localhost:11434/api/tags"

    # Open WebUI
    openwebui_base_url: str = "http://localhost:3000"
    openwebui_token: str = ""
    openwebui_kb_research: str = ""
    openwebui_kb_coding: str = ""
    openwebui_kb_ops: str = ""
    openwebui_timeout: int = 600
    openwebui_process_timeout: int = 300
    openwebui_poll_interval: float = 2.0

    # File handling
    text_extensions: frozenset = frozenset({
        ".txt", ".md", ".py", ".json", ".yaml", ".yml",
        ".csv", ".log", ".sh", ".tex", ".toml", ".cfg",
        ".ini", ".rst", ".xml", ".html", ".css", ".js",
        ".ts", ".r", ".jl", ".lua",
    })
    max_text_bytes: int = 500_000
    max_pdf_bytes: int = 50 * 1024 * 1024
    min_free_disk_mb: int = 100

    # Retry
    http_max_retries: int = 3
    http_backoff_factor: float = 1.0


    # Logging
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3

    # Conversation
    max_history_turns: int = 20

    # Web search
    web_search_max_results: int = 5

    search_provider: str = "auto"   # auto | tavily | google | duckduckgo

    tavily_api_key: str = ""
    tavily_search_depth: str = "basic"   # basic | advanced

    google_cse_api_key: str = ""
    google_cse_cx: str = ""

    web_search_lang: str = "auto"  # en, ko, or auto 

    # Heavy-routing keywords
    heavy_keywords: Tuple[str, ...] = (
        "비교", "분석", "설계", "근거", "계획",
        "rebuttal", "reviewer", "실험", "로그",
        "architecture", "design", "compare", "analyze",
    )

    # Derived paths ----------------------------------------------------------
    @property
    def inbox_dir(self) -> Path:
        return self.base_dir / "inbox"

    @property
    def workspace_dir(self) -> Path:
        return self.base_dir / "workspace"

    @property
    def export_dir(self) -> Path:
        return self.base_dir / "export"

    @property
    def trash_dir(self) -> Path:
        return self.base_dir / "trash"

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def state_file(self) -> Path:
        return self.base_dir / "state.json"

    @property
    def history_file(self) -> Path:
        return self.base_dir / ".friday_history"

    @property
    def templates_dir(self) -> Path:
        return self.base_dir / "templates"

    @property
    def calendar_dir(self) -> Path:
        return self.base_dir / "calendar"

    @property
    def library_dir(self) -> Path:
        return self.base_dir / "library"

    @property
    def paper_library_dir(self) -> Path:
        return self.library_dir / "papers"

    @property
    def paper_index_dir(self) -> Path:
        return self.library_dir / "index"

    @property
    def skills_dir(self) -> Path:
        return self.base_dir / "skills"

    @property
    def vendor_skills_dir(self) -> Path:
        return self.base_dir / "vendor" / "skills"

    @property
    def friday_data_dir(self) -> Path:
        return self.base_dir / "data" / "friday"

    @property
    def night_data_dir(self) -> Path:
        """Runtime-owned data introduced after the Friday → night rename."""
        return self.base_dir / "data" / "night"

    @property
    def session_db_file(self) -> Path:
        return self.night_data_dir / "state.db"

    @property
    def study_dir(self) -> Path:
        return self.base_dir / "study"

    @property
    def all_dirs(self) -> List[Path]:
        return [
            self.base_dir, self.inbox_dir, self.workspace_dir,
            self.export_dir, self.trash_dir, self.logs_dir,
            self.templates_dir, self.calendar_dir, self.library_dir,
            self.paper_library_dir, self.paper_index_dir, self.skills_dir,
            self.vendor_skills_dir, self.friday_data_dir, self.night_data_dir,
            self.study_dir,
        ]

    @property
    def openwebui_kb_map(self) -> Dict[str, str]:
        return {
            "research": self.openwebui_kb_research,
            "coding": self.openwebui_kb_coding,
            "ops": self.openwebui_kb_ops,
        }


# ── Model routing weights ─────────────────────────────────────────────────────
# Deterministic scoring router — edit weights here as trace data accumulates.
# Positive → push toward main; negative → push toward fast.
# Thresholds: total >= _main_threshold → main; code intent + >= _coder_threshold → coder.
ROUTING_WEIGHTS: Dict[str, int] = {
    # intent scores
    "intent:qa":                   -2,
    "intent:search":               -2,
    "intent:summarize":             0,
    "intent:calendar":             -1,
    "intent:file_manage":          -1,
    "intent:inspect_logs":          1,
    "intent:inspect_code":          1,
    "intent:document_write":        2,
    "intent:code_edit":             3,
    "intent:debug":                 3,
    "intent:experiment_run":        2,
    "intent:workflow_multi_step":   4,
    # interaction_mode scores
    "mode:consult":                -2,
    "mode:inspect":                -1,
    "mode:draft":                   0,
    "mode:execute":                 1,
    "mode:workflow":                3,
    # confidence bonus/penalty
    "confidence:high":             -1,   # high confidence → simpler → fast
    "confidence:low":               2,   # low confidence → conservative → main
    # thresholds (not scores — prefixed with _ to distinguish)
    "_coder_threshold":             3,   # code intent + score >= this → coder
    "_main_threshold":              3,   # total score >= this → main
}


def _env(name: str, legacy_name: str, default: str) -> str:
    """Read the newer NIGHT name first while preserving FRIDAY compatibility."""
    return os.environ.get(name, os.environ.get(legacy_name, default))


def load_config() -> FridayConfig:
    """Load configuration from compatibility NIGHT env vars, then FRIDAY aliases."""
    base = Path(_env("NIGHT_BASE_DIR", "FRIDAY_BASE_DIR", str(Path.home() / "FridayLocal")))
    return FridayConfig(
        base_dir=base,
        ollama_url=_env("NIGHT_OLLAMA_URL", "FRIDAY_OLLAMA_URL", "http://localhost:11434/api/chat"),
        ollama_embed_url=_env("NIGHT_OLLAMA_EMBED_URL", "FRIDAY_OLLAMA_EMBED_URL", "http://localhost:11434/api/embed"),
        fast_model=_env("NIGHT_FAST_MODEL", "FRIDAY_FAST_MODEL", "gemma4:e4b"),
        main_model=_env("NIGHT_MAIN_MODEL", "FRIDAY_MAIN_MODEL", "gemma4:31b"),
        night_model=_env("NIGHT_RESEARCH_MODEL", "FRIDAY_NIGHT_MODEL", "qwen3.6:27b"),
        reviewer_model=_env("NIGHT_REVIEWER_MODEL", "FRIDAY_REVIEWER_MODEL", "deepseek-r1:32b"),
        coder_model=_env("NIGHT_CODER_MODEL", "FRIDAY_CODER_MODEL", "qwen3-coder-30b-instruct"),
        embed_model=_env("NIGHT_EMBED_MODEL", "FRIDAY_EMBED_MODEL", "nomic-embed-text"),
        struct_model=_env("NIGHT_STRUCT_MODEL", "FRIDAY_STRUCT_MODEL", "qwen2.5:1.5b"),
        max_pdf_bytes=int(_env("NIGHT_MAX_PDF_BYTES", "FRIDAY_MAX_PDF_BYTES", str(50 * 1024 * 1024))),
        ollama_health_url=_env(
            "NIGHT_OLLAMA_HEALTH_URL", "FRIDAY_OLLAMA_HEALTH_URL", "http://localhost:11434/api/tags"
        ),
        openwebui_base_url=os.environ.get("OPENWEBUI_BASE_URL", "http://localhost:3000"),
        openwebui_token=os.environ.get("OPENWEBUI_TOKEN", ""),
        openwebui_kb_research=os.environ.get("OPENWEBUI_KB_RESEARCH", ""),
        openwebui_kb_coding=os.environ.get("OPENWEBUI_KB_CODING", ""),
        openwebui_kb_ops=os.environ.get("OPENWEBUI_KB_OPS", ""),
        
        search_provider=_env("NIGHT_SEARCH_PROVIDER", "FRIDAY_SEARCH_PROVIDER", "auto"),
        tavily_api_key=os.environ.get("TAVILY_API_KEY", ""),
        tavily_search_depth=_env("NIGHT_TAVILY_SEARCH_DEPTH", "FRIDAY_TAVILY_SEARCH_DEPTH", "basic"),

        google_cse_api_key=os.environ.get("GOOGLE_CSE_API_KEY", ""),
        google_cse_cx=os.environ.get("GOOGLE_CSE_CX", ""),
        web_search_lang=_env("NIGHT_SEARCH_LANG", "FRIDAY_SEARCH_LANG", "en"),
    )
