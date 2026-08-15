from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from buildup.config import BuildupConfig
from buildup.conversation import ConversationHistory
from buildup.session_store import (
    archive_session,
    export_session,
    list_sessions,
    load_session,
    rename_session,
    resolve_session,
    save_session,
    search_sessions,
    SessionLease,
)


class SessionStoreTests(unittest.TestCase):
    def test_session_lease_prevents_two_writers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(base_dir=Path(temp_dir))
            first = SessionLease(cfg, "shared")
            second = SessionLease(cfg, "shared")
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_full_transcript_is_preserved_while_prompt_window_is_bounded(self) -> None:
        history = ConversationHistory(max_turns=2)
        for index in range(5):
            history.add("user", f"question {index}")
            history.add("assistant", f"answer {index}")

        self.assertEqual(10, len(history.raw_messages()))
        prompt = history.get_messages("system")
        self.assertEqual(5, len(prompt))
        self.assertEqual("question 3", prompt[1]["content"])
        self.assertEqual("answer 4", prompt[-1]["content"])

    def test_sessions_are_workspace_scoped_searchable_and_archivable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(base_dir=Path(temp_dir))
            save_session(
                "alpha-session",
                [
                    {"role": "user", "content": "transformer positional encoding 조사"},
                    {"role": "assistant", "content": "근거를 정리했다", "kind": "research"},
                ],
                "alpha",
                cfg,
                workspace_key="job:alpha",
                context_summary="earlier research",
            )
            save_session(
                "beta-session",
                [{"role": "user", "content": "unrelated cooking notes"}],
                "beta",
                cfg,
                workspace_key="job:beta",
            )

            self.assertEqual(["alpha-session"], [item.session_id for item in list_sessions(cfg, workspace_key="job:alpha")])
            hits = search_sessions("positional", cfg, workspace_key="job:alpha")
            self.assertEqual("alpha-session", hits[0].session.session_id)
            self.assertEqual([], search_sessions("positional", cfg, workspace_key="job:beta"))

            self.assertTrue(rename_session("alpha-session", "Attention research", cfg))
            self.assertEqual("alpha-session", resolve_session("Attention research", cfg, workspace_key="job:alpha").session_id)
            exported = export_session("alpha-session", cfg, fmt="json")
            payload = json.loads(exported.read_text(encoding="utf-8"))
            self.assertEqual("earlier research", payload["context_summary"])
            self.assertEqual(2, len(payload["messages"]))

            self.assertTrue(archive_session("alpha-session", cfg))
            self.assertIsNone(resolve_session("alpha-session", cfg, workspace_key="job:alpha"))
            self.assertEqual([], list_sessions(cfg, workspace_key="job:alpha"))

    def test_legacy_json_is_migrated_once_and_kept_as_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy_dir = root / "sessions"
            legacy_dir.mkdir()
            legacy = legacy_dir / "legacy.session.json"
            legacy.write_text(
                json.dumps({
                    "session_id": "legacy",
                    "job_id": "paper-job",
                    "created_at": "2026-08-12T10:00:00+09:00",
                    "updated_at": "2026-08-12T10:01:00+09:00",
                    "title": "old conversation",
                    "messages": [{"role": "user", "content": "old research"}],
                }),
                encoding="utf-8",
            )
            cfg = BuildupConfig(base_dir=root)

            info = load_session("legacy", cfg)
            self.assertIsNotNone(info)
            self.assertEqual("job:paper-job", info.workspace_key)
            self.assertTrue(legacy.is_file())
            self.assertEqual(1, len(list_sessions(cfg, workspace_key="job:paper-job")))


if __name__ == "__main__":
    unittest.main()
