"""Release-readiness checks for the build-up local runtime."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from typing import Any, List

from friday.config import FridayConfig
from friday.session_store import list_sessions
from friday.skills import load_skills


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


def run_doctor(cfg: FridayConfig, session: Any) -> DoctorReport:
    checks: List[DoctorCheck] = []
    python_ok = sys.version_info >= (3, 10)
    checks.append(DoctorCheck(
        "Python",
        "PASS" if python_ok else "FAIL",
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    ))

    try:
        cfg.night_data_dir.mkdir(parents=True, exist_ok=True)
        writable = os.access(cfg.night_data_dir, os.W_OK)
        checks.append(DoctorCheck(
            "Data directory",
            "PASS" if writable else "FAIL",
            str(cfg.night_data_dir),
        ))
    except OSError as exc:
        checks.append(DoctorCheck("Data directory", "FAIL", str(exc)))

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
        expected_names = {cfg.night_model}
        if ":" not in cfg.night_model:
            expected_names.add(cfg.night_model + ":latest")
        model_ok = bool(expected_names & names)
        detail = f"connected · {cfg.night_model} {'found' if model_ok else 'missing'}"
        checks.append(DoctorCheck("Ollama + research model", "PASS" if model_ok else "FAIL", detail))
    except Exception as exc:
        checks.append(DoctorCheck("Ollama + research model", "FAIL", str(exc)))

    keyless_search = _package_available("ddgs", "duckduckgo_search")
    configured_search = bool(
        cfg.tavily_api_key or (cfg.google_cse_api_key and cfg.google_cse_cx)
    )
    checks.append(DoctorCheck(
        "Research search",
        "PASS" if configured_search or keyless_search else "FAIL",
        (
            "API-backed provider configured"
            if configured_search else
            "DuckDuckGo keyless fallback available"
            if keyless_search else
            "install ddgs or configure Tavily/Google"
        ),
    ))

    pdf_ok = _package_available("pdfminer")
    checks.append(DoctorCheck(
        "PDF support",
        "PASS" if pdf_ok else "WARN",
        "pdfminer available" if pdf_ok else "install pdfminer.six for paper workflows",
        required=False,
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
