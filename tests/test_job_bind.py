"""Bound-directory reach and its sandbox boundaries."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buildup.config import load_config
from buildup.jobs import (
    bind_job_path,
    cmd_job_new,
    cmd_read,
    cmd_write,
    job_binds,
    unbind_job_path,
)
from buildup.sandbox import SandboxViolation

Blocked = (SandboxViolation, ValueError)


class JobBindTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.real_dir = root / "real"
        self.real_dir.mkdir()
        (self.real_dir / "note.md").write_text("hello from real dir", encoding="utf-8")
        self._env = patch.dict(os.environ, {"BUILDUP_BASE_DIR": str(root / "base")})
        self._env.start()
        self.cfg = load_config()
        self.job_id, _ = cmd_job_new("bindtest", self.cfg, None)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_read_only_bind_reads_but_refuses_writes(self) -> None:
        bind_job_path(self.job_id, str(self.real_dir), self.cfg)
        self.assertIn("hello from real dir", cmd_read(self.job_id, "note.md", self.cfg))
        with self.assertRaises(Blocked):
            cmd_write(self.job_id, str(self.real_dir / "blocked.md"), "x", self.cfg)
        self.assertFalse((self.real_dir / "blocked.md").exists())

    def test_writable_bind_allows_writes(self) -> None:
        bind_job_path(self.job_id, str(self.real_dir), self.cfg, writable=True)
        cmd_write(self.job_id, str(self.real_dir / "allowed.md"), "written", self.cfg)
        self.assertEqual("written", (self.real_dir / "allowed.md").read_text(encoding="utf-8"))

    def test_writes_outside_every_root_are_blocked(self) -> None:
        bind_job_path(self.job_id, str(self.real_dir), self.cfg, writable=True)
        for target in (
            Path(self._tmp.name) / "escape.md",
            self.real_dir / ".." / ".." / "escape.md",
        ):
            with self.subTest(target=target):
                with self.assertRaises(Blocked):
                    cmd_write(self.job_id, str(target), "x", self.cfg)

    def test_unbind_removes_reach(self) -> None:
        bind_job_path(self.job_id, str(self.real_dir), self.cfg)
        self.assertTrue(unbind_job_path(self.job_id, str(self.real_dir), self.cfg))
        self.assertEqual([], job_binds(self.job_id, self.cfg))
        with self.assertRaises(Exception):
            cmd_read(self.job_id, "note.md", self.cfg)

    def test_data_root_and_home_cannot_be_bound(self) -> None:
        for target in (self.cfg.base_dir, Path.home()):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    bind_job_path(self.job_id, str(target), self.cfg)

    def test_rebinding_updates_the_write_flag(self) -> None:
        bind_job_path(self.job_id, str(self.real_dir), self.cfg)
        self.assertFalse(job_binds(self.job_id, self.cfg)[0].writable)
        bind_job_path(self.job_id, str(self.real_dir), self.cfg, writable=True)
        binds = job_binds(self.job_id, self.cfg)
        self.assertEqual(1, len(binds))
        self.assertTrue(binds[0].writable)


if __name__ == "__main__":
    unittest.main()
