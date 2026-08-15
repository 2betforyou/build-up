from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from buildup.config import BuildupConfig
from buildup.deep_research import run_deep_research
from buildup.intent import parse_intent_instant
from buildup.knowledge import (
    add_research_to_vault,
    bind_job_to_vault,
    lint_vault,
    query_vault,
    rollback_proposal,
    verify_claim,
    vault_summary,
)
from buildup.research.models import stable_id
from buildup.research.providers import NoSearchProviderConfigured, SearchBroker


def _config(base: Path, **overrides) -> BuildupConfig:
    values = {
        "base_dir": base,
        "research_model": "research-model",
        "reviewer_model": "reviewer-model",
        "research_max_rounds": 1,
        "research_max_tasks": 4,
        "research_max_searches": 8,
        "research_max_sources": 8,
        "research_max_model_calls": 10,
    }
    values.update(overrides)
    return BuildupConfig(**values)


def _completed_run(
    cfg: BuildupConfig,
    *,
    job_id: str,
    url: str,
    passage: str,
    claim_text: str,
    now: datetime,
    contradicting_passage: str = "",
    ingest_knowledge=None,
):
    source_id = stable_id("S", url)
    evidence_id = stable_id("E", source_id, passage, length=14)
    contradiction_id = (
        stable_id("E", source_id, contradicting_passage, length=14)
        if contradicting_passage else ""
    )
    claim_id = stable_id("C", claim_text, length=14)
    evidence_rows = [{
        "source_id": source_id,
        "passage": passage,
        "locator": "primary passage",
        "stance": "supports",
        "claim_hint": claim_text,
        "relevance": 1.0,
        "credibility": 0.9,
        "notes": "",
    }]
    if contradicting_passage:
        evidence_rows.append({
            "source_id": source_id,
            "passage": contradicting_passage,
            "locator": "counter passage",
            "stance": "contradicts",
            "claim_hint": claim_text,
            "relevance": 0.9,
            "credibility": 0.8,
            "notes": "preserve the unresolved conflict",
        })
    payloads = [
        {
            "contract": {
                "question": claim_text,
                "objective": "Test grounded compounding knowledge.",
                "audience": "developers",
                "deliverable": "cited report",
                "subquestions": [claim_text],
                "in_scope": ["knowledge vault"],
                "out_of_scope": [],
                "source_requirements": ["exact source passage"],
                "success_criteria": ["citation audit passes"],
                "freshness": "current where relevant",
                "assumptions": [],
            },
            "tasks": [{
                "question": claim_text,
                "queries": [claim_text],
                "rationale": "ground the claim",
                "priority": 5,
                "critical": True,
            }],
        },
        {"evidence": evidence_rows, "unanswered": []},
        {
            "coverage": 0.96,
            "sufficient": True,
            "stop_reason": "critical claim covered",
            "claims": [{
                "text": claim_text,
                "evidence_ids": [evidence_id],
                "contradicting_evidence_ids": [contradiction_id]
                if contradiction_id else [],
                "confidence": 0.9,
                "critical": True,
                "status": "contested" if contradiction_id else "supported",
            }],
            "gaps": [{
                "question": "What would change this conclusion?",
                "reason": "future evidence may differ",
                "importance": 3,
                "suggested_queries": ["new contrary evidence"],
            }],
        },
        {
            "report": f"# Result\n\n{claim_text} [{evidence_id}]",
            "used_claim_ids": [claim_id],
        },
        {
            "overall_passed": True,
            "summary": "The supporting passage directly entails the claim.",
            "claim_results": [{
                "claim_id": claim_id,
                "supported": True,
                "evidence_ids": [evidence_id],
                "reason": "Direct support.",
            }],
        },
    ]

    def fake_chat(*args, **kwargs):
        return json.dumps(payloads.pop(0), ensure_ascii=False)

    def fake_search(*args, **kwargs):
        content = " ".join(filter(None, (
            passage,
            contradicting_passage,
            "Additional exact primary-source context keeps this document long enough for ingestion.",
        )))
        return [{
            "title": "Grounded primary source",
            "href": url,
            "body": passage,
            "raw_content": content,
            "metadata": {"doi": "https://doi.org/10.0000/example"},
        }], "fixture"

    result = run_deep_research(
        claim_text,
        cfg,
        object(),
        None,
        job_id=job_id,
        search_fn=fake_search,
        chat_fn=fake_chat,
        depth="shallow",
        now=now,
        ingest_knowledge=ingest_knowledge,
    )
    return result, claim_id


class KnowledgeVaultTests(unittest.TestCase):
    def test_wiki_is_optional_and_derivative_failure_cannot_fail_research(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _config(Path(temp_dir))
            skipped, _ = _completed_run(
                cfg,
                job_id="optional-job",
                url="https://example.com/no-wiki",
                passage="This completed research remains valid without a derived Wiki view.",
                claim_text="Research remains valid when Wiki ingestion is disabled.",
                now=datetime(2026, 8, 15, 9, 0, 0),
                ingest_knowledge=False,
            )
            self.assertIsNone(skipped.knowledge_vault_path)
            self.assertEqual("", skipped.knowledge_error)

            with patch(
                "buildup.knowledge.auto_ingest_research_run",
                side_effect=RuntimeError("derived view unavailable"),
            ):
                completed, _ = _completed_run(
                    cfg,
                    job_id="optional-job",
                    url="https://example.com/wiki-failure",
                    passage="A Wiki failure must not invalidate this sealed completed research run.",
                    claim_text="A derived Wiki failure does not invalidate sealed research.",
                    now=datetime(2026, 8, 15, 9, 30, 0),
                )
            self.assertIn("# Result", completed.report)
            self.assertIn("derived view unavailable", completed.knowledge_error)

    def test_research_auto_ingests_compiles_queries_verifies_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _config(Path(temp_dir))
            result, claim_id = _completed_run(
                cfg,
                job_id="knowledge-job",
                url="https://example.com/grounded",
                passage=(
                    "A sealed source passage supports deterministic append-only knowledge ingestion."
                ),
                claim_text="Append-only knowledge ingestion is grounded in sealed sources.",
                now=datetime(2026, 8, 15, 10, 0, 0),
            )

            self.assertIsNotNone(result.knowledge_vault_path)
            self.assertEqual(1, result.knowledge_claims_added)
            self.assertEqual(0, result.knowledge_invalid)
            vault = result.knowledge_vault_path
            assert vault is not None
            self.assertTrue((vault / "schema.yaml").is_file())
            self.assertTrue((vault / "sources.jsonl").is_file())
            self.assertTrue((vault / "claims.jsonl").is_file())
            self.assertTrue((vault / "log.jsonl").is_file())
            self.assertTrue((vault / "proposals" / f"{result.knowledge_proposal_id}.json").is_file())
            for category in (
                "concepts", "methods", "models", "datasets", "papers", "claims",
                "contradictions", "open-questions", "synthesis",
            ):
                self.assertTrue((vault / "pages" / category).is_dir())
            claim_page = (vault / "pages" / "claims" / f"{claim_id}.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("source hash", claim_page)
            self.assertIn("artifact:", claim_page)
            self.assertIn("https://example.com/grounded", claim_page)

            hits = query_vault("append-only knowledge", "knowledge-job", cfg)
            self.assertEqual(claim_id, hits[0].claim_id)
            self.assertEqual("supported", hits[0].status)
            self.assertTrue(hits[0].citations[0]["artifact_path"].endswith("03-sources.jsonl"))
            self.assertTrue(lint_vault("knowledge-job", cfg).passed)

            verified = verify_claim(
                claim_id, "knowledge-job", cfg, via="study:test-session"
            )
            self.assertEqual("verified", verified["status"])
            rollback_proposal(result.knowledge_proposal_id, "knowledge-job", cfg)
            summary = vault_summary("knowledge-job", cfg)
            self.assertEqual(0, sum(summary.claims_by_status.values()))
            events = (vault / "claims.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "claim_upsert"', events)
            self.assertIn('"event": "proposal_rollback"', events)
            restored = add_research_to_vault("latest", "knowledge-job", cfg)
            self.assertFalse(restored.duplicate)
            restored_hits = query_vault("append-only knowledge", "knowledge-job", cfg)
            self.assertEqual("verified", restored_hits[0].status)
            events = (vault / "claims.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "proposal_restore"', events)

    def test_conflicts_are_preserved_and_changed_source_stales_old_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _config(Path(temp_dir))
            first, first_claim = _completed_run(
                cfg,
                job_id="evolving-job",
                url="https://example.com/evolving",
                passage="Version one says the original alpha conclusion remains applicable.",
                claim_text="The original alpha conclusion remains applicable.",
                now=datetime(2026, 8, 15, 11, 0, 0),
            )
            second, second_claim = _completed_run(
                cfg,
                job_id="evolving-job",
                url="https://example.com/evolving",
                passage="Version two supports a materially different beta conclusion instead.",
                claim_text="A materially different beta conclusion now applies.",
                now=datetime(2026, 8, 15, 12, 0, 0),
            )
            self.assertNotEqual(first.knowledge_proposal_id, second.knowledge_proposal_id)
            first_hits = query_vault(
                "original alpha conclusion", "evolving-job", cfg
            )
            second_hits = query_vault(
                "different beta conclusion", "evolving-job", cfg
            )
            self.assertEqual(first_claim, first_hits[0].claim_id)
            self.assertEqual("stale", first_hits[0].status)
            self.assertEqual(second_claim, second_hits[0].claim_id)
            self.assertEqual("supported", second_hits[0].status)

            conflict, conflict_claim = _completed_run(
                cfg,
                job_id="evolving-job",
                url="https://example.com/conflict",
                passage="The controlled trial reports a positive effect in the measured cohort.",
                contradicting_passage=(
                    "A separate analysis in the same source reports no effect under a second protocol."
                ),
                claim_text="The intervention has a positive measured effect.",
                now=datetime(2026, 8, 15, 13, 0, 0),
            )
            conflict_hits = query_vault("positive measured effect", "evolving-job", cfg)
            self.assertEqual("disputed", conflict_hits[0].status)
            assert conflict.knowledge_vault_path is not None
            conflict_page = (
                conflict.knowledge_vault_path
                / "pages" / "contradictions" / f"{conflict_claim}.md"
            ).read_text(encoding="utf-8")
            self.assertIn("no automatic winner", conflict_page)
            self.assertIn("Counter-evidence", conflict_page)

    def test_lint_detects_proposal_and_sealed_source_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _config(Path(temp_dir))
            result, claim_id = _completed_run(
                cfg,
                job_id="tamper-job",
                url="https://example.com/tamper-test",
                passage="This exact source sentence anchors the immutable knowledge proposal.",
                claim_text="Immutable proposals remain anchored to sealed source artifacts.",
                now=datetime(2026, 8, 15, 14, 0, 0),
            )
            assert result.knowledge_vault_path is not None
            proposal_path = (
                result.knowledge_vault_path
                / "proposals"
                / f"{result.knowledge_proposal_id}.json"
            )
            original_proposal = proposal_path.read_text(encoding="utf-8")
            proposal = json.loads(original_proposal)
            proposal["synthesis"]["objective"] = "tampered objective"
            proposal_path.write_text(
                json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            proposal_report = lint_vault(
                "tamper-job", cfg, repair_stale=False
            )
            self.assertFalse(proposal_report.passed)
            self.assertTrue(any(
                "immutable proposal hash mismatch" in item
                for item in proposal_report.errors
            ))
            self.assertEqual([], query_vault("immutable proposals", "tamper-job", cfg))
            proposal_path.write_text(original_proposal, encoding="utf-8")

            source_ledger = result.knowledge_vault_path / "sources.jsonl"
            original_ledger = source_ledger.read_text(encoding="utf-8")
            ledger_rows = [
                json.loads(line) for line in original_ledger.splitlines() if line.strip()
            ]
            ledger_rows[0]["title"] = "tampered source registry title"
            source_ledger.write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    for row in ledger_rows
                ),
                encoding="utf-8",
            )
            self.assertEqual([], query_vault("immutable proposals", "tamper-job", cfg))
            source_ledger.write_text(original_ledger, encoding="utf-8")

            source_path = result.run_dir / "03-sources.jsonl"
            source_rows = [
                json.loads(line)
                for line in source_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            source_rows[0]["content"] = "The sealed source was changed after ingestion."
            source_path.write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                    for row in source_rows
                ),
                encoding="utf-8",
            )
            source_report = lint_vault("tamper-job", cfg)
            self.assertFalse(source_report.passed)
            self.assertEqual(1, source_report.repaired_stale)
            self.assertEqual(1, source_report.stats["stale"])
            self.assertEqual(0, source_report.stats["supported"])
            self.assertTrue(any(
                "source content/hash changed" in item
                or "sealed research manifest failed" in item
                for item in source_report.errors
            ))
            hits = query_vault("immutable proposals", "tamper-job", cfg)
            self.assertEqual(claim_id, hits[0].claim_id)
            self.assertEqual("stale", hits[0].status)

    def test_cross_vault_binding_is_explicit_and_wiki_pages_cannot_be_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _config(Path(temp_dir))
            (cfg.workspace_dir / "job-one").mkdir(parents=True)
            (cfg.workspace_dir / "job-two").mkdir(parents=True)
            with self.assertRaises(PermissionError):
                bind_job_to_vault("job-one", "shared-vault", cfg)
            store = bind_job_to_vault(
                "job-one", "shared-vault", cfg, confirmed=True
            )
            second = bind_job_to_vault(
                "job-two", "shared-vault", cfg, confirmed=True
            )
            self.assertEqual(store.root, second.root)
            self.assertTrue(lint_vault("job-one", cfg).passed)
            content_hash = hashlib.sha256(b"not evidence").hexdigest()
            source_id = stable_id(
                "KS",
                "job-one",
                "invalid-run",
                "SINVALID",
                "https://example.com/invalid",
                content_hash,
                length=18,
            )
            with store.lease():
                store.append_sources(({
                    "schema_version": 1,
                    "source_version_id": source_id,
                    "research_source_id": "SINVALID",
                    "proposal_id": "KPINVALID",
                    "run_id": "invalid-run",
                    "job_id": "job-one",
                    "title": "Invalid Wiki source",
                    "url": "https://example.com/invalid",
                    "normalized_url": "https://example.com/invalid",
                    "artifact_path": str(store.root / "index.md"),
                    "manifest_path": str(store.root / "manifest.json"),
                    "source_type": "wiki",
                    "provider": "wiki",
                    "content_sha256": content_hash,
                },))
            report = lint_vault("job-one", cfg, repair_stale=False)
            self.assertFalse(report.passed)
            self.assertTrue(any("wiki page cannot be an evidence" in item for item in report.errors))

    def test_natural_language_wiki_intents_are_deterministic(self) -> None:
        cfg = BuildupConfig()
        cases = {
            "이 리서치를 지식에 반영해줘": "wiki_add",
            "내가 MoE에 대해 지금까지 뭘 알고 있지?": "wiki_ask",
            "위키 상태 점검해줘": "wiki_lint",
            "충돌하는 주장만 보여줘": "wiki_review",
            "C0123456789ABCD 주장은 내가 확인했어": "wiki_verify",
            "C0123456789ABCD 주장은 틀렸어": "wiki_reject",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                intent = parse_intent_instant(text, cfg)
                self.assertIsNotNone(intent)
                self.assertEqual(expected, intent["intent"])


class OpenAlexProviderTests(unittest.TestCase):
    def test_openalex_is_keyed_discovery_and_never_raw_evidence(self) -> None:
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "meta": {"count": 1},
                    "results": [{
                        "id": "https://openalex.org/W123",
                        "doi": "https://doi.org/10.1000/test",
                        "display_name": "A Grounded Paper",
                        "publication_year": 2026,
                        "publication_date": "2026-01-02",
                        "type": "article",
                        "cited_by_count": 7,
                        "is_retracted": False,
                        "authorships": [{"author": {"display_name": "Ada Researcher"}}],
                        "primary_location": {
                            "landing_page_url": "https://journal.example/paper"
                        },
                        "best_oa_location": {
                            "pdf_url": "https://journal.example/paper.pdf"
                        },
                        "open_access": {"is_oa": True},
                        "abstract_inverted_index": {
                            "Grounded": [0], "metadata": [1], "only": [2]
                        },
                        "relevance_score": 42.0,
                    }],
                }

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = _config(
                Path(temp_dir),
                search_provider="openalex",
                openalex_api_key="free-tier-key",
                search_cache_ttl_hours=0,
            )
            session = Session()
            response = SearchBroker(cfg, session=session).search(
                "grounded paper", max_results=3
            )
            self.assertEqual("openalex", response.provider)
            self.assertEqual("https://journal.example/paper.pdf", response.results[0].url)
            self.assertEqual("", response.results[0].raw_content)
            self.assertEqual("Grounded metadata only", response.results[0].snippet)
            self.assertEqual("https://openalex.org/W123", response.results[0].metadata["openalex_id"])
            self.assertEqual("free-tier-key", session.calls[0][1]["params"]["api_key"])

            auto_cfg = _config(
                Path(temp_dir) / "auto",
                search_provider="auto",
                openalex_api_key="free-tier-key",
                brave_search_api_key="brave-key",
            )
            auto_broker = SearchBroker(auto_cfg, session=Session())
            self.assertEqual(
                "openalex", auto_broker._providers("최신 학술 논문")[0].name
            )
            self.assertEqual(
                "brave", auto_broker._providers("latest product news")[0].name
            )

            no_key = _config(
                Path(temp_dir) / "no-key",
                search_provider="openalex",
                search_cache_ttl_hours=0,
            )
            with self.assertRaises(NoSearchProviderConfigured):
                SearchBroker(no_key, session=Session()).search("paper")
