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

    try:
        response = session.get(cfg.ollama_health_url, timeout=5)
        response.raise_for_status()
        payload = response.json()
        names = {
            str(item.get("name") or item.get("model") or "")
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
            else "missing: " + ", ".join(sorted(missing_models))
        )
        checks.append(DoctorCheck("Ollama research models", "PASS" if model_ok else "FAIL", detail))
    except Exception as exc:
        checks.append(DoctorCheck("Ollama + research model", "FAIL", str(exc)))

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
        detail = ", ".join(usable) + " available" if usable else "no usable search provider"
        if cfg.browser_search_enabled and not browser_ready:
            detail += f"; browser unavailable: {browser_detail}"
    else:
        search_ready = availability.get(selected_provider, False)
        detail = (
            f"{labels.get(selected_provider, selected_provider)} available"
            if search_ready
            else f"selected provider {selected_provider!r} is not ready"
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
    required_skills = {"deep-research", "personal-study-coach", "paper-pdf-translation"}
    missing_skills = sorted(required_skills - skill_names)
    checks.append(DoctorCheck(
        "build-up skills",
        "PASS" if not missing_skills else "FAIL",
        "3 core skills found" if not missing_skills else "missing: " + ", ".join(missing_skills),
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
