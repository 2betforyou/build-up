from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from friday.config import FridayConfig
from friday.conversation_memory import append_conversation_memory, load_conversation_memory


class MemoryScopeTests(unittest.TestCase):
    def test_user_memory_is_shared_but_workspace_memory_does_not_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = FridayConfig(base_dir=Path(temp_dir))
            append_conversation_memory(
                cfg, "A에서 항상 A 규칙을 기억해", "A 전용 설정으로 반영했어",
                workspace_key="job:a", scope="workspace", manual=True,
            )
            append_conversation_memory(
                cfg, "B에서 항상 B 규칙을 기억해", "B 전용 설정으로 반영했어",
                workspace_key="job:b", scope="workspace", manual=True,
            )
            append_conversation_memory(
                cfg, "앞으로 한국어를 선호한다고 기억해", "사용자 설정으로 반영했어",
                workspace_key="job:a", scope="user", manual=True,
            )

            memory_a = load_conversation_memory(cfg, workspace_key="job:a")
            memory_b = load_conversation_memory(cfg, workspace_key="job:b")
            self.assertIn("A 규칙", memory_a)
            self.assertNotIn("B 규칙", memory_a)
            self.assertIn("B 규칙", memory_b)
            self.assertNotIn("A 규칙", memory_b)
            self.assertIn("한국어", memory_a)
            self.assertIn("한국어", memory_b)


if __name__ == "__main__":
    unittest.main()
