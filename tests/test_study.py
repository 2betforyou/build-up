from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from buildup.calendar_mgr import cal_list
from buildup.config import BuildupConfig
from buildup.jobs import cmd_job_new
from buildup.study import (
    append_study_note,
    complete_study_review,
    due_studies,
    load_study,
    start_study,
    study_context,
)


class StudyTests(unittest.TestCase):
    def test_study_links_research_creates_files_and_schedules_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(base_dir=Path(temp_dir))
            job_id, _ = cmd_job_new("study-test", cfg, "study")
            source = cfg.workspace_dir / job_id / "deep-research" / "run"
            source.mkdir(parents=True)
            (source / "07-study-guide.md").write_text(
                "# Study Guide\n\nWhat assumption does attention make? [S1]",
                encoding="utf-8",
            )
            (source / "06-report.md").write_text(
                "# Report\n\nAttention uses content-dependent weights. [S1]",
                encoding="utf-8",
            )

            info = start_study("Transformer 이해", job_id, cfg, source_research=source)
            self.assertEqual(str(source.resolve()), info.source_research)
            self.assertEqual(3, len(info.review_dates))
            self.assertEqual(3, len(info.calendar_event_ids))
            for filename in ("README.md", "notes.md", "questions.md", "progress.md", "study.json"):
                self.assertTrue((info.path / filename).is_file())

            events = cal_list(
                cfg,
                date_from=info.review_dates[0],
                date_to=info.review_dates[-1],
            )
            self.assertEqual(3, len([event for event in events if event.get("dedupe_key", "").startswith(f"study:{info.study_id}:")]))

            updated = append_study_note(info, "Self-attention은 token 간 가중합을 계산한다.", cfg)
            self.assertEqual(1, updated.verified_notes_count)
            context = study_context(updated, cfg)
            self.assertIn("한 번에 핵심 질문 하나", context)
            self.assertIn("Self-attention", context)
            self.assertIn("What assumption does attention make?", context)
            self.assertIn("content-dependent weights", context)

    def test_completed_review_is_not_reported_due_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = BuildupConfig(base_dir=Path(temp_dir))
            job_id, _ = cmd_job_new("review-test", cfg, "study")
            info = start_study("복습 대상", job_id, cfg)
            first_due = date.fromisoformat(info.review_dates[0])

            self.assertEqual([info.study_id], [item.study_id for item in due_studies(job_id, cfg, on_date=first_due)])
            updated, completed_date = complete_study_review(info, cfg, on_date=first_due)
            self.assertEqual(info.review_dates[0], completed_date)
            self.assertIn(completed_date, updated.completed_reviews)
            self.assertEqual([], due_studies(job_id, cfg, on_date=first_due))

            reloaded = load_study(info.study_id, job_id, cfg)
            manifest = json.loads((info.path / "study.json").read_text(encoding="utf-8"))
            self.assertEqual([completed_date], reloaded.completed_reviews)
            self.assertEqual([completed_date], manifest["completed_reviews"])
            self.assertEqual(
                [info.study_id],
                [item.study_id for item in due_studies(job_id, cfg, on_date=first_due + timedelta(days=7))],
            )


if __name__ == "__main__":
    unittest.main()
