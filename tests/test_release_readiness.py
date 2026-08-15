from __future__ import annotations

import io
import hashlib
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests

from buildup import COMMAND_NAME, PRODUCT_NAME
from buildup.cli import main as cli_main
from buildup.command_runner import UnsafeCommandError, parse_command, run_command
from buildup.config import BuildupConfig, load_config
from buildup.deep_research import run_deep_research
from buildup.doctor import run_doctor
from buildup.git_mgr import RequiresConfirmation, UnsafeGitCommand, git_run
from buildup.paper_source import _download_pdf
from buildup.research.engine import create_state
from buildup.research.providers import (
    NoSearchProviderConfigured,
    SearchBroker,
    SearchProviderError,
)
from buildup.search import _page_excerpt, is_public_web_url
from buildup.research.reader import SafeWebReader
from buildup.skills import load_skills
from buildup.tool_registry import ToolContext, execute_tool


class _OllamaResponse:
    def __init__(self, models):
        self.models = models

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"models": [{"name": model} for model in self.models]}


class _OllamaSession:
    def __init__(self, *models):
        self.models = models

    def get(self, url, timeout):
        return _OllamaResponse(self.models)


class ReleaseReadinessTests(unittest.TestCase):
    def test_power_user_commands_never_invoke_shell_syntax(self) -> None:
        blocked = (
            "echo safe; python3 -c 'print(1)'",
            "echo safe && date",
            "echo $(id)",
            "echo safe > result.txt",
            "/tmp/ls -la",
        )
        for command in blocked:
            with self.subTest(command=command), self.assertRaises(UnsafeCommandError):
                parse_command(command)

        with tempfile.TemporaryDirectory() as temp_dir:
            completed = run_command("echo build-up-safe", Path(temp_dir))
            limited = run_command(
                "python3 -c \"print('x' * 2000)\"",
                Path(temp_dir),
                max_output_bytes=64,
            )
        self.assertEqual(0, completed.returncode)
        self.assertEqual("build-up-safe", completed.output)
        self.assertTrue(limited.output_limit_reached)
        self.assertIn("output limit reached", limited.output)

    def test_agent_process_tools_require_explicit_capability_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(base_dir=Path(temp_dir))
            context = ToolContext(
                cfg=cfg,
                session=requests.Session(),
                logger=logging.getLogger("test"),
                mode="fast",
                last_file=None,
            )
            blocked, _ = execute_tool(
                "run_shell", {"command": "echo should-not-run"}, context,
            )
            approved, _ = execute_tool(
                "run_shell",
                {"command": "echo approved"},
                ToolContext(
                    cfg=cfg,
                    session=context.session,
                    logger=context.logger,
                    mode="fast",
                    last_file=None,
                    approved_capabilities=frozenset({"confirm-shell"}),
                ),
            )
        self.assertTrue(blocked.startswith("[차단]"))
        self.assertIn("명시적 승인", blocked)
        self.assertIn("[exit 0]\napproved", approved)

    def test_git_runner_blocks_global_options_aliases_and_external_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            _, init_code = git_run("init", repo)
            self.assertEqual(0, init_code)
            _, status_code = git_run("status", repo)
            self.assertEqual(0, status_code)

            unsafe = (
                "-c alias.pwn=!id pwn",
                "difftool",
                "bisect run id",
                "remote add origin ext::sh -c id",
                "status; id",
            )
            for args in unsafe:
                with self.subTest(args=args), self.assertRaises(UnsafeGitCommand):
                    git_run(args, repo)
            with self.assertRaises(RequiresConfirmation):
                git_run("reset --hard", repo)

    def test_help_and_version_do_not_create_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for flag in ("--help", "--version"):
                base_dir = Path(temp_dir) / flag.removeprefix("-")
                with (
                    self.subTest(flag=flag),
                    patch.object(sys, "argv", ["buildup", flag]),
                    patch.dict(os.environ, {"BUILDUP_BASE_DIR": str(base_dir)}, clear=True),
                    redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as exit_context,
                ):
                    cli_main()
                self.assertEqual(0, exit_context.exception.code)
                self.assertFalse(base_dir.exists())

    def test_public_brand_package_and_launcher_are_build_up_only(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        old_name = "fri" + "day"
        self.assertEqual("build-up", PRODUCT_NAME)
        self.assertEqual("buildup", COMMAND_NAME)
        self.assertTrue((project_root / "buildup").is_file())
        self.assertTrue((project_root / "router" / "run_buildup.py").is_file())
        self.assertTrue((project_root / "router" / "buildup").is_dir())
        self.assertFalse((project_root / "router" / old_name).exists())
        self.assertFalse((project_root / old_name).exists())
        self.assertIn(
            'buildup = "buildup:main"',
            (project_root / "pyproject.toml").read_text(encoding="utf-8"),
        )
        requirements = (project_root / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("dd" + "gs", requirements)

        other_old_name = "ni" + "ght"
        banned_markers = (
            old_name.upper() + "_",
            other_old_name.upper() + "_",
            "." + old_name,
            "run_" + old_name,
            "run_" + other_old_name,
            "router/" + old_name,
            "router/" + other_old_name,
            old_name.title() + "Config",
            other_old_name + "_model",
            "Build" + "upLocal",
        )
        public_roots = [
            project_root / "router" / "buildup",
            project_root / "skills",
            project_root / ".github",
        ]
        public_files = [
            project_root / "README.md",
            project_root / "CHANGELOG.md",
            project_root / "CONTRIBUTING.md",
            project_root / "SECURITY.md",
            project_root / "pyproject.toml",
            project_root / "MANIFEST.in",
            project_root / "requirements.txt",
            project_root / "buildup",
            project_root / "router" / "run_buildup.py",
        ]
        for root in public_roots:
            public_files.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix.lower() in {".py", ".md", ".yaml", ".yml", ".toml", ".txt"}
            )
        for path in public_files:
            with self.subTest(path=path.relative_to(project_root)):
                text = path.read_text(encoding="utf-8")
                self.assertFalse(
                    any(marker in text for marker in banned_markers),
                    f"legacy product marker remains in {path}",
                )

    def test_configuration_uses_build_up_environment_surface(self) -> None:
        with patch.dict(os.environ, {
            "BUILDUP_BASE_DIR": "/tmp/buildup-config",
            "BUILDUP_RESEARCH_MODEL": "new-model",
            "BUILDUP_REVIEWER_MODEL": "reviewer",
            "BUILDUP_SEARCH_PROVIDER": "brave",
            "BRAVE_SEARCH_API_KEY": "key",
        }, clear=True):
            cfg = load_config()
        self.assertEqual(Path("/tmp/buildup-config"), cfg.base_dir)
        self.assertEqual("new-model", cfg.research_model)
        self.assertEqual("reviewer", cfg.reviewer_model)
        self.assertEqual("brave", cfg.search_provider)
        self.assertEqual("key", cfg.brave_search_api_key)

    def test_invalid_configuration_and_budget_overrides_fail_loudly(self) -> None:
        invalid_values = (
            ("BUILDUP_RESEARCH_MAX_ROUNDS", "many", "must be an integer"),
            ("BUILDUP_RESEARCH_MIN_COVERAGE", "nan", "must be finite"),
            ("BUILDUP_RESPECT_ROBOTS_TXT", "perhaps", "must be a boolean"),
        )
        for name, value, message in invalid_values:
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {name: value}, clear=True),
                self.assertRaisesRegex(ValueError, message),
            ):
                load_config()

        cfg = BuildupConfig(research_max_rounds=3, research_max_searches=10)
        with self.assertRaisesRegex(ValueError, "max_rounds=2"):
            create_state("q", "run", "job", cfg, depth="shallow", max_rounds=2)
        with self.assertRaisesRegex(ValueError, "max_searches must be positive"):
            create_state("q", "run", "job", cfg, max_searches=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_cfg = BuildupConfig(base_dir=Path(temp_dir) / "runtime")
            with (
                patch("buildup.deep_research.cmd_job_new") as create_job,
                self.assertRaisesRegex(ValueError, "max_results_per_search"),
            ):
                run_deep_research(
                    "invalid preflight",
                    runtime_cfg,
                    requests.Session(),
                    logging.getLogger("test"),
                    max_results=0,
                )
            create_job.assert_not_called()
            self.assertFalse(runtime_cfg.base_dir.exists())

    def test_provider_cache_avoids_a_second_network_request(self) -> None:
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [{
                        "title": "Cached result",
                        "url": "https://example.com/result",
                        "text": "source text",
                    }]
                }

        class Session:
            def __init__(self):
                self.calls = 0

            def post(self, url, **kwargs):
                self.calls += 1
                return Response()

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                search_provider="exa",
                exa_api_key="key",
            )
            session = Session()
            broker = SearchBroker(cfg, session=session)
            first = broker.search("agent harness", max_results=3)
            second = broker.search("agent harness", max_results=3)
            query_cache = next((cfg.search_cache_dir / "queries").glob("*.json"))
            cached_payload = json.loads(query_cache.read_text(encoding="utf-8"))
            cached_payload["results"][0]["title"] = "tampered"
            query_cache.write_text(json.dumps(cached_payload), encoding="utf-8")
            repaired = broker.search("agent harness", max_results=3)
            disabled_cfg = BuildupConfig(
                base_dir=Path(temp_dir) / "disabled",
                search_provider="exa",
                exa_api_key="key",
                search_cache_ttl_hours=0,
            )
            disabled_session = Session()
            disabled_broker = SearchBroker(disabled_cfg, session=disabled_session)
            disabled_broker.search("no storage", max_results=3)
            disabled_broker.search("no storage", max_results=3)
        self.assertEqual("exa", first.provider)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertFalse(repaired.cache_hit)
        self.assertEqual(2, session.calls)
        self.assertEqual(2, disabled_session.calls)
        self.assertFalse(disabled_cfg.search_cache_dir.exists())

    def test_exa_content_request_respects_the_official_character_cap(self) -> None:
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [{
                        "title": "Exa result",
                        "url": "https://example.com/exa-cap",
                        "text": "A source body long enough for the normalized search response.",
                    }]
                }

        class Session:
            def __init__(self):
                self.payload = None

            def post(self, url, **kwargs):
                self.payload = kwargs["json"]
                return Response()

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                search_provider="exa",
                exa_api_key="key",
                research_source_chars=100_000,
            )
            session = Session()
            SearchBroker(cfg, session=session).search("current Exa schema")
        self.assertEqual(10_000, session.payload["contents"]["text"]["maxCharacters"])
        self.assertFalse(session.payload["contents"]["text"]["includeHtmlTags"])

    def test_auto_provider_fails_over_and_browser_is_opt_in(self) -> None:
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [{
                        "title": "Exa result",
                        "url": "https://example.com/exa",
                        "text": "source text",
                    }]
                }

        class Session:
            def get(self, url, **kwargs):
                raise requests.ConnectionError("Brave unavailable")

            def post(self, url, **kwargs):
                return Response()

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                search_provider="auto",
                brave_search_api_key="brave-key",
                exa_api_key="exa-key",
            )
            result = SearchBroker(cfg, session=Session()).search("fallback", max_results=2)
            self.assertEqual("exa", result.provider)
            self.assertEqual("brave", result.failures[0]["provider"])

            with self.assertRaises(NoSearchProviderConfigured):
                SearchBroker(BuildupConfig(base_dir=Path(temp_dir))).search("none")

    def test_provider_failure_diagnostics_do_not_leak_api_keys(self) -> None:
        secret = "super-secret-google-key"

        class Session:
            def get(self, url, **kwargs):
                raise requests.HTTPError(
                    f"403 Client Error for url: {url}?key={secret}"
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                search_provider="google-cse",
                google_cse_api_key=secret,
                google_cse_cx="engine",
            )
            with self.assertRaises(SearchProviderError) as error:
                SearchBroker(cfg, session=Session()).search("private query")
        self.assertNotIn(secret, str(error.exception))

    def test_public_url_policy_and_private_redirect_blocking(self) -> None:
        self.assertFalse(is_public_web_url("http://127.0.0.1/private"))
        self.assertFalse(is_public_web_url("http://localhost/private"))
        self.assertTrue(is_public_web_url("https://example.com/research"))

        class RedirectResponse:
            status_code = 302
            headers = {"location": "http://127.0.0.1/private"}

            def close(self):
                return None

        class RedirectSession:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append(url)
                return RedirectResponse()

        session = RedirectSession()
        address = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("buildup.research.reader.socket.getaddrinfo", return_value=address),
        ):
            excerpt = _page_excerpt(
                "https://example.com/start",
                session,
                cfg=BuildupConfig(
                    base_dir=Path(temp_dir),
                    respect_robots_txt=False,
                ),
            )
        self.assertEqual("", excerpt)
        self.assertEqual(["https://example.com/start"], session.calls)

        pdf_session = RedirectSession()
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("buildup.research.reader.socket.getaddrinfo", return_value=address),
            self.assertRaisesRegex(ValueError, "공개 HTTP"),
        ):
            destination = Path(temp_dir) / "paper.pdf"
            _download_pdf(
                "https://example.com/paper.pdf",
                destination,
                pdf_session,
                BuildupConfig(base_dir=Path(temp_dir)),
                logging.getLogger("test"),
            )
        self.assertEqual(["https://example.com/paper.pdf"], pdf_session.calls)
        self.assertFalse(destination.exists())

    def test_page_cache_rejects_tampered_content(self) -> None:
        class PageResponse:
            status_code = 200
            headers = {"content-type": "text/plain"}
            encoding = "utf-8"

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                return iter([b"Verified public source content for the cache integrity test."])

            def close(self):
                return None

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, url, **kwargs):
                self.calls += 1
                return PageResponse()

        address = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("buildup.research.reader.socket.getaddrinfo", return_value=address),
        ):
            cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                respect_robots_txt=False,
            )
            session = Session()
            reader = SafeWebReader(cfg, session=session)
            first = reader.read("https://example.com/source")
            cache_file = next((cfg.search_cache_dir / "pages").glob("*.json"))
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            payload["result"]["content"] = "tampered content"
            cache_file.write_text(json.dumps(payload), encoding="utf-8")
            second = reader.read("https://example.com/source")

        self.assertEqual(first.content, second.content)
        self.assertEqual(2, session.calls)

    def test_pdf_reader_returns_exact_bytes_for_per_run_archival(self) -> None:
        raw_pdf = b"%PDF-1.7\nimmutable reader bytes\n%%EOF\n"

        class PDFResponse:
            status_code = 200
            headers = {"content-type": "application/pdf"}
            encoding = "utf-8"

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                return iter([raw_pdf])

            def close(self):
                return None

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, url, **kwargs):
                self.calls += 1
                return PDFResponse()

        address = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("buildup.research.reader.socket.getaddrinfo", return_value=address),
            patch.object(
                SafeWebReader,
                "_extract_pdf",
                return_value="Exact extracted PDF passage with enough content for evidence.",
            ),
        ):
            cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                respect_robots_txt=False,
            )
            session = Session()
            reader = SafeWebReader(cfg, session=session)
            first = reader.read("https://example.com/paper.pdf")
            second = reader.read("https://example.com/paper.pdf")
            self.assertFalse((cfg.search_cache_dir / "pages").exists())

        self.assertEqual(raw_pdf, first.raw_bytes)
        self.assertEqual(hashlib.sha256(raw_pdf).hexdigest(), first.raw_sha256)
        self.assertEqual(raw_pdf, second.raw_bytes)
        self.assertEqual(2, session.calls)

    def test_doctor_reports_ready_for_a_complete_local_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                research_model="research-model",
                reviewer_model="reviewer-model",
                tavily_api_key="configured",
            )
            for name in ("deep-research", "personal-study-coach", "paper-pdf-translation"):
                directory = cfg.skills_dir / name
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test skill for {name}\n---\n",
                    encoding="utf-8",
                )
            report = run_doctor(
                cfg,
                _OllamaSession(cfg.research_model, cfg.reviewer_model),
            )
            self.assertTrue(report.ready)
            self.assertTrue(all(check.status != "FAIL" for check in report.checks))

    def test_doctor_checks_the_selected_search_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                search_provider="tavily",
                exa_api_key="configured-but-not-selected",
            )
            report = run_doctor(
                cfg,
                _OllamaSession(cfg.research_model, cfg.reviewer_model),
            )
            search_check = next(item for item in report.checks if item.name == "Research search")
            self.assertEqual("FAIL", search_check.status)
            self.assertIn("tavily", search_check.detail)

            auto_cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                search_provider="auto",
                tavily_api_key="usable",
                browser_search_enabled=True,
            )
            with patch(
                "buildup.doctor._browser_runtime",
                return_value=(False, "Chromium missing"),
            ):
                auto_report = run_doctor(
                    auto_cfg,
                    _OllamaSession(auto_cfg.research_model, auto_cfg.reviewer_model),
                )
            auto_search = next(
                item for item in auto_report.checks if item.name == "Research search"
            )
            self.assertEqual("PASS", auto_search.status)
            self.assertIn("browser unavailable", auto_search.detail)

    def test_packaged_core_skills_exist_and_local_skill_can_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(base_dir=Path(temp_dir))
            builtins = load_skills(cfg, refresh=True)
            names = {skill.name for skill in builtins}
            self.assertTrue(
                {"deep-research", "personal-study-coach", "paper-pdf-translation"} <= names
            )

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
