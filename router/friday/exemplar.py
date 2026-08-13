"""Exemplar retrieval from past execution traces.

At startup, loads successful traces from .friday_traces.jsonl and embeds
their user_input.  At query time, finds the nearest neighbours and returns
the top-k step sequences as context for the agent planner.

This is the "Exemplar Retrieval Layer" from the Technical Design doc (§5.3).
It does NOT change the agent loop — it enriches the system prompt context
so the agent sees "similar past tasks looked like this."

Design constraints
──────────────────
- Silently no-ops if traces file is empty or embed model unavailable.
- Only uses *successful* traces (success=True).
- Respects EXEMPLAR_MAX_TRACES to avoid unbounded memory usage.
- Re-indexes lazily: index is rebuilt when new traces are flushed and
  the caller asks for retrieval (via needs_rebuild flag).
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Tuple

import requests

from friday.config import FridayConfig


EXEMPLAR_MAX_TRACES = 500   # max traces kept in memory
EXEMPLAR_TOP_K = 3          # number of exemplars to inject
EXEMPLAR_MIN_SCORE = 0.78   # cosine similarity threshold


# ── Cosine similarity (no numpy) ─────────────────────────────

def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb + 1e-9)


# ── Embed helper (reuses Ollama embed API) ───────────────────

def _embed_one(text: str, session: requests.Session, cfg: FridayConfig) -> Optional[List[float]]:
    try:
        resp = session.post(
            cfg.ollama_embed_url,
            json={"model": cfg.embed_model, "input": [text]},
            timeout=15,
        )
        if resp.status_code == 404:
            # legacy endpoint
            legacy = cfg.ollama_embed_url.replace("/api/embed", "/api/embeddings")
            resp = session.post(legacy, json={"model": cfg.embed_model, "prompt": text}, timeout=15)
            resp.raise_for_status()
            return resp.json().get("embedding")
        resp.raise_for_status()
        embs = resp.json().get("embeddings", [])
        return embs[0] if embs else None
    except Exception:
        return None


# ── Trace loader ─────────────────────────────────────────────

def _load_successful_traces(cfg: FridayConfig) -> List[Dict[str, Any]]:
    path = cfg.base_dir / ".friday_traces.jsonl"
    if not path.exists():
        return []
    traces = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
            # Accept both legacy ReAct traces (steps list) and
            # state machine traces (plan_trace metadata present).
            has_steps = bool(t.get("steps"))
            has_plan = bool(t.get("plan_trace"))
            if t.get("success") and t.get("user_input") and (has_steps or has_plan):
                traces.append(t)
        except json.JSONDecodeError:
            continue
    # most recent first, capped
    return traces[-EXEMPLAR_MAX_TRACES:]


# ── Exemplar index ────────────────────────────────────────────

class ExemplarIndex:
    """In-memory exemplar index backed by trace embeddings.

    Usage
    ─────
    idx = ExemplarIndex(session, cfg)
    idx.build()           # call once at startup (skipped if embed unavailable)
    context = idx.retrieve("로그 보고 분석해줘")
    # context: str to inject into agent system prompt, or "" if nothing useful
    """

    def __init__(self, session: requests.Session, cfg: FridayConfig):
        self._session = session
        self._cfg = cfg
        self._entries: List[Tuple[List[float], Dict[str, Any]]] = []
        self.available = False

    def build(self) -> None:
        """Load and embed successful traces.  Silently skips on errors."""
        traces = _load_successful_traces(self._cfg)
        if not traces:
            return

        entries: List[Tuple[List[float], Dict[str, Any]]] = []
        for t in traces:
            emb = _embed_one(t["user_input"], self._session, self._cfg)
            if emb is None:
                return  # embed unavailable → stop building
            entries.append((emb, t))

        self._entries = entries
        self.available = len(entries) > 0

    def rebuild_if_needed(self) -> None:
        """Rebuild from scratch (called after new traces are flushed)."""
        self._entries = []
        self.available = False
        self.build()

    def retrieve(self, query: str) -> str:
        """Return a formatted exemplar context block for the agent prompt.

        Returns "" if nothing useful is found.
        """
        if not self.available or not self._entries:
            return ""

        query_emb = _embed_one(query, self._session, self._cfg)
        if query_emb is None:
            return ""

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for emb, trace in self._entries:
            score = _cosine(query_emb, emb)
            if score >= EXEMPLAR_MIN_SCORE:
                scored.append((score, trace))

        if not scored:
            return ""

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:EXEMPLAR_TOP_K]

        lines = ["[유사한 과거 작업 참고]"]
        for rank, (score, t) in enumerate(top, 1):
            steps = t.get("steps", [])
            if t.get("plan_trace"):
                # State machine trace: include per-step description when available
                parts = []
                for s in steps:
                    if s.get("action") == "answer":
                        continue
                    desc = s.get("description", "")
                    if desc:
                        parts.append(f"{s['action']}({desc[:25]})")
                    else:
                        parts.append(s["action"])
                steps_desc = " → ".join(parts)
                source_tag = "[SM]"
            else:
                steps_desc = " → ".join(
                    s["action"] for s in steps if s.get("action") != "answer"
                )
                source_tag = ""
            lines.append(
                f"{rank}. \"{t['user_input'][:60]}\" "
                f"(유사도 {score:.2f}){source_tag}\n   실행 순서: {steps_desc or '(없음)'}"
            )
        lines.append("")  # trailing blank line
        return "\n".join(lines)
