from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from buildup.config import BuildupConfig
from buildup.jobs import (
    cmd_job_new,
    cmd_job_rename,
    format_job_label,
    job_display_name,
)
from buildup.rendering import render_previous_conversation
from buildup.session_store import (
    SessionInfo,
    list_sessions,
    load_session,
    rename_session,
    resolve_session,
    save_session,
)
from buildup.shell import InteractiveShell


class SessionUxTests(unittest.TestCase):
    def test_previous_conversation_card_has_aligned_roles_without_emoji(self) -> None:
        output = Console(record=True, width=100, color_system=None)
        messages = [
            {"role": "user", "content": "이 논문의 핵심 가정은 뭐야?"},
            {"role": "assistant", "content": "핵심 가정은 독립적인 평가 분포입니다."},
        ]

        with patch("buildup.rendering.console", output):
            render_previous_conversation(
                "논문 분석",
                messages,
                turn_count=1,
                updated_at="2026-08-13T19:30:00",
            )

        rendered = output.export_text()
        self.assertIn("previous conversation", rendered)
        self.assertIn("session", rendered)
        self.assertIn("you", rendered)
        self.assertIn("build-up", rendered)
        self.assertNotIn("●", rendered)
        self.assertNotIn("◆", rendered)

    def test_job_display_name_changes_context_without_changing_job_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(base_dir=Path(temp_dir))
            job_id, _ = cmd_job_new("todolist", cfg, "research")
            workspace = cfg.workspace_dir / job_id
            save_session(
                "stable-job-session",
                [{"role": "user", "content": "기존 연구"}],
                job_id,
                cfg,
            )

            display_name = cmd_job_rename(job_id, "Qwen 3.8 Research", cfg)

            self.assertEqual("Qwen 3.8 Research", display_name)
            self.assertEqual(display_name, job_display_name(job_id, cfg))
            self.assertIn(job_id, format_job_label(job_id, cfg))
            self.assertTrue(workspace.is_dir())
            session = load_session("stable-job-session", cfg)
            self.assertEqual(job_id, session.job_id)
            self.assertEqual(f"job:{job_id}", session.workspace_key)

            shell = InteractiveShell(cfg, object(), logging.getLogger("test-job-name"))
            self.assertEqual(display_name, shell._prompt_context_label())
            with self.assertRaises(ValueError):
                cmd_job_rename(job_id, "   ", cfg)

    def test_session_context_round_trip_and_number_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(base_dir=Path(temp_dir))
            messages = [
                {"role": "user", "content": "첫 질문"},
                {"role": "assistant", "content": "첫 답변"},
            ]
            save_session(
                "abc123",
                messages,
                None,
                cfg,
                assistant_mode="research",
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
            cfg = BuildupConfig(base_dir=Path(temp_dir))
            messages = [{"role": "user", "content": "자동 생성 제목"}]
            save_session("rename-me", messages, None, cfg)
            self.assertTrue(rename_session("rename-me", "내 연구 대화", cfg))
            save_session("rename-me", messages, None, cfg)
            self.assertEqual("내 연구 대화", load_session("rename-me", cfg).title)

    def test_legacy_session_defaults_to_isolated_research_context(self) -> None:
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
                BuildupConfig(base_dir=Path(temp_dir)),
                object(),
                logging.getLogger("test-session-ux"),
            )
            shell.assistant_mode = "daytime"
            shell.active_paper_id = "must-not-leak"
            shell.paper_reviewer_mode = True
            shell.steering.profile = "critical"

            shell._restore_session(info)

            self.assertEqual("research", shell.assistant_mode)
            self.assertIsNone(shell.active_paper_id)
            self.assertFalse(shell.paper_reviewer_mode)
            self.assertEqual("default", shell.steering.profile)
            shell._release_session_lease()

    def test_short_commands_and_natural_language_route_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shell = InteractiveShell(
                BuildupConfig(base_dir=Path(temp_dir)),
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
                BuildupConfig(base_dir=Path(temp_dir)),
                object(),
                logging.getLogger("test-session-reset"),
            )
            shell.history.load_messages([{"role": "user", "content": "기존 대화"}])
            shell.active_paper_id = "paper-old"
            shell.paper_reviewer_mode = True
            shell.steering.profile = "critical"

            with patch("buildup.shell.render_info"):
                shell._start_new_session()

            self.assertEqual(0, shell.history.turn_count)
            self.assertEqual("research", shell.assistant_mode)
            self.assertIsNone(shell.active_paper_id)
            self.assertFalse(shell.paper_reviewer_mode)
            self.assertEqual("default", shell.steering.profile)
            shell._release_session_lease()

    def test_retry_does_not_remove_a_research_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            shell = InteractiveShell(
                BuildupConfig(base_dir=Path(temp_dir)),
                object(),
                logging.getLogger("test-retry-research"),
            )
            shell.history.load_messages([
                {"role": "user", "content": "[deep research] attention"},
                {"role": "assistant", "content": "report"},
            ])
            with patch("buildup.shell.render_info") as render:
                shell._cmd_retry("/retry")
            self.assertEqual(2, len(shell.history.raw_messages()))
            self.assertIn("workflow", render.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
