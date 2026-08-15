"""Immutable runtime configuration for Build-up."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class BuildupConfig:
    """Runtime configuration. Environment variables override these defaults."""

    base_dir: Path = Path.home() / ".buildup"

    # Ollama
    ollama_url: str = "http://localhost:11434/api/chat"
    ollama_embed_url: str = "http://localhost:11434/api/embed"
    fast_model: str = "gemma4:e4b"
    main_model: str = "qwen3.8:27b"
    research_model: str = "qwen3.8:27b"
    reviewer_model: str = "deepseek-r1:32b"
    # Korean-only answers by default; opt in to the English companion section.
    english_brief: bool = False
    coder_model: str = "qwen3.8:27b"
    embed_model: str = "nomic-embed-text"
    struct_model: str = "qwen2.5:1.5b"
    ollama_timeout: int = 600
    ollama_connect_timeout: int = 10
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

    # Retry and logging
    http_max_retries: int = 3
    http_backoff_factor: float = 1.0
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3

    # Conversation
    max_history_turns: int = 20

    # Search discovery and reading
    web_search_max_results: int = 5
    search_provider: str = "auto"  # auto | tavily | brave | exa | openalex | searxng | browser | google
    search_timeout: int = 30
    search_cache_ttl_hours: int = 24
    respect_robots_txt: bool = True
    web_search_lang: str = "auto"

    tavily_api_key: str = ""
    tavily_search_depth: str = "basic"  # basic | advanced | fast | ultra-fast
    brave_search_api_key: str = ""
    exa_api_key: str = ""
    openalex_api_key: str = ""
    searxng_url: str = ""
    google_cse_api_key: str = ""
    google_cse_cx: str = ""

    # Browser discovery is opt-in. CAPTCHA/consent pages fail closed; Build-up
    # never implements stealth, proxy rotation, or challenge bypasses.
    browser_search_enabled: bool = False
    browser_search_engine: str = "google"
    browser_headless: bool = True

    # Adaptive research budgets. These are hard caps persisted with every run.
    research_default_depth: str = "auto"  # auto | shallow | standard | deep
    research_max_rounds: int = 3
    research_max_tasks: int = 8
    research_max_searches: int = 24
    research_max_sources: int = 30
    research_max_model_calls: int = 20
    research_max_concurrency: int = 4
    research_max_runtime_seconds: int = 1800
    research_min_coverage: float = 0.85
    research_source_chars: int = 100_000
    research_evidence_passage_chars: int = 4_000

    # Compounding knowledge vault. Ingestion/compilation/linting are
    # deterministic and non-destructive; verification and cross-vault binding
    # always remain explicit user actions.
    knowledge_auto_ingest: bool = True
    knowledge_auto_compile: bool = True
    knowledge_auto_lint: bool = True

    # Heavy-routing keywords
    heavy_keywords: Tuple[str, ...] = (
        "비교", "분석", "설계", "근거", "계획",
        "rebuttal", "reviewer", "실험", "로그",
        "architecture", "design", "compare", "analyze",
    )

    def __post_init__(self) -> None:
        providers = {
            "auto", "tavily", "brave", "exa", "openalex", "searxng",
            "browser", "google", "google-cse",
        }
        if self.search_provider.lower() not in providers:
            raise ValueError(
                f"BUILDUP_SEARCH_PROVIDER must be one of {sorted(providers)}; "
                f"got {self.search_provider!r}"
            )
        if self.research_default_depth.lower() not in {"auto", "shallow", "standard", "deep"}:
            raise ValueError(f"invalid BUILDUP_RESEARCH_DEPTH: {self.research_default_depth!r}")
        if self.browser_search_engine.lower() not in {"google", "brave", "bing"}:
            raise ValueError(
                f"invalid BUILDUP_BROWSER_SEARCH_ENGINE: {self.browser_search_engine!r}"
            )
        if self.tavily_search_depth.lower() not in {
            "basic", "advanced", "fast", "ultra-fast",
        }:
            raise ValueError(
                f"invalid BUILDUP_TAVILY_SEARCH_DEPTH: {self.tavily_search_depth!r}"
            )
        if self.web_search_lang.lower() not in {"auto", "ko", "en"}:
            raise ValueError(f"invalid BUILDUP_SEARCH_LANG: {self.web_search_lang!r}")
        positive = {
            "search_timeout": self.search_timeout,
            "research_max_rounds": self.research_max_rounds,
            "research_max_tasks": self.research_max_tasks,
            "research_max_searches": self.research_max_searches,
            "research_max_sources": self.research_max_sources,
            "research_max_model_calls": self.research_max_model_calls,
            "research_max_concurrency": self.research_max_concurrency,
            "research_max_runtime_seconds": self.research_max_runtime_seconds,
            "research_source_chars": self.research_source_chars,
            "research_evidence_passage_chars": self.research_evidence_passage_chars,
        }
        invalid = [name for name, value in positive.items() if value < 1]
        if invalid:
            raise ValueError("configuration values must be positive: " + ", ".join(invalid))
        if self.search_cache_ttl_hours < 0:
            raise ValueError("BUILDUP_SEARCH_CACHE_TTL_HOURS must be zero or positive")
        if not 0.0 <= self.research_min_coverage <= 1.0:
            raise ValueError("BUILDUP_RESEARCH_MIN_COVERAGE must be between 0 and 1")

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
        return self.base_dir / ".buildup_history"

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
    def buildup_data_dir(self) -> Path:
        return self.base_dir / "data" / "buildup"

    @property
    def session_db_file(self) -> Path:
        return self.buildup_data_dir / "state.db"

    @property
    def trace_file(self) -> Path:
        return self.buildup_data_dir / "traces.jsonl"

    @property
    def search_cache_dir(self) -> Path:
        return self.buildup_data_dir / "search-cache"

    @property
    def study_dir(self) -> Path:
        return self.base_dir / "study"

    @property
    def knowledge_dir(self) -> Path:
        return self.base_dir / "knowledge"

    @property
    def knowledge_vaults_dir(self) -> Path:
        return self.knowledge_dir / "vaults"

    @property
    def knowledge_bindings_file(self) -> Path:
        return self.knowledge_dir / "bindings.jsonl"

    @property
    def all_dirs(self) -> List[Path]:
        return [
            self.base_dir, self.inbox_dir, self.workspace_dir,
            self.export_dir, self.trash_dir, self.logs_dir,
            self.templates_dir, self.calendar_dir, self.library_dir,
            self.paper_library_dir, self.paper_index_dir, self.skills_dir,
            self.vendor_skills_dir, self.buildup_data_dir,
            self.search_cache_dir, self.study_dir, self.knowledge_dir,
            self.knowledge_vaults_dir,
        ]

    @property
    def openwebui_kb_map(self) -> Dict[str, str]:
        return {
            "research": self.openwebui_kb_research,
            "coding": self.openwebui_kb_coding,
            "ops": self.openwebui_kb_ops,
        }


# Deterministic model-routing weights.
ROUTING_WEIGHTS: Dict[str, int] = {
    "intent:qa": -2,
    "intent:search": -2,
    "intent:summarize": 0,
    "intent:calendar": -1,
    "intent:file_manage": -1,
    "intent:inspect_logs": 1,
    "intent:inspect_code": 1,
    "intent:document_write": 2,
    "intent:code_edit": 3,
    "intent:debug": 3,
    "intent:experiment_run": 2,
    "intent:workflow_multi_step": 4,
    "mode:consult": -2,
    "mode:inspect": -1,
    "mode:draft": 0,
    "mode:execute": 1,
    "mode:workflow": 3,
    "confidence:high": -1,
    "confidence:low": 2,
    "_coder_threshold": 3,
    "_main_threshold": 3,
}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite; got {raw!r}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name, "true" if default else "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean; got {value!r}")


def load_config() -> BuildupConfig:
    """Load Build-up settings plus provider-native secret variables."""
    return BuildupConfig(
        base_dir=Path(_env("BUILDUP_BASE_DIR", str(Path.home() / ".buildup"))).expanduser(),
        ollama_url=_env("BUILDUP_OLLAMA_URL", "http://localhost:11434/api/chat"),
        ollama_embed_url=_env("BUILDUP_OLLAMA_EMBED_URL", "http://localhost:11434/api/embed"),
        fast_model=_env("BUILDUP_FAST_MODEL", "gemma4:e4b"),
        main_model=_env("BUILDUP_MAIN_MODEL", "qwen3.8:27b"),
        research_model=_env("BUILDUP_RESEARCH_MODEL", "qwen3.8:27b"),
        reviewer_model=_env("BUILDUP_REVIEWER_MODEL", "deepseek-r1:32b"),
        english_brief=_env_bool("BUILDUP_ENGLISH_BRIEF", False),
        coder_model=_env("BUILDUP_CODER_MODEL", "qwen3.8:27b"),
        embed_model=_env("BUILDUP_EMBED_MODEL", "nomic-embed-text"),
        struct_model=_env("BUILDUP_STRUCT_MODEL", "qwen2.5:1.5b"),
        max_pdf_bytes=_env_int("BUILDUP_MAX_PDF_BYTES", 50 * 1024 * 1024),
        ollama_health_url=_env("BUILDUP_OLLAMA_HEALTH_URL", "http://localhost:11434/api/tags"),
        openwebui_base_url=_env("OPENWEBUI_BASE_URL", "http://localhost:3000"),
        openwebui_token=_env("OPENWEBUI_TOKEN", ""),
        openwebui_kb_research=_env("OPENWEBUI_KB_RESEARCH", ""),
        openwebui_kb_coding=_env("OPENWEBUI_KB_CODING", ""),
        openwebui_kb_ops=_env("OPENWEBUI_KB_OPS", ""),
        search_provider=_env("BUILDUP_SEARCH_PROVIDER", "auto"),
        search_timeout=_env_int("BUILDUP_SEARCH_TIMEOUT", 30),
        search_cache_ttl_hours=_env_int("BUILDUP_SEARCH_CACHE_TTL_HOURS", 24),
        respect_robots_txt=_env_bool("BUILDUP_RESPECT_ROBOTS_TXT", True),
        web_search_lang=_env("BUILDUP_SEARCH_LANG", "auto"),
        tavily_api_key=_env("TAVILY_API_KEY", ""),
        tavily_search_depth=_env("BUILDUP_TAVILY_SEARCH_DEPTH", "basic"),
        brave_search_api_key=_env("BRAVE_SEARCH_API_KEY", ""),
        exa_api_key=_env("EXA_API_KEY", ""),
        openalex_api_key=_env("OPENALEX_API_KEY", ""),
        searxng_url=_env("BUILDUP_SEARXNG_URL", ""),
        google_cse_api_key=_env("GOOGLE_CSE_API_KEY", ""),
        google_cse_cx=_env("GOOGLE_CSE_CX", ""),
        browser_search_enabled=_env_bool("BUILDUP_BROWSER_SEARCH_ENABLED", False),
        browser_search_engine=_env("BUILDUP_BROWSER_SEARCH_ENGINE", "google"),
        browser_headless=_env_bool("BUILDUP_BROWSER_HEADLESS", True),
        research_default_depth=_env("BUILDUP_RESEARCH_DEPTH", "auto"),
        research_max_rounds=_env_int("BUILDUP_RESEARCH_MAX_ROUNDS", 3),
        research_max_tasks=_env_int("BUILDUP_RESEARCH_MAX_TASKS", 8),
        research_max_searches=_env_int("BUILDUP_RESEARCH_MAX_SEARCHES", 24),
        research_max_sources=_env_int("BUILDUP_RESEARCH_MAX_SOURCES", 30),
        research_max_model_calls=_env_int("BUILDUP_RESEARCH_MAX_MODEL_CALLS", 20),
        research_max_concurrency=_env_int("BUILDUP_RESEARCH_MAX_CONCURRENCY", 4),
        research_max_runtime_seconds=_env_int("BUILDUP_RESEARCH_MAX_RUNTIME_SECONDS", 1800),
        research_min_coverage=_env_float("BUILDUP_RESEARCH_MIN_COVERAGE", 0.85),
        research_source_chars=_env_int("BUILDUP_RESEARCH_SOURCE_CHARS", 100_000),
        research_evidence_passage_chars=_env_int(
            "BUILDUP_RESEARCH_EVIDENCE_PASSAGE_CHARS", 4_000
        ),
        knowledge_auto_ingest=_env_bool("BUILDUP_KNOWLEDGE_AUTO_INGEST", True),
        knowledge_auto_compile=_env_bool("BUILDUP_KNOWLEDGE_AUTO_COMPILE", True),
        knowledge_auto_lint=_env_bool("BUILDUP_KNOWLEDGE_AUTO_LINT", True),
    )
