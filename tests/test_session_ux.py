from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from friday.config import FridayConfig
from friday.jobs import cmd_job_new
from friday.session_store import (
    SessionInfo,
    list_sessions,
    load_session,
    rename_session,
    resolve_session,
    save_session,
)
from friday.shell import InteractiveShell


class SessionUxTests(unittest.TestCase):
    def test_session_context_round_trip_and_number_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = FridayConfig(base_dir=Path(temp_dir))
            messages = [
                {"role": "user", "content": "첫 질문"},
                {"role": "assistant", "content": "첫 답변"},
            ]
            save_session(
                "abc123",
                messages,
                None,
                cfg,
                assistant_mode="nighttime",
                response_mode="main",
                active_paper_id="paper-1",
                paper_reviewer_mode=True,
                steering_profile="critical",
                steering_directives=["결론부터"],
            )

            info = load_session("abc123", cfg)
            self.assertIsNotNone(info)
            self.assertEqual("main", info.response_mode)
            self.assertEqual("paper-1", info.active_paper_id)
            self.assertTrue(info.paper_reviewer_mode)
            self.assertEqual("critical", info.steering_profile)
            self.assertEqual(["결론부터"], info.steering_directives)

            newest = list_sessions(cfg)[0]
            self.assertEqual(newest.session_id, resolve_session("1", cfg).session_id)

    def test_renamed_title_survives_later_autosave(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = FridayConfig(base_dir=Path(temp_dir))
            messages = [{"role": "user", "content": "자동 생성 제목"}]
            save_session("rename-me", messages, None, cfg)
            self.assertTrue(rename_session("rename-me", "내 연구 대화", cfg))
            save_session("rename-me", messages, None, cfg)
            self.assertEqual("내 연구 대화", load_session("rename-me", cfg).title)

    def test_legacy_session_defaults_to_isolated_night_context(self) -> None:
        info = SessionInfo(
            session_id="legacy",
            created_at="2026-08-12T10:00:00",
            updated_at="2026-08-12T10:00:00",
            job_id=None,
            title="legacy",
            turn_count=1,
            messages=[{"role": "user", "content": "이전 대화"}],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            shell = InteractiveShell(
                FridayConfig(base_dir=Path(temp_dir)),
                object(),
                logging.getLogger("test-session-ux"),
            )
            shell.assistant_mode = "daytime"
            shell.active_paper_id = "must-not-leak"
            shell.paper_reviewer_mode = True
            shell.steering.profile = "critical"

            shell._restore_session(info)

            self.assertEqual("nighttime", shell.assistant_mode)
            self.assertIsNone(shell.active_paper_id)
            self.assertFalse(shell.paper_reviewer_mode)
            self.assertEqual("default", shell.steering.profile)
            shell._release_session_lease()

    def test_short_commands_and_natural_language_route_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shell = InteractiveShell(
                FridayConfig(base_dir=Path(temp_dir)),
                object(),
                logging.getLogger("test-session-shortcuts"),
            )
            self.assertIn("/new", shell._commands)
            self.assertIn("/sessions", shell._commands)
            self.assertIn("/resume", shell._commands)

            with patch.object(shell, "_cmd_session") as command:
                self.assertTrue(shell._handle_session_shortcut("2번 대화 이어서"))
                command.assert_called_once_with("/session load 2")

            with patch.object(shell, "_cmd_session") as command:
                self.assertTrue(shell._handle_session_shortcut("대화 목록"))
                command.assert_called_once_with("/session list")

    def test_new_conversation_resets_session_scoped_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shell = InteractiveShell(
                FridayConfig(base_dir=Path(temp_dir)),
                object(),
                logging.getLogger("test-session-reset"),
            )
            shell.history.load_messages([{"role": "user", "content": "기존 대화"}])
            shell.active_paper_id = "paper-old"
            shell.paper_reviewer_mode = True
            shell.steering.profile = "critical"

            with patch("friday.shell.render_info"):
                shell._start_new_session()

            self.assertEqual(0, shell.history.turn_count)
            self.assertEqual("nighttime", shell.assistant_mode)
            self.assertIsNone(shell.active_paper_id)
            self.assertFalse(shell.paper_reviewer_mode)
            self.assertEqual("default", shell.steering.profile)
            shell._release_session_lease()

    def test_study_starts_in_a_new_session_but_keeps_the_same_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = FridayConfig(base_dir=Path(temp_dir))
            job_id, _ = cmd_job_new("research-to-study", cfg, "research")
            shell = InteractiveShell(cfg, object(), logging.getLogger("test-study-isolation"))
            shell._acquire_session_lease(shell._session_id)
            shell._session_kind = "research"
            shell.history.load_messages([
                {"role": "user", "content": "research question"},
                {"role": "assistant", "content": "research answer"},
            ])
            research_session_id = shell._session_id

            with patch("friday.shell.render_info"):
                shell._cmd_study("/study start attention mechanisms")

            self.assertNotEqual(research_session_id, shell._session_id)
            self.assertEqual(job_id, shell._session_job_id)
            self.assertEqual("study", shell._session_kind)
            self.assertEqual(0, shell.history.turn_count)
            self.assertIsNotNone(shell.active_study_id)
            sessions = list_sessions(cfg, workspace_key=f"job:{job_id}")
            self.assertEqual({"research", "study"}, {info.kind for info in sessions})
            study_session = next(info for info in sessions if info.kind == "study")
            self.assertEqual(shell.active_study_id, study_session.active_study_id)
            shell._release_session_lease()

    def test_retry_does_not_remove_a_research_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shell = InteractiveShell(
                FridayConfig(base_dir=Path(temp_dir)),
                object(),
                logging.getLogger("test-retry-research"),
            )
            shell.history.load_messages([
                {"role": "user", "content": "[deep research] attention"},
                {"role": "assistant", "content": "report"},
            ])
            with patch("friday.shell.render_info") as render:
                shell._cmd_retry("/retry")
            self.assertEqual(2, len(shell.history.raw_messages()))
            self.assertIn("workflow", render.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
