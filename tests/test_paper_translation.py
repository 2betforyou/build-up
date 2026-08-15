from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buildup.config import BuildupConfig
from buildup.paper import PaperPage
from buildup.paper_translation import TranslationTarget, translate_paper_pdf


class PaperTranslationTests(unittest.TestCase):
    def _target(self, root: Path) -> tuple[BuildupConfig, TranslationTarget]:
        cfg = BuildupConfig(base_dir=root, research_model="translation-model")
        job_id = "20260812-120000-paper"
        base = cfg.workspace_dir / job_id
        base.mkdir(parents=True)
        source = base / "paper.pdf"
        source.write_bytes(b"%PDF-test")
        return cfg, TranslationTarget(
            source_path=source,
            output_path=base / "paper.ko.md",
            job_id=job_id,
            library=False,
        )

    def test_translation_preserves_literals_and_separates_explanation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg, target = self._target(Path(temp_dir))
            pages = [
                PaperPage(1, "We optimize $L(x_i)$ following [12]."),
                PaperPage(2, "The LLM uses attention."),
            ]
            calls = []

            def fake_chat(session, cfg, model, messages, **kwargs):
                calls.append(messages)
                prompt = messages[-1]["content"]
                tokens = re.findall(r"\[\[\[BUILDUP_KEEP_\d{4}\]\]\]", prompt)
                return json.dumps({
                    "translation": "번역 " + " ".join(tokens) + " Machine learning LLM attention",
                    "explanation": "별도 핵심 설명",
                }, ensure_ascii=False)

            with patch("buildup.paper.extract_pdf_pages", return_value=pages):
                result = translate_paper_pdf(target, cfg, object(), None, chat_fn=fake_chat)

            self.assertEqual(1, result.model_calls)
            self.assertEqual(1, len(calls))
            output = target.output_path.read_text(encoding="utf-8")
            self.assertIn("### 번역", output)
            self.assertIn("### 핵심 의미", output)
            self.assertIn("$L(x_i)$", output)
            self.assertIn("[12]", output)
            self.assertNotIn("BUILDUP_KEEP", output)

    def test_long_translation_chunks_have_independent_histories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg, target = self._target(Path(temp_dir))
            pages = [
                PaperPage(1, "A" * 4_500),
                PaperPage(2, "B" * 4_500),
            ]
            calls = []

            def fake_chat(session, cfg, model, messages, **kwargs):
                calls.append(messages)
                prompt = messages[-1]["content"]
                tokens = re.findall(r"\[\[\[BUILDUP_KEEP_\d{4}\]\]\]", prompt)
                return json.dumps({
                    "translation": "번역 " + " ".join(tokens),
                    "explanation": "설명",
                }, ensure_ascii=False)

            with patch("buildup.paper.extract_pdf_pages", return_value=pages):
                result = translate_paper_pdf(
                    target,
                    cfg,
                    object(),
                    None,
                    chat_fn=fake_chat,
                    chunk_chars=4_000,
                )

            self.assertGreaterEqual(result.model_calls, 2)
            self.assertEqual(result.model_calls, len(calls))
            self.assertTrue(all(len(messages) == 2 for messages in calls))
            self.assertTrue(all(messages[0]["role"] == "system" for messages in calls))


if __name__ == "__main__":
    unittest.main()
