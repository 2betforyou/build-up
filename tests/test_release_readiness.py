from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from friday import COMMAND_NAME, PRODUCT_NAME
from friday.config import FridayConfig, load_config
from friday.doctor import run_doctor
from friday.search import _page_excerpt, is_public_web_url, web_search
from friday.skills import load_skills


class _OllamaResponse:
    def __init__(self, model: str):
        self.model = model

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"models": [{"name": self.model}]}


class _OllamaSession:
    def __init__(self, model: str):
        self.model = model

    def get(self, url, timeout):
        return _OllamaResponse(self.model)


class ReleaseReadinessTests(unittest.TestCase):
    def test_public_brand_and_launcher_are_build_up(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual("build-up", PRODUCT_NAME)
        self.assertEqual("buildup", COMMAND_NAME)
        self.assertTrue((project_root / "buildup").is_file())
        self.assertTrue((project_root / "router" / "run_buildup.py").is_file())
        self.assertIn(
            'buildup = "friday:main"',
            (project_root / "pyproject.toml").read_text(encoding="utf-8"),
        )

    def test_night_environment_names_override_legacy_names(self) -> None:
        with patch.dict(os.environ, {
            "NIGHT_BASE_DIR": "/tmp/night-new",
            "FRIDAY_BASE_DIR": "/tmp/night-old",
            "NIGHT_RESEARCH_MODEL": "new-model",
            "FRIDAY_NIGHT_MODEL": "old-model",
        }, clear=True):
            cfg = load_config()
        self.assertEqual(Path("/tmp/night-new"), cfg.base_dir)
        self.assertEqual("new-model", cfg.night_model)

    def test_keyless_search_is_available_and_private_urls_are_rejected(self) -> None:
        result = [{"title": "Public", "href": "https://example.com/research", "body": "evidence"}]
        with patch("friday.search._duckduckgo_search", return_value=result):
            rows, engine = web_search(
                "agent harness",
                FridayConfig(search_provider="duckduckgo"),
                object(),
                max_results=3,
                bilingual=False,
            )
        self.assertEqual("DuckDuckGo", engine)
        self.assertEqual(result, rows)
        self.assertFalse(is_public_web_url("http://127.0.0.1/private"))
        self.assertFalse(is_public_web_url("http://localhost/private"))
        self.assertTrue(is_public_web_url("https://example.com/research"))

    def test_page_hydration_rejects_redirects_to_private_hosts(self) -> None:
        class RedirectResponse:
            status_code = 302
            headers = {"location": "http://127.0.0.1/private"}

        class RedirectSession:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append(url)
                return RedirectResponse()

        session = RedirectSession()
        address = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with patch("friday.search.socket.getaddrinfo", return_value=address):
            excerpt = _page_excerpt("https://example.com/start", session)
        self.assertEqual("", excerpt)
        self.assertEqual(["https://example.com/start"], session.calls)

    def test_doctor_reports_ready_for_a_complete_local_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = FridayConfig(base_dir=Path(temp_dir), night_model="research-model")
            for name in ("deep-research", "personal-study-coach", "paper-pdf-translation"):
                directory = cfg.skills_dir / name
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test skill for {name}\n---\n",
                    encoding="utf-8",
                )
            with patch("friday.doctor._package_available", return_value=True):
                report = run_doctor(cfg, _OllamaSession(cfg.night_model))

            self.assertTrue(report.ready)
            self.assertTrue(all(check.status != "FAIL" for check in report.checks))

    def test_packaged_core_skills_exist_and_local_skill_can_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = FridayConfig(base_dir=Path(temp_dir))
            builtins = load_skills(cfg, refresh=True)
            names = {skill.name for skill in builtins}
            self.assertTrue({"deep-research", "personal-study-coach", "paper-pdf-translation"} <= names)

            local = cfg.skills_dir / "deep-research"
            local.mkdir(parents=True)
            (local / "SKILL.md").write_text(
                "---\nname: deep-research\ndescription: local override\n---\n",
                encoding="utf-8",
            )
            skills = load_skills(cfg, refresh=True)
            selected = [skill for skill in skills if skill.name == "deep-research"]
            self.assertEqual(1, len(selected))
            self.assertEqual("local", selected[0].source)


if __name__ == "__main__":
    unittest.main()
