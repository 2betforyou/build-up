from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from friday.config import FridayConfig
from friday.deep_research import (
    ROLE_KEYS,
    latest_completed_research_run,
    list_research_runs,
    run_deep_research,
)
from friday.intent import parse_intent_instant
from friday.ollama import chat
from friday.rendering import (
    _ascii_banner_lines,
    _build_header_lines,
    _render_banner_word,
    _weekday_name,
)
from friday.search import research_search
from friday.shell import InteractiveShell


class DeepResearchTests(unittest.TestCase):
    def test_single_model_call_and_separate_role_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = FridayConfig(base_dir=Path(temp_dir), night_model="one-local-model")
            calls = []
            search_calls = []

            def fake_search(query, cfg, session, max_results=10):
                search_calls.append(query)
                return [
                    {
                        "title": "Primary source",
                        "href": "https://example.test/one",
                        "body": "evidence one",
                        "raw_content": "full evidence one",
                    },
                    {
                        "title": "Second source",
                        "href": "https://example.test/two",
                        "body": "evidence two",
                        "raw_content": "full evidence two",
                    },
                ], "Fake Search"

            payload = {
                "01_coordinator_scope": {
                    "question": "test question",
                    "scope": "test scope",
                    "subquestions": [],
                    "exclusions": [],
                },
                "02_literature_review": {"sources": [], "coverage_gaps": []},
                "03_research_notes": {"notes": [], "conflicts": []},
                "04_critical_synthesis": {
                    "synthesis": "synthesis [S1]",
                    "agreements": [],
                    "disagreements": [],
                    "limitations": [],
                    "confidence": "medium",
                },
                "05_reference_audit": {
                    "verified_source_ids": ["S1"],
                    "unsupported_claims": [],
                    "citation_issues": [],
                },
                "06_final_report": "# 결과\n\n근거가 있다. [S1]",
            }

            def fake_chat(session, cfg, model, messages, **kwargs):
                calls.append({"model": model, "messages": messages, "kwargs": kwargs})
                return json.dumps(payload, ensure_ascii=False)

            result = run_deep_research(
                "local agent harness",
                cfg,
                object(),
                None,
                job_id="20260812-120000-test",
                search_fn=fake_search,
                chat_fn=fake_chat,
                now=datetime(2026, 8, 12, 12, 0, 0),
            )

            self.assertEqual(1, len(calls))
            self.assertEqual(3, len(search_calls))
            self.assertEqual("one-local-model", calls[0]["model"])
            self.assertEqual(2, len(calls[0]["messages"]))
            self.assertEqual(["system", "user"], [message["role"] for message in calls[0]["messages"]])
            self.assertTrue(calls[0]["kwargs"]["json_mode"])
            self.assertEqual(list(ROLE_KEYS), calls[0]["kwargs"]["json_schema"]["required"])
            self.assertFalse(calls[0]["kwargs"]["sanitize_thinking"])
            self.assertEqual(set(ROLE_KEYS), set(result.role_files))

            metadata = json.loads((result.run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(1, metadata["model_calls"])
            self.assertEqual(list(ROLE_KEYS), metadata["role_order"])
            self.assertEqual("completed", metadata["status"])
            self.assertEqual("completed", metadata["phase"])
            self.assertEqual(3, metadata["search_requests"])
            self.assertEqual([], metadata["search_errors"])
            self.assertEqual("single-inference-staged-research", metadata["execution_mode"])
            self.assertIn("not independent model sessions", metadata["isolation_level"])
            self.assertEqual(["S1"], metadata["citation_stats"]["cited_source_ids"])
            self.assertTrue(metadata["sha256"])
            self.assertTrue(result.study_guide_path.is_file())
            self.assertIn("Self-check", result.study_guide_path.read_text(encoding="utf-8"))

            for key in ROLE_KEYS[:-1]:
                saved = json.loads(result.role_files[key].read_text(encoding="utf-8"))
                self.assertEqual(payload[key], saved)
            self.assertIn("[S1]", result.role_files["06_final_report"].read_text(encoding="utf-8"))
            self.assertNotEqual(
                result.role_files["01_coordinator_scope"],
                result.role_files["05_reference_audit"],
            )

    def test_invalid_contract_is_recorded_as_failed_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = FridayConfig(base_dir=Path(temp_dir), night_model="one-local-model")
            model_calls = []

            def fake_search(query, cfg, session, max_results=10):
                return [{
                    "title": "Source",
                    "href": "https://example.test/source",
                    "body": "evidence",
                }], "Fake Search"

            def fake_chat(*args, **kwargs):
                model_calls.append(1)
                return '{"01_coordinator_scope": {}}'

            with self.assertRaisesRegex(ValueError, "역할 경계"):
                run_deep_research(
                    "invalid response test",
                    cfg,
                    object(),
                    None,
                    job_id="research-job",
                    search_fn=fake_search,
                    chat_fn=fake_chat,
                    now=datetime(2026, 8, 12, 12, 0, 0),
                )

            self.assertEqual(1, len(model_calls))
            run = list_research_runs("research-job", cfg)[0]
            self.assertEqual("failed", run["status"])
            self.assertEqual("validation", run["phase"])
            self.assertEqual(1, run["model_calls"])
            self.assertTrue((Path(run["run_dir"]) / ".invalid-model-response.txt").is_file())

    def test_study_link_skips_a_newer_failed_research_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = FridayConfig(base_dir=Path(temp_dir))
            root = cfg.workspace_dir / "research-job" / "deep-research"
            completed = root / "completed"
            failed = root / "failed"
            completed.mkdir(parents=True)
            failed.mkdir(parents=True)
            (completed / "metadata.json").write_text(json.dumps({
                "run_id": "completed",
                "status": "completed",
                "updated_at": "2026-08-12T10:00:00+09:00",
            }), encoding="utf-8")
            (failed / "metadata.json").write_text(json.dumps({
                "run_id": "failed",
                "status": "failed",
                "updated_at": "2026-08-12T11:00:00+09:00",
            }), encoding="utf-8")

            selected = latest_completed_research_run("research-job", cfg)
            self.assertEqual("completed", selected["run_id"])

    def test_explicit_intents_do_not_need_an_llm_parser(self) -> None:
        cfg = FridayConfig()
        research = parse_intent_instant("로컬 모델용 에이전트 하네스를 딥 리서치해줘", cfg)
        translation = parse_intent_instant("paper.pdf 번역해줘", cfg)
        self.assertEqual("deep_research", research["intent"])
        self.assertEqual("로컬 모델용 에이전트 하네스", research["params"]["query"])
        self.assertEqual("paper_translate", translation["intent"])
        self.assertEqual("paper.pdf", translation["params"]["source"])
        self.assertIsNone(parse_intent_instant("이 논문의 abstract만 번역해줘", cfg))

    def test_weekday_banner_uses_requested_day(self) -> None:
        wednesday = datetime(2026, 8, 12)
        self.assertEqual("WEDNESDAY", _weekday_name(wednesday))
        self.assertEqual(
            _render_banner_word("WEDNESDAY"),
            _ascii_banner_lines(wednesday),
        )
        self.assertEqual(6, len(_ascii_banner_lines(wednesday)))
        self.assertTrue(_render_banner_word("FRIDAY")[0].startswith("███████╗"))
        self.assertTrue(_render_banner_word("BUILD-UP")[0].startswith("██████╗"))

    def test_interactive_shell_starts_in_nighttime(self) -> None:
        shell = InteractiveShell(FridayConfig(), object(), None)
        self.assertEqual("nighttime", shell.assistant_mode)

    def test_header_uses_public_brand_and_research_mode_label(self) -> None:
        rendered = "\n".join(
            line.plain for line in _build_header_lines("auto", None, assistant_mode="night")
        )
        self.assertIn("build-up", rendered)
        self.assertIn("assistant     research", rendered)

    def test_research_search_requests_advanced_raw_content(self) -> None:
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [{
                        "title": "Source",
                        "url": "https://example.test/source",
                        "content": "snippet",
                        "raw_content": "full page",
                        "score": 0.9,
                    }]
                }

        class Session:
            def __init__(self):
                self.payloads = []

            def post(self, url, json, headers, timeout):
                self.payloads.append(json)
                return Response()

        cfg = FridayConfig(tavily_api_key="test-key", search_provider="tavily")
        session = Session()
        results, engine = research_search("agent harness", cfg, session, max_results=3)
        self.assertEqual("Tavily advanced", engine)
        self.assertEqual("full page", results[0]["raw_content"])
        self.assertEqual("advanced", session.payloads[0]["search_depth"])
        self.assertEqual("markdown", session.payloads[0]["include_raw_content"])
        self.assertEqual(3, session.payloads[0]["chunks_per_source"])

    def test_ollama_chat_sends_json_schema_in_one_request(self) -> None:
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"message": {"content": '{"ok": true}'}}

        class Session:
            def __init__(self):
                self.payloads = []

            def post(self, url, json, timeout):
                self.payloads.append(json)
                return Response()

        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }
        session = Session()
        result = chat(
            session,
            FridayConfig(),
            "test-model",
            [{"role": "user", "content": "ok"}],
            json_schema=schema,
            sanitize_thinking=False,
        )
        self.assertEqual('{"ok": true}', result)
        self.assertEqual(1, len(session.payloads))
        self.assertEqual(schema, session.payloads[0]["format"])


if __name__ == "__main__":
    unittest.main()
