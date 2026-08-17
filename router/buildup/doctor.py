"""Release-readiness checks for the build-up local runtime."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

from buildup.config import BuildupConfig
from buildup.session_store import list_sessions
from buildup.skills import load_skills


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    required: bool = True


@dataclass(frozen=True)
class DoctorReport:
    checks: List[DoctorCheck]

    @property
    def ready(self) -> bool:
        return all(check.status != "FAIL" for check in self.checks if check.required)


def _package_available(*names: str) -> bool:
    return any(importlib.util.find_spec(name) is not None for name in names)


def _gib(num_bytes: float) -> str:
    return f"{num_bytes / (1024 ** 3):.0f}GB"


def _stale_env_vars() -> List[str]:
    """Pre-rename FRIDAY_* variables, which load_config no longer reads."""
    return sorted(name for name in os.environ if name.startswith("FRIDAY_"))


def _total_ram_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return 0


def _job_count(workspace: Path) -> int:
    try:
        return sum(1 for path in workspace.iterdir() if path.is_dir())
    except OSError:
        return 0


def _stranded_workspaces(cfg: BuildupConfig) -> List[Path]:
    """Workspaces holding jobs that the configured base_dir does not point at.

    A renamed product or a moved base_dir silently hides existing jobs, so the
    check looks where earlier versions used to keep them.
    """
    configured = cfg.workspace_dir.expanduser().resolve()
    found: List[Path] = []
    for base in (Path.home() / ".friday", Path.cwd()):
        candidate = (base / "workspace").expanduser()
        if candidate.resolve() == configured or not candidate.is_dir():
            continue
        if _job_count(candidate):
            found.append(candidate)
    return found


def _browser_runtime() -> tuple[bool, str]:
    if not _package_available("playwright"):
        return False, "Playwright package is not installed"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
        if not executable.is_file():
            return False, "Chromium is not installed; run `playwright install chromium`"
        return True, str(executable)
    except Exception as exc:
        return False, f"Playwright runtime failed: {exc}"


def run_doctor(cfg: BuildupConfig, session: Any) -> DoctorReport:
    checks: List[DoctorCheck] = []
    python_ok = sys.version_info >= (3, 10)
    checks.append(DoctorCheck(
        "Python",
        "PASS" if python_ok else "FAIL",
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    ))

    try:
        cfg.buildup_data_dir.mkdir(parents=True, exist_ok=True)
        writable = os.access(cfg.buildup_data_dir, os.W_OK)
        checks.append(DoctorCheck(
            "Data directory",
            "PASS" if writable else "FAIL",
            str(cfg.buildup_data_dir),
        ))
    except OSError as exc:
        checks.append(DoctorCheck("Data directory", "FAIL", str(exc)))

    try:
        cfg.knowledge_vaults_dir.mkdir(parents=True, exist_ok=True)
        knowledge_writable = os.access(cfg.knowledge_vaults_dir, os.W_OK)
        checks.append(DoctorCheck(
            "Knowledge Vault",
            "PASS" if knowledge_writable else "FAIL",
            (
                f"{cfg.knowledge_vaults_dir} · "
                f"ingest={cfg.knowledge_auto_ingest}, "
                f"compile={cfg.knowledge_auto_compile}, lint={cfg.knowledge_auto_lint} · "
                "auto-verify/cross-vault/destructive edit disabled"
                if knowledge_writable
                else "knowledge directory is not writable"
            ),
        ))
    except OSError as exc:
        checks.append(DoctorCheck("Knowledge Vault", "FAIL", str(exc)))

    try:
        list_sessions(cfg, limit=1)
        with closing(sqlite3.connect(str(cfg.session_db_file))) as conn:
            integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        checks.append(DoctorCheck(
            "Session database",
            "PASS" if integrity == "ok" else "FAIL",
            f"{cfg.session_db_file} · {integrity}",
        ))
    except Exception as exc:
        checks.append(DoctorCheck("Session database", "FAIL", str(exc)))

    model_sizes: dict[str, int] = {}
    try:
        response = session.get(cfg.ollama_health_url, timeout=5)
        response.raise_for_status()
        payload = response.json()
        names = {
            str(item.get("name") or item.get("model") or "")
            for item in payload.get("models", [])
            if isinstance(item, dict)
        }
        model_sizes = {
            str(item.get("name") or item.get("model") or ""): int(item.get("size") or 0)
            for item in payload.get("models", [])
            if isinstance(item, dict)
        }
        expected_models = {cfg.research_model, cfg.reviewer_model}
        missing_models = []
        for model in expected_models:
            aliases = {model, model + ":latest"} if ":" not in model else {model}
            if not aliases & names:
                missing_models.append(model)
        model_ok = not missing_models
        detail = (
            "connected · research/reviewer models found"
            if model_ok
            else (
                "missing: " + ", ".join(sorted(missing_models))
                + " · run: " + "; ".join(f"ollama pull {m}" for m in sorted(missing_models))
            )
        )
        checks.append(DoctorCheck("Ollama research models", "PASS" if model_ok else "FAIL", detail))
    except Exception as exc:
        checks.append(DoctorCheck(
            "Ollama + research model",
            "FAIL",
            f"{exc} · is `ollama serve` running at {cfg.ollama_health_url}?",
        ))

    ram_bytes = _total_ram_bytes()
    weight_bytes = sum(
        model_sizes.get(model, 0) for model in {cfg.research_model, cfg.reviewer_model}
    )
    if ram_bytes and weight_bytes:
        # Ollama cannot hold both sets of weights plus a long-context KV cache
        # much past ~70% of physical memory, so it evicts and reloads instead.
        crowded = weight_bytes > ram_bytes * 0.7
        checks.append(DoctorCheck(
            "Model memory",
            "WARN" if crowded else "PASS",
            f"research+reviewer weights {_gib(weight_bytes)} vs {_gib(ram_bytes)} RAM · "
            + (
                "too tight to keep both resident; expect a model reload before the "
                "audit pass, or pick a smaller reviewer model"
                if crowded
                else "both models fit alongside each other"
            ),
            required=False,
        ))

    stale_env = _stale_env_vars()
    checks.append(DoctorCheck(
        "Legacy environment",
        "WARN" if stale_env else "PASS",
        (
            "ignored since the Build-up rename; rename these to BUILDUP_*: "
            + ", ".join(stale_env)
            if stale_env
            else "no stale FRIDAY_* variables"
        ),
        required=False,
    ))

    configured_jobs = _job_count(cfg.workspace_dir)
    stranded = _stranded_workspaces(cfg)
    checks.append(DoctorCheck(
        "Workspace data",
        "WARN" if not configured_jobs and stranded else "PASS",
        (
            f"{cfg.workspace_dir} has no jobs, but jobs exist in "
            + ", ".join(str(path) for path in stranded)
            + " · set BUILDUP_BASE_DIR to that root, or move the data across"
            if not configured_jobs and stranded
            else f"{cfg.workspace_dir} · {configured_jobs} job(s)"
        ),
        required=False,
    ))

    browser_ready, browser_detail = (
        _browser_runtime()
        if cfg.browser_search_enabled
        else (False, "browser search is disabled")
    )
    availability = {
        "tavily": bool(cfg.tavily_api_key),
        "brave": bool(cfg.brave_search_api_key),
        "exa": bool(cfg.exa_api_key),
        "openalex": bool(cfg.openalex_api_key),
        "searxng": bool(cfg.searxng_url),
        "google": bool(cfg.google_cse_api_key and cfg.google_cse_cx),
        "google-cse": bool(cfg.google_cse_api_key and cfg.google_cse_cx),
        "browser": bool(cfg.browser_search_enabled and browser_ready),
    }
    labels = {
        "tavily": "Tavily",
        "brave": "Brave",
        "exa": "Exa",
        "openalex": "OpenAlex",
        "searxng": "SearXNG",
        "google": "Google CSE",
        "google-cse": "Google CSE",
        "browser": "browser",
    }
    selected_provider = cfg.search_provider.lower()
    if selected_provider == "auto":
        usable = [
            labels[name]
            for name in (
                "tavily", "brave", "exa", "openalex", "searxng",
                "google", "browser",
            )
            if availability[name]
        ]
        search_ready = bool(usable)
        detail = (
            ", ".join(usable) + " available"
            if usable
            else (
                "no usable search provider · checked Tavily, Brave, Exa, OpenAlex, "
                "SearXNG, Google CSE · set one, e.g. export TAVILY_API_KEY=..."
            )
        )
        if cfg.browser_search_enabled and not browser_ready:
            detail += f"; browser unavailable: {browser_detail}"
    else:
        search_ready = availability.get(selected_provider, False)
        detail = (
            f"{labels.get(selected_provider, selected_provider)} available"
            if search_ready
            else (
                f"selected provider {selected_provider!r} is not ready · "
                f"set its credentials or use BUILDUP_SEARCH_PROVIDER=auto"
            )
        )
        if selected_provider == "browser" and not browser_ready:
            detail += f": {browser_detail}"
    checks.append(DoctorCheck(
        "Research search",
        "PASS" if search_ready else "FAIL",
        detail,
    ))

    pdf_ok = _package_available("pdfminer")
    checks.append(DoctorCheck(
        "PDF support",
        "PASS" if pdf_ok else "FAIL",
        "pdfminer available" if pdf_ok else "install pdfminer.six for paper workflows",
    ))

    skill_names = {skill.name for skill in load_skills(cfg, refresh=True)}
    required_skills = {"deep-research", "paper-pdf-translation"}
    missing_skills = sorted(required_skills - skill_names)
    checks.append(DoctorCheck(
        "build-up skills",
        "PASS" if not missing_skills else "FAIL",
        "2 core skills found"
        if not missing_skills
        else "missing: " + ", ".join(missing_skills) + " · run: buildup skills reindex",
    ))
    return DoctorReport(checks)


def format_doctor(report: DoctorReport) -> str:
    icons = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}
    lines = [
        f"{icons.get(check.status, '-')} {check.status:<4}  {check.name}: {check.detail}"
        for check in report.checks
    ]
    lines.extend(["", "READY" if report.ready else "NOT READY"])
    return "\n".join(lines)
