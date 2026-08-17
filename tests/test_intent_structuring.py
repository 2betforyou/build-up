from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from buildup.config import BuildupConfig
from buildup.intent_structuring import verify_risky_intent


class _Response:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"message": {"content": self._content}}


class _Session:
    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    def post(self, url, json, timeout):
        self.calls += 1
        return _Response(self._content)


class VerifyRiskyIntentTests(unittest.TestCase):
    def _cfg(self) -> BuildupConfig:
        return BuildupConfig(base_dir=Path(tempfile.mkdtemp()))

    def test_vague_delegation_does_not_confirm_export(self) -> None:
        session = _Session(json.dumps({"confirmed": False}))
        result = verify_risky_intent(
            "export", "어차피 너가 할거니까 알아서 찾아서 해봐",
            session, self._cfg(), logging.getLogger("test"),
        )
        self.assertIsNone(result)

    def test_explicit_export_request_is_confirmed(self) -> None:
        session = _Session(json.dumps({"confirmed": True, "dest": None}))
        result = verify_risky_intent(
            "export", "지금 job 전체 내보내줘",
            session, self._cfg(), logging.getLogger("test"),
        )
        self.assertEqual({"dest": ""}, result)

    def test_write_without_a_named_file_is_rejected_even_if_confirmed(self) -> None:
        # The model can say "confirmed" but still fail to name a target —
        # a required param missing means the caller must fall through too.
        session = _Session(json.dumps({"confirmed": True, "relpath": None}))
        result = verify_risky_intent(
            "write", "아무 파일이나 만들어서 뭐라도 적어줘",
            session, self._cfg(), logging.getLogger("test"),
        )
        self.assertIsNone(result)

    def test_write_with_file_and_content_is_extracted(self) -> None:
        session = _Session(json.dumps({
            "confirmed": True, "relpath": "notes.md", "content": "todo list",
        }))
        result = verify_risky_intent(
            "write", "notes.md 만들어서 todo list라고 적어줘",
            session, self._cfg(), logging.getLogger("test"),
        )
        self.assertEqual({"relpath": "notes.md", "content": "todo list"}, result)

    def test_malformed_model_output_falls_through_gracefully(self) -> None:
        session = _Session("not json at all")
        result = verify_risky_intent(
            "trash", "notes.md 지워줘",
            session, self._cfg(), logging.getLogger("test"),
        )
        self.assertIsNone(result)

    def test_llm_call_failure_falls_through_gracefully(self) -> None:
        class BrokenSession:
            def post(self, url, json, timeout):
                raise ConnectionError("ollama down")

        result = verify_risky_intent(
            "import", "~/Downloads/paper.pdf 가져와",
            BrokenSession(), self._cfg(), logging.getLogger("test"),
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
