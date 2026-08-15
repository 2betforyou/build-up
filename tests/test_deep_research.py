from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from buildup.config import BuildupConfig
from buildup.deep_research import (
    ROLE_KEYS,
    evaluate_research_run,
    latest_completed_research_run,
    list_research_runs,
    run_deep_research,
)
from buildup.intent import parse_intent_instant
from buildup.ollama import chat
from buildup.rendering import (
    _ascii_banner_lines,
    _build_header_lines,
    _render_banner_word,
    _weekday_name,
    build_prompt,
    read_prompt,
)
from buildup.research.citations import append_used_sources, audit_report, verify_exact_passage
from buildup.research.models import Claim, Evidence, Source, now_iso, stable_id
from buildup.research.reader import ReaderResult
from buildup.research.store import ResearchStore
from buildup.search import research_search
from buildup.shell import InteractiveShell


def _cfg(base: Path, **overrides):
    values = {
        "base_dir": base,
        "research_model": "research-model",
        "reviewer_model": "reviewer-model",
        "research_max_rounds": 2,
        "research_max_tasks": 6,
        "research_max_searches": 10,
        "research_max_sources": 10,
        "research_max_model_calls": 16,
    }
    values.update(overrides)
    return BuildupConfig(**values)


def _plan(tasks):
    return {
        "contract": {
            "question": "How does the research engine work?",
            "objective": "Explain its evidence controls.",
            "audience": "developers",
            "deliverable": "cited report",
            "subquestions": [item["question"] for item in tasks],
            "in_scope": ["research engine"],
            "out_of_scope": [],
            "source_requirements": ["primary or official sources"],
            "success_criteria": ["all factual claims cite evidence"],
            "freshness": "current where relevant",
            "assumptions": [],
        },
        "tasks": tasks,
    }


class DeepResearchTests(unittest.TestCase):
    def test_adaptive_run_writes_auditable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(Path(temp_dir), research_max_rounds=1)
            url = "https://example.com/research"
            source_id = stable_id("S", url)
            passage = (
                "Build-up checks every quoted evidence passage against saved source text "
                "before accepting that passage into its durable evidence ledger."
            )
            evidence_id = stable_id("E", source_id, passage, length=14)
            claim_text = "Build-up validates quoted evidence against saved source text."
            claim_id = stable_id("C", claim_text, length=14)
            calls = []
            payloads = [
                _plan([{
                    "question": "How is evidence validated?",
                    "queries": ["evidence validation"],
                    "rationale": "core quality control",
                    "priority": 5,
                    "critical": True,
                }]),
                {
                    "evidence": [{
                        "source_id": source_id,
                        "passage": passage,
                        "locator": "Evidence section",
                        "stance": "supports",
                        "claim_hint": claim_text,
                        "relevance": 1.0,
                        "credibility": 0.85,
                        "notes": "",
                    }],
                    "unanswered": [],
                },
                {
                    "coverage": 0.95,
                    "sufficient": True,
                    "stop_reason": "critical question covered",
                    "claims": [{
                        "text": claim_text,
                        "evidence_ids": [evidence_id],
                        "contradicting_evidence_ids": [],
                        "confidence": 0.9,
                        "critical": True,
                        "status": "supported",
                    }],
                    "gaps": [],
                },
                {
                    "report": (
                        "# Result\n\nBuild-up validates every accepted quotation against its "
                        f"saved source text before adding it to the durable ledger. [{evidence_id}]"
                    ),
                    "used_claim_ids": [claim_id],
                },
                {
                    "overall_passed": True,
                    "summary": "The exact passage entails the claim.",
                    "claim_results": [{
                        "claim_id": claim_id,
                        "supported": True,
                        "evidence_ids": [evidence_id],
                        "reason": "Direct entailment.",
                    }],
                },
            ]

            def fake_chat(session, cfg, model, messages, **kwargs):
                calls.append((model, kwargs["json_schema"]))
                return json.dumps(payloads.pop(0), ensure_ascii=False)

            def fake_search(query, cfg, session, max_results=5):
                return [{
                    "title": "Primary source",
                    "href": url,
                    "body": passage,
                    "raw_content": "",
                }], "fixture"

            raw_pdf = b"%PDF-1.7\nexact immutable test PDF bytes\n%%EOF\n"

            def fake_reader(requested_url):
                content = (
                    passage
                    + " This additional context makes the saved source comfortably long."
                )
                return ReaderResult(
                    url=requested_url,
                    final_url=requested_url,
                    title="Primary source PDF",
                    content=content,
                    content_type="application/pdf",
                    retrieved_at=now_iso(),
                    content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    locator_kind="page",
                    metadata={"injected": True},
                    raw_sha256=hashlib.sha256(raw_pdf).hexdigest(),
                    raw_bytes=raw_pdf,
                )

            result = run_deep_research(
                "local agent harness",
                cfg,
                object(),
                None,
                job_id="research-job",
                max_results=7,
                search_fn=fake_search,
                chat_fn=fake_chat,
                reader_fn=fake_reader,
                depth="shallow",
                now=datetime(2026, 8, 12, 12, 0, 0),
            )

            self.assertEqual(5, result.model_calls)
            self.assertEqual(1, result.rounds)
            self.assertEqual(1, result.source_count)
            self.assertEqual(1, result.evidence_count)
            self.assertEqual("reviewer-model", calls[-1][0])
            self.assertEqual(set(ROLE_KEYS), set(result.role_files))
            self.assertIn(evidence_id, result.report)
            self.assertIn("## Sources", result.report)

            expected = {
                "state.json", "events.jsonl", "metadata.json", "manifest.json",
                "00-sources.json", "01-contract.json", "02-plan.json", "03-sources.jsonl",
                "04-evidence.jsonl", "05-claims.jsonl", "05-gaps.json",
                "06-report.md", "07-study-guide.md", "08-citation-audit.json",
                "09-claim-audit.json",
            }
            self.assertTrue(expected <= {item.name for item in result.run_dir.iterdir()})
            metadata = json.loads((result.run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual("completed", metadata["status"])
            self.assertEqual(5, metadata["model_calls"])
            self.assertEqual(7, metadata["max_results_per_search"])
            self.assertTrue(metadata["citation_stats"]["passed"])
            self.assertTrue(metadata["claim_audit"]["passed"])

            manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
            raw_relative = f"raw-sources/{source_id}.pdf"
            self.assertTrue(
                {"state.json", "metadata.json", "events.jsonl", "00-sources.json"}
                <= set(manifest["files"])
            )
            self.assertIn(raw_relative, manifest["files"])
            self.assertEqual(
                hashlib.sha256(raw_pdf).hexdigest(),
                manifest["files"][raw_relative]["sha256"],
            )
            self.assertEqual(raw_pdf, (result.run_dir / raw_relative).read_bytes())
            self.assertEqual("", result.knowledge_error)
            self.assertIsNotNone(result.knowledge_vault_path)
            assert result.knowledge_vault_path is not None
            wiki_claim = (
                result.knowledge_vault_path / "pages" / "claims" / f"{claim_id}.md"
            ).read_text(encoding="utf-8")
            self.assertIn("original PDF hash", wiki_claim)
            self.assertIn(hashlib.sha256(raw_pdf).hexdigest(), wiki_claim)
            raw_path = result.run_dir / raw_relative
            raw_path.write_bytes(raw_pdf + b"tampered")
            raw_tamper = evaluate_research_run("latest", "research-job", cfg)
            self.assertFalse(raw_tamper["passed"])
            self.assertIn(raw_relative, raw_tamper["failures"]["manifest_files"])
            raw_path.write_bytes(raw_pdf)
            (result.run_dir / "manifest.json").unlink()
            resumed = run_deep_research(
                "",
                cfg,
                object(),
                None,
                job_id="research-job",
                search_fn=fake_search,
                chat_fn=fake_chat,
                resume="latest",
            )
            self.assertTrue(resumed.resumed)
            self.assertEqual(5, resumed.model_calls)
            self.assertTrue((result.run_dir / "manifest.json").is_file())
            resumed_state = json.loads(
                (result.run_dir / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(7, resumed_state["max_results_per_search"])

            with self.assertRaisesRegex(ValueError, "저장된 research policy"):
                run_deep_research(
                    "",
                    cfg,
                    object(),
                    None,
                    job_id="research-job",
                    resume="latest",
                    max_results=5,
                )

            evaluation = evaluate_research_run("latest", "research-job", cfg)
            self.assertTrue(evaluation["passed"], evaluation)
            self.assertEqual(1.0, evaluation["metrics"]["evidence_exactness"])
            self.assertTrue((result.run_dir / "10-evaluation.json").is_file())

            manifest_path = result.run_dir / "manifest.json"
            incomplete_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            incomplete_manifest["files"].pop("state.json")
            manifest_path.write_text(
                json.dumps(incomplete_manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            tampered = evaluate_research_run("latest", "research-job", cfg)
            self.assertFalse(tampered["passed"])
            self.assertIn(
                "manifest.json:missing:state.json",
                tampered["failures"]["manifest_files"],
            )

            corrupted_state = json.loads(
                (result.run_dir / "state.json").read_text(encoding="utf-8")
            )
            corrupted_state["sources"][0]["content_sha256"] = "0" * 64
            (result.run_dir / "state.json").write_text(
                json.dumps(corrupted_state, ensure_ascii=False),
                encoding="utf-8",
            )
            corrupted_evaluation = evaluate_research_run(
                "latest", "research-job", cfg
            )
            self.assertFalse(corrupted_evaluation["passed"])
            self.assertIn(
                f"source:{source_id}:sha256",
                corrupted_evaluation["failures"]["checkpoint_invariants"],
            )
            with self.assertRaisesRegex(ValueError, "checkpoint invariant"):
                run_deep_research(
                    "",
                    cfg,
                    object(),
                    None,
                    job_id="research-job",
                    resume="latest",
                )

    def test_non_exact_model_quote_is_rejected_and_run_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(Path(temp_dir), research_max_rounds=1)
            url = "https://example.com/source"
            source_id = stable_id("S", url)
            passage = (
                "The actual source sentence is long enough to be accepted only when it is quoted exactly."
            )
            payloads = [
                _plan([{
                    "question": "What does the source say?",
                    "queries": ["source question"],
                    "rationale": "test",
                    "priority": 5,
                    "critical": True,
                }]),
                {
                    "evidence": [{
                        "source_id": source_id,
                        "passage": "A paraphrase that does not occur in the stored source at all.",
                        "locator": "section",
                        "stance": "supports",
                        "claim_hint": "unsupported",
                        "relevance": 1.0,
                        "credibility": 1.0,
                        "notes": "",
                    }],
                    "unanswered": [],
                },
            ]

            def fake_chat(*args, **kwargs):
                return json.dumps(payloads.pop(0))

            def fake_search(*args, **kwargs):
                return [{
                    "title": "Source",
                    "href": url,
                    "body": passage,
                    "raw_content": passage + " More source context is present for deterministic testing.",
                }], "fixture"

            with self.assertRaisesRegex(RuntimeError, "검증 가능한 원문 근거"):
                run_deep_research(
                    "exact quote test",
                    cfg,
                    object(),
                    None,
                    job_id="job",
                    search_fn=fake_search,
                    chat_fn=fake_chat,
                    depth="shallow",
                )
            run = list_research_runs("job", cfg)[0]
            state = json.loads((Path(run["run_dir"]) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", state["status"])
            self.assertEqual(2, state["budget"]["model_calls_used"])
            self.assertTrue(any(row["phase"] == "evidence_validation" for row in state["errors"]))
            self.assertFalse((Path(run["run_dir"]) / "06-report.md").exists())

    def test_material_gap_creates_a_second_round_followup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(Path(temp_dir), research_min_coverage=0.8)
            url_one = "https://example.com/one"
            url_two = "https://example.com/two"
            source_one = stable_id("S", url_one)
            source_two = stable_id("S", url_two)
            passage_one = "The first source documents the baseline behavior with a precise reproducible observation."
            passage_two = "The second source documents the missing limitation with a precise counterexample and boundary."
            evidence_one = stable_id("E", source_one, passage_one, length=14)
            evidence_two = stable_id("E", source_two, passage_two, length=14)
            claim_one_text = "The baseline behavior is documented."
            claim_two_text = "A material limitation is documented."
            claim_one = stable_id("C", claim_one_text, length=14)
            claim_two = stable_id("C", claim_two_text, length=14)
            search_calls = []
            assessment_count = 0

            def fake_search(query, cfg, session, max_results=5):
                search_calls.append(query)
                if "limitation" in query:
                    return [{
                        "title": "Limitation",
                        "href": url_two,
                        "body": passage_two,
                        "raw_content": passage_two + " Additional exact context for the follow-up source.",
                    }], "fixture"
                return [{
                    "title": "Baseline",
                    "href": url_one,
                    "body": passage_one,
                    "raw_content": passage_one + " Additional exact context for the baseline source.",
                }], "fixture"

            def fake_chat(session, cfg, model, messages, **kwargs):
                nonlocal assessment_count
                required = tuple(kwargs["json_schema"].get("required", []))
                user = messages[-1]["content"]
                if required == ("contract", "tasks"):
                    return json.dumps(_plan([{
                        "question": "What is the baseline?",
                        "queries": ["baseline evidence"],
                        "rationale": "establish baseline",
                        "priority": 5,
                        "critical": True,
                    }]))
                if required == ("evidence", "unanswered"):
                    if source_two in user:
                        source_id, passage, hint = source_two, passage_two, claim_two_text
                    else:
                        source_id, passage, hint = source_one, passage_one, claim_one_text
                    return json.dumps({
                        "evidence": [{
                            "source_id": source_id,
                            "passage": passage,
                            "locator": "section",
                            "stance": "supports",
                            "claim_hint": hint,
                            "relevance": 1.0,
                            "credibility": 0.8,
                            "notes": "",
                        }],
                        "unanswered": [],
                    })
                if required[:3] == ("coverage", "sufficient", "stop_reason"):
                    assessment_count += 1
                    if assessment_count == 1:
                        return json.dumps({
                            "coverage": 0.55,
                            "sufficient": False,
                            "stop_reason": "limitation missing",
                            "claims": [{
                                "text": claim_one_text,
                                "evidence_ids": [evidence_one],
                                "contradicting_evidence_ids": [],
                                "confidence": 0.8,
                                "critical": True,
                                "status": "supported",
                            }],
                            "gaps": [{
                                "question": "What material limitation exists?",
                                "reason": "The baseline source does not establish boundaries.",
                                "importance": 5,
                                "suggested_queries": ["material limitation evidence"],
                            }],
                        })
                    return json.dumps({
                        "coverage": 0.94,
                        "sufficient": True,
                        "stop_reason": "covered",
                        "claims": [
                            {
                                "text": claim_one_text,
                                "evidence_ids": [evidence_one],
                                "contradicting_evidence_ids": [],
                                "confidence": 0.85,
                                "critical": True,
                                "status": "supported",
                            },
                            {
                                "text": claim_two_text,
                                "evidence_ids": [evidence_two],
                                "contradicting_evidence_ids": [],
                                "confidence": 0.85,
                                "critical": True,
                                "status": "supported",
                            },
                        ],
                        "gaps": [],
                    })
                if required == ("report", "used_claim_ids"):
                    return json.dumps({
                        "report": (
                            "# Synthesis\n\nThe baseline and its material limitation are both "
                            f"documented by exact source passages. [{evidence_one}] [{evidence_two}]"
                        ),
                        "used_claim_ids": [claim_one, claim_two],
                    })
                return json.dumps({
                    "overall_passed": True,
                    "summary": "Both claims are entailed.",
                    "claim_results": [
                        {"claim_id": claim_one, "supported": True, "evidence_ids": [evidence_one], "reason": "direct"},
                        {"claim_id": claim_two, "supported": True, "evidence_ids": [evidence_two], "reason": "direct"},
                    ],
                })

            result = run_deep_research(
                "adaptive gap test",
                cfg,
                object(),
                None,
                job_id="job",
                search_fn=fake_search,
                chat_fn=fake_chat,
                depth="standard",
            )
            self.assertEqual(2, result.rounds)
            self.assertEqual(2, result.source_count)
            self.assertEqual(2, result.evidence_count)
            self.assertEqual(7, result.model_calls)
            self.assertEqual(["baseline evidence", "material limitation evidence"], search_calls)
            state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(2, len(state["tasks"]))
            self.assertEqual("resolved", state["gaps"][0]["status"])

    def test_resume_reuses_completed_task_and_persisted_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(Path(temp_dir), research_max_rounds=1, research_max_model_calls=12)
            queries = ["first query", "second query"]
            urls = {
                "first query": "https://example.com/first",
                "second query": "https://example.com/second",
            }
            passages = {
                "first query": "The first task has an exact source passage that is sufficiently long for validation.",
                "second query": "The second task has another exact source passage that is sufficiently long for validation.",
            }
            source_ids = {query: stable_id("S", url) for query, url in urls.items()}
            evidence_ids = {
                query: stable_id("E", source_ids[query], passages[query], length=14)
                for query in queries
            }
            claim_text = "Both task observations are supported by saved source passages."
            claim_id = stable_id("C", claim_text, length=14)
            search_calls = []
            evidence_calls = 0

            def fake_search(query, cfg, session, max_results=5):
                search_calls.append(query)
                passage = passages[query]
                return [{
                    "title": query,
                    "href": urls[query],
                    "body": passage,
                    "raw_content": passage + " Additional context preserves a stable readable source.",
                }], "fixture"

            def first_chat(session, cfg, model, messages, **kwargs):
                nonlocal evidence_calls
                required = tuple(kwargs["json_schema"].get("required", []))
                if required == ("contract", "tasks"):
                    return json.dumps(_plan([
                        {"question": "First task", "queries": [queries[0]], "rationale": "one", "priority": 5, "critical": True},
                        {"question": "Second task", "queries": [queries[1]], "rationale": "two", "priority": 4, "critical": True},
                    ]))
                if required == ("evidence", "unanswered"):
                    evidence_calls += 1
                    if evidence_calls == 2:
                        raise KeyboardInterrupt()
                    query = next(item for item in queries if source_ids[item] in messages[-1]["content"])
                    return json.dumps({
                        "evidence": [{
                            "source_id": source_ids[query],
                            "passage": passages[query],
                            "locator": "section",
                            "stance": "supports",
                            "claim_hint": claim_text,
                            "relevance": 1.0,
                            "credibility": 0.8,
                            "notes": "",
                        }],
                        "unanswered": [],
                    })
                raise AssertionError("run should have been interrupted before assessment")

            with self.assertRaises(KeyboardInterrupt):
                run_deep_research(
                    "resume test",
                    cfg,
                    object(),
                    None,
                    job_id="job",
                    search_fn=fake_search,
                    chat_fn=first_chat,
                    depth="shallow",
                )
            interrupted = list_research_runs("job", cfg)[0]
            self.assertEqual("interrupted", interrupted["status"])
            state = json.loads((Path(interrupted["run_dir"]) / "state.json").read_text(encoding="utf-8"))
            completed_query = next(
                task["queries"][0] for task in state["tasks"] if task["status"] == "completed"
            )

            def resume_chat(session, cfg, model, messages, **kwargs):
                required = tuple(kwargs["json_schema"].get("required", []))
                if required == ("evidence", "unanswered"):
                    query = next(item for item in queries if source_ids[item] in messages[-1]["content"])
                    return json.dumps({
                        "evidence": [{
                            "source_id": source_ids[query],
                            "passage": passages[query],
                            "locator": "section",
                            "stance": "supports",
                            "claim_hint": claim_text,
                            "relevance": 1.0,
                            "credibility": 0.8,
                            "notes": "",
                        }],
                        "unanswered": [],
                    })
                if required[:3] == ("coverage", "sufficient", "stop_reason"):
                    return json.dumps({
                        "coverage": 0.95,
                        "sufficient": True,
                        "stop_reason": "covered",
                        "claims": [{
                            "text": claim_text,
                            "evidence_ids": list(evidence_ids.values()),
                            "contradicting_evidence_ids": [],
                            "confidence": 0.9,
                            "critical": True,
                            "status": "supported",
                        }],
                        "gaps": [],
                    })
                if required == ("report", "used_claim_ids"):
                    return json.dumps({
                        "report": (
                            "# Resume\n\nBoth completed tasks contribute exact evidence to the "
                            f"resumed final report. [{evidence_ids[queries[0]]}] "
                            f"[{evidence_ids[queries[1]]}]"
                        ),
                        "used_claim_ids": [claim_id],
                    })
                return json.dumps({
                    "overall_passed": True,
                    "summary": "supported",
                    "claim_results": [{
                        "claim_id": claim_id,
                        "supported": True,
                        "evidence_ids": list(evidence_ids.values()),
                        "reason": "direct",
                    }],
                })

            result = run_deep_research(
                "",
                cfg,
                object(),
                None,
                job_id="job",
                search_fn=fake_search,
                chat_fn=resume_chat,
                resume="latest",
            )
            self.assertTrue(result.resumed)
            self.assertEqual(7, result.model_calls)
            self.assertEqual(1, search_calls.count(completed_query))
            resumed_state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(all(task["status"] == "completed" for task in resumed_state["tasks"]))

    def test_model_call_budget_is_never_silently_increased(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(
                Path(temp_dir),
                research_max_rounds=1,
                research_max_model_calls=2,
            )

            def fake_chat(*args, **kwargs):
                return json.dumps(_plan([{
                    "question": "Budgeted task",
                    "queries": ["budget query"],
                    "rationale": "test",
                    "priority": 5,
                    "critical": True,
                }]))

            with self.assertRaisesRegex(RuntimeError, "검증 가능한 원문 근거"):
                run_deep_research(
                    "budget test",
                    cfg,
                    object(),
                    None,
                    job_id="job",
                    search_fn=lambda *args, **kwargs: ([], "fixture"),
                    chat_fn=fake_chat,
                    depth="shallow",
                )
            run = list_research_runs("job", cfg)[0]
            self.assertEqual(1, run["model_calls"])
            self.assertEqual(0, run["search_requests"])

    def test_writer_correction_remains_single_use_across_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(Path(temp_dir), research_max_rounds=1)
            url = "https://example.com/bounded-correction"
            passage = (
                "This source contains a literal passage proving the bounded correction test claim."
            )
            source_id = stable_id("S", url)
            evidence_id = stable_id("E", source_id, passage, length=14)
            claim_text = "The bounded correction test claim is supported."
            claim_id = stable_id("C", claim_text, length=14)
            writer_calls = 0

            def fake_search(*args, **kwargs):
                return [{
                    "title": "Bounded correction source",
                    "href": url,
                    "body": passage,
                    "raw_content": passage + " Additional primary-source context is stored here.",
                }], "fixture"

            def fake_chat(session, cfg, model, messages, **kwargs):
                nonlocal writer_calls
                required = tuple(kwargs["json_schema"].get("required", []))
                if required == ("contract", "tasks"):
                    return json.dumps(_plan([{
                        "question": "Is the claim supported?",
                        "queries": ["bounded correction evidence"],
                        "rationale": "quality gate regression",
                        "priority": 5,
                        "critical": True,
                    }]))
                if required == ("evidence", "unanswered"):
                    return json.dumps({
                        "evidence": [{
                            "source_id": source_id,
                            "passage": passage,
                            "locator": "section",
                            "stance": "supports",
                            "claim_hint": claim_text,
                            "relevance": 1.0,
                            "credibility": 0.8,
                            "notes": "",
                        }],
                        "unanswered": [],
                    })
                if required[:3] == ("coverage", "sufficient", "stop_reason"):
                    return json.dumps({
                        "coverage": 1.0,
                        "sufficient": True,
                        "stop_reason": "covered",
                        "claims": [{
                            "text": claim_text,
                            "evidence_ids": [evidence_id],
                            "contradicting_evidence_ids": [],
                            "confidence": 0.9,
                            "critical": True,
                            "status": "supported",
                        }],
                        "gaps": [],
                    })
                if required == ("report", "used_claim_ids"):
                    writer_calls += 1
                    return json.dumps({
                        "report": (
                            "# Bad draft\n\nThis deliberately invalid factual paragraph keeps "
                            "citing an unknown evidence record. [E000000000000]"
                        ),
                        "used_claim_ids": [claim_id],
                    })
                return json.dumps({
                    "overall_passed": True,
                    "summary": "The ledger claim itself is supported.",
                    "claim_results": [{
                        "claim_id": claim_id,
                        "supported": True,
                        "evidence_ids": [evidence_id],
                        "reason": "direct",
                    }],
                })

            with self.assertRaisesRegex(RuntimeError, "품질 gate"):
                run_deep_research(
                    "bounded correction",
                    cfg,
                    object(),
                    None,
                    job_id="job",
                    search_fn=fake_search,
                    chat_fn=fake_chat,
                    depth="shallow",
                )
            self.assertEqual(2, writer_calls)
            failed = list_research_runs("job", cfg)[0]
            state_path = Path(failed["run_dir"]) / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(1, state["writer_corrections_used"])
            self.assertEqual(2, state["writer_attempts"])

            resume_calls = 0

            def unexpected_chat(*args, **kwargs):
                nonlocal resume_calls
                resume_calls += 1
                raise AssertionError("resume must not grant another correction")

            with self.assertRaisesRegex(RuntimeError, "bounded writer correction already used"):
                run_deep_research(
                    "",
                    cfg,
                    object(),
                    None,
                    job_id="job",
                    search_fn=fake_search,
                    chat_fn=unexpected_chat,
                    resume="latest",
                )
            self.assertEqual(0, resume_calls)

    def test_search_snippet_is_never_accepted_as_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(Path(temp_dir), research_max_rounds=1)
            long_snippet = (
                "A search-engine snippet can be long, but it is not a verified copy of the source page. "
                "Build-up must never promote it into the exact evidence ledger."
            )

            def fake_chat(*args, **kwargs):
                return json.dumps(_plan([{
                    "question": "Can a snippet become evidence?",
                    "queries": ["snippet evidence"],
                    "rationale": "source integrity",
                    "priority": 5,
                    "critical": True,
                }]))

            def fake_search(*args, **kwargs):
                return [{
                    "title": "Unreachable source",
                    "href": "https://example.com/unreachable",
                    "body": long_snippet,
                }], "fixture"

            def failed_reader(url):
                raise OSError("offline")

            with self.assertRaisesRegex(RuntimeError, "검증 가능한 원문 근거"):
                run_deep_research(
                    "snippet integrity",
                    cfg,
                    object(),
                    None,
                    job_id="job",
                    search_fn=fake_search,
                    chat_fn=fake_chat,
                    reader_fn=failed_reader,
                    depth="shallow",
                )
            run = list_research_runs("job", cfg)[0]
            state = json.loads(
                (Path(run["run_dir"]) / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], state["sources"])
            self.assertEqual([], state["evidence"])
            self.assertTrue(any(item["phase"] == "reader" for item in state["errors"]))

    def test_invalid_plan_schema_is_preserved_for_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(Path(temp_dir), research_max_rounds=1)
            with self.assertRaisesRegex(ValueError, "planning JSON"):
                run_deep_research(
                    "invalid response test",
                    cfg,
                    object(),
                    None,
                    job_id="job",
                    search_fn=lambda *args, **kwargs: ([], "fixture"),
                    chat_fn=lambda *args, **kwargs: '{"contract": [], "tasks": []}',
                    depth="shallow",
                )
            run = list_research_runs("job", cfg)[0]
            self.assertEqual("failed", run["status"])
            self.assertEqual(1, run["model_calls"])
            self.assertTrue((Path(run["run_dir"]) / ".invalid-planning-response.txt").is_file())

    def test_citation_audit_rejects_unknown_ids_and_non_registry_urls(self) -> None:
        source = Source(
            id="S1",
            url="https://example.com/source",
            normalized_url="https://example.com/source",
            title="Source",
            provider="fixture",
            search_query="q",
            retrieved_at=now_iso(),
            content="This exact passage is sufficiently long for deterministic validation.",
            content_sha256="",
        )
        evidence = Evidence(
            id="EABCDEF123456",
            source_id="S1",
            task_id="T1",
            passage=source.content,
            locator="section",
            stance="supports",
            claim_hint="claim",
            relevance=1.0,
            credibility=0.8,
            extracted_at=now_iso(),
            passage_sha256="hash",
            verified_exact=True,
        )
        claim = Claim("C1", "Supported claim", [evidence.id], critical=True, confidence=0.9)
        report = (
            "This factual paragraph is long enough but cites an unknown record "
            "[E000000000000] and [bad](https://attacker.example/)."
        )
        audit = audit_report(report, [evidence], [source], [claim], used_claim_ids=["C1"])
        self.assertFalse(audit["passed"])
        self.assertEqual(["E000000000000"], audit["unknown_evidence_ids"])
        self.assertEqual(["https://attacker.example/"], audit["unsafe_urls"])
        self.assertEqual(["C1"], audit["uncited_claim_ids"])
        omitted = audit_report(
            f"Supported claim with a valid citation. [{evidence.id}]",
            [evidence],
            [source],
            [claim],
            used_claim_ids=[],
        )
        self.assertFalse(omitted["passed"])
        self.assertEqual(["C1"], omitted["omitted_critical_claim_ids"])
        self.assertTrue(verify_exact_passage(source.content, source.content).valid)
        self.assertFalse(verify_exact_passage(source.content, "a fabricated passage that is absent").valid)

    def test_generated_source_links_escape_markdown_parentheses(self) -> None:
        source = Source(
            id="S1",
            url="https://example.com/spec_(v2)",
            normalized_url="https://example.com/spec_(v2)",
            title="Specification (v2)",
            provider="fixture",
            search_query="q",
            retrieved_at=now_iso(),
            content="A sufficiently long exact passage supports the specification claim.",
            content_sha256="",
        )
        evidence = Evidence(
            id="EABCDEF123456",
            source_id=source.id,
            task_id="T1",
            passage=source.content,
            locator="section",
            stance="supports",
            claim_hint="claim",
            relevance=1.0,
            credibility=0.8,
            extracted_at=now_iso(),
            passage_sha256="hash",
            verified_exact=True,
        )
        claim = Claim("C1", "The specification claim is supported.", [evidence.id])
        report = (
            "The specification claim is supported by the exact saved passage in the source. "
            f"[{evidence.id}]"
        )
        first_audit = audit_report(
            report, [evidence], [source], [claim], used_claim_ids=[claim.id]
        )
        final = append_used_sources(report, first_audit, [evidence], [source])
        final_audit = audit_report(
            final, [evidence], [source], [claim], used_claim_ids=[claim.id]
        )
        self.assertIn("spec_%28v2%29", final)
        self.assertTrue(final_audit["passed"], final_audit)

    def test_study_link_skips_a_newer_failed_research_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(Path(temp_dir))
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
            (completed / "06-report.md").write_text("report", encoding="utf-8")
            (completed / "07-study-guide.md").write_text("guide", encoding="utf-8")
            sealed_state = SimpleNamespace(
                status="completed",
                citation_audit={"passed": True},
                claim_audit={"passed": True},
            )
            with (
                patch.object(ResearchStore, "load", return_value=sealed_state),
                patch.object(ResearchStore, "manifest_failures", return_value=[]),
            ):
                selected = latest_completed_research_run("research-job", cfg)
            self.assertEqual("completed", selected["run_id"])

    def test_explicit_intents_do_not_need_an_llm_parser(self) -> None:
        cfg = BuildupConfig()
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
        self.assertEqual(_render_banner_word("WEDNESDAY"), _ascii_banner_lines(wednesday))
        self.assertEqual(6, len(_ascii_banner_lines(wednesday)))
        self.assertEqual(6, len(_render_banner_word("BUILD-UP")))
        self.assertTrue(any("█" in line for line in _render_banner_word("BUILD-UP")))

    def test_interactive_shell_starts_in_research(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _cfg(
                Path(temp_dir),
                research_model="runtime-research-model",
                reviewer_model="runtime-reviewer-model",
                search_provider="tavily",
            )
            shell = InteractiveShell(cfg, object(), None)
            self.assertEqual("research", shell.assistant_mode)
            facts = dict(shell._header_runtime_info())
            self.assertEqual("runtime-research-model", facts["model"])
            self.assertEqual("runtime-reviewer-model · standby", facts["reviewer"])
            self.assertEqual("tavily", facts["search"])

    def test_header_and_prompt_use_public_brand(self) -> None:
        rendered = "\n".join(
            line.plain for line in _build_header_lines(
                "auto",
                None,
                assistant_mode="research",
                runtime_info=(("model", "runtime-model"), ("search", "google")),
                now=datetime(2026, 8, 12, 9, 30),
            )
        )
        self.assertIn("build-up", rendered)
        self.assertRegex(rendered, r"assistant\s+research")
        self.assertIn("runtime-model", rendered)
        self.assertIn("2026-08-12 09:30:00", rendered)

        prompt = build_prompt("auto", "hidden-job", assistant_mode="research")
        self.assertRegex(prompt.plain, r"^build-up  research  \d{2}:\d{2}  > $")
        self.assertNotIn("hidden-job", prompt.plain)
        explicit = build_prompt(
            "main",
            "paper-review",
            assistant_mode="research/reviewer+steer:critical",
        )
        self.assertIn("build-up  research/reviewer/critical / main", explicit.plain)

    def test_prompt_is_owned_by_readline_and_protects_styled_prefix(self) -> None:
        prompt = build_prompt("auto", None, assistant_mode="research")
        color_console = Console(
            file=StringIO(),
            force_terminal=True,
            color_system="standard",
            no_color=False,
        )
        with (
            patch("buildup.rendering.console", color_console),
            patch("builtins.input", return_value="hello") as mocked_input,
        ):
            self.assertEqual("hello", read_prompt(prompt))
        readline_prompt = mocked_input.call_args.args[0]
        self.assertEqual(prompt.plain, re.sub(r"\x01\x1b\[[0-9;]*m\x02", "", readline_prompt))
        self.assertIn("\x01\x1b[", readline_prompt)

    def test_research_search_requests_tavily_advanced_raw_content(self) -> None:
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": [{
                    "title": "Source",
                    "url": "https://example.test/source",
                    "content": "snippet",
                    "raw_content": "full page",
                    "score": 0.9,
                }]}

        class Session:
            def __init__(self):
                self.payloads = []

            def post(self, url, json, headers, timeout):
                self.payloads.append(json)
                return Response()

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(
                base_dir=Path(temp_dir),
                tavily_api_key="test-key",
                search_provider="tavily",
            )
            session = Session()
            results, engine = research_search("agent harness", cfg, session, max_results=3)
        self.assertEqual("tavily", engine)
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
            BuildupConfig(),
            "test-model",
            [{"role": "user", "content": "ok"}],
            json_schema=schema,
            sanitize_thinking=False,
        )
        self.assertEqual('{"ok": true}', result)
        self.assertEqual(schema, session.payloads[0]["format"])


if __name__ == "__main__":
    unittest.main()
