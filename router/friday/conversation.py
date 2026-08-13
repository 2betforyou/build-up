"""Multi-turn conversation history manager."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


class ConversationHistory:
    """Manages multi-turn chat history within a session."""

    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        # Keep the complete durable transcript. ``max_turns`` limits only the
        # prompt window sent to the model; it must never destroy session data.
        self._messages: List[Dict[str, Any]] = []
        self._context_summary = ""
        # Optional hook called after every add() — used for auto-save
        self._on_change: Optional[Callable[[], None]] = None

    def set_on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked after every message is added."""
        self._on_change = callback

    def add(self, role: str, content: str, **metadata: Any) -> None:
        message: Dict[str, Any] = {
            "role": role,
            "content": content,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        message.update(metadata)
        self._messages.append(message)
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass  # auto-save failures must never crash Friday

    def get_messages(self, system_prompt: str) -> List[Dict[str, str]]:
        """Return the bounded active context, leaving the transcript intact."""
        prompt_messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]
        if self._context_summary:
            prompt_messages.append({
                "role": "system",
                "content": "[earlier conversation summary]\n" + self._context_summary,
            })
        window = self._messages[-(self.max_turns * 2):]
        for message in window:
            role = str(message.get("role") or "assistant")
            # Ollama chat accepts the conversational roles here. Tool metadata
            # remains durable in SQLite but is not blindly replayed as schema.
            if role not in {"user", "assistant", "system", "tool"}:
                role = "assistant"
            prompt_messages.append({
                "role": role,
                "content": str(message.get("content") or ""),
            })
        return prompt_messages

    def load_messages(self, messages: List[Dict[str, Any]]) -> None:
        """Replace current messages (used when restoring a saved session)."""
        self._messages = [
            dict(message) for message in messages
            if isinstance(message, dict) and message.get("role") and "content" in message
        ]

    def raw_messages(self) -> List[Dict[str, Any]]:
        """Return a copy of the raw message list (for serialization)."""
        return [dict(message) for message in self._messages]

    def clear(self) -> None:
        self._messages.clear()
        self._context_summary = ""

    @property
    def context_summary(self) -> str:
        return self._context_summary

    def set_context_summary(self, summary: str) -> None:
        self._context_summary = summary.strip()

    def pop_last_turn(self) -> Optional[str]:
        """Remove the last user turn and everything after it; return its text."""
        user_index: Optional[int] = None
        for index in range(len(self._messages) - 1, -1, -1):
            if self._messages[index].get("role") == "user":
                user_index = index
                break
        if user_index is None:
            return None
        user_text = str(self._messages[user_index].get("content") or "")
        del self._messages[user_index:]
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass
        return user_text

    @property
    def turn_count(self) -> int:
        return sum(1 for m in self._messages if m["role"] == "user")

    def summary_text(self) -> str:
        """Return a compact text representation of the conversation."""
        lines: List[str] = []
        for m in self._messages:
            tag = "User" if m["role"] == "user" else "build-up"
            preview = m["content"][:120].replace("\n", " ")
            lines.append(f"[{tag}] {preview}")
        return "\n".join(lines)
