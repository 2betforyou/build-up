"""Ollama LLM interaction: health check, chat, routing, and file rewrite."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import requests

from buildup.config import BuildupConfig
from buildup.conversation import ConversationHistory
from buildup.jobs import append_action_log
from buildup.paths import (
    compute_diff,
    ensure_within,
    is_text_file,
    next_output_name,
    read_text_file,
)
from buildup.prompts import SYSTEM_REWRITE, system_fast, system_main, system_research
from buildup.state import job_dir


def _with_thinking_tags(thinking: str, content: str) -> str:
    """Convert Ollama's separate thinking field into Build-up's tagged format."""
    thinking = (thinking or "").strip()
    if not thinking:
        return content or ""
    return f"<think>{thinking}</think>{content or ''}"


# ============================================================
# Diff computation (lives here because rendering uses it too,
# but the pure computation is path-level — we import from paths
# and re-export for convenience)
# ============================================================
# compute_diff is imported from paths and used by rewrite_file.
# render_diff lives in rendering.py.


def check_ollama(session: requests.Session, cfg: BuildupConfig) -> bool:
    """Return True if the Ollama server is reachable."""
    try:
        resp = session.get(cfg.ollama_health_url, timeout=5)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def chat(
    session: requests.Session,
    cfg: BuildupConfig,
    model: str,
    messages: List[Dict[str, str]],
    keep_alive: str = "5m",
    logger: Optional[logging.Logger] = None,
    think: Optional[bool] = False,
    json_mode: bool = False,
    sanitize_thinking: bool = True,
    display_thinking: bool = False,
    json_schema: Optional[Dict[str, Any]] = None,
) -> str:
    """Send a chat request to Ollama and return the response text.

    think=False  — disable chain-of-thought (models that support it)
    json_mode=True — force JSON-only output via Ollama format field
    json_schema=... — enforce the supplied JSON Schema instead of plain JSON mode
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": keep_alive,
    }
    if think is not None:
        payload["think"] = think
    if json_schema is not None:
        payload["format"] = json_schema
    elif json_mode:
        payload["format"] = "json"
    if logger:
        logger.info("Ollama request  model=%s  msgs=%d", model, len(messages))
    try:
        response = session.post(
            cfg.ollama_url, json=payload,
            timeout=(cfg.ollama_connect_timeout, cfg.ollama_timeout),
        )
        response.raise_for_status()
    except requests.ConnectionError:
        raise ConnectionError(
            "Ollama에 연결할 수 없습니다. 'ollama serve'가 실행 중인지 확인하십시오."
        )
    except requests.Timeout:
        raise TimeoutError(
            f"Ollama 응답 대기 시간 초과 ({cfg.ollama_timeout}초)."
        )
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Ollama HTTP 오류: {exc.response.status_code} — {exc.response.text[:200]}"
        )
    try:
        data = response.json()
    except json.JSONDecodeError:
        raise RuntimeError("Ollama 응답을 JSON으로 파싱할 수 없습니다.")
    message = data.get("message", {}) or {}
    content = message.get("content") or ""
    thinking = message.get("thinking") or data.get("thinking") or ""
    if not content and not thinking:
        raise RuntimeError(
            f"Ollama 응답에 content가 없습니다: {json.dumps(data, ensure_ascii=False)[:300]}"
        )
    content = _with_thinking_tags(thinking, content)
    if logger:
        logger.info("Ollama response  model=%s  len=%d", model, len(content))
    if sanitize_thinking:
        if display_thinking:
            render_thinking_block(content)
        if _needs_final_answer_repair(content):
            system_prompt = _first_system_message(messages)
            original_user_text = _last_user_message(messages)
            return _repair_final_answer(
                original_user_text, system_prompt, model,
                session, cfg, logger, keep_alive=keep_alive,
            )
        return user_visible_answer(content)
    return content


def chat_stream(
    session: requests.Session,
    cfg: BuildupConfig,
    model: str,
    messages: List[Dict[str, str]],
    keep_alive: str = "5m",
    logger: Optional[logging.Logger] = None,
    think: Optional[bool] = False,
) -> Generator[str, None, None]:
    """Stream a chat request to Ollama, yielding text chunks as they arrive."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": keep_alive,
    }
    if think is not None:
        payload["think"] = think
    if logger:
        logger.info("Ollama stream  model=%s  msgs=%d", model, len(messages))
    try:
        response = session.post(
            cfg.ollama_url, json=payload,
            timeout=(cfg.ollama_connect_timeout, cfg.ollama_timeout), stream=True,
        )
        response.raise_for_status()
    except requests.ConnectionError:
        raise ConnectionError(
            "Ollama에 연결할 수 없습니다. 'ollama serve'가 실행 중인지 확인하십시오."
        )
    except requests.Timeout:
        raise TimeoutError(f"Ollama 응답 대기 시간 초과 ({cfg.ollama_timeout}초).")
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Ollama HTTP 오류: {exc.response.status_code} — {exc.response.text[:200]}"
        )

    in_thinking_field = False
    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if data.get("done"):
            if in_thinking_field:
                yield "</think>"
            if logger:
                logger.info("Ollama stream done  model=%s", model)
            break
        message = data.get("message", {}) or {}
        thinking = message.get("thinking") or data.get("thinking") or ""
        chunk = message.get("content", "")
        if thinking:
            if not in_thinking_field:
                yield "<think>"
                in_thinking_field = True
            yield thinking
        if chunk:
            if in_thinking_field:
                yield "</think>"
                in_thinking_field = False
            yield chunk


def _with_assistant_context(system_prompt: str, assistant_context: str = "") -> str:
    context = assistant_context.strip()
    if not context:
        return system_prompt
    return f"{system_prompt}\n\n[build-up assistant context]\n{context}"


def _is_research_context(assistant_context: str = "") -> bool:
    return "buildup_research_paper_reader" in (assistant_context or "")


_THINK_TAG_RE = re.compile(
    r"<(?:think|thought|thinking)>[\s\S]*?</(?:think|thought|thinking)>",
    re.I,
)
_THINK_OPEN_RE = re.compile(r"<(?:think|thought|thinking)>", re.I)
_THINK_CLOSE_RE = re.compile(r"</(?:think|thought|thinking)>", re.I)
_UNCLOSED_THINK_RE = re.compile(r"<(?:think|thought|thinking)>[\s\S]*$", re.I)


def strip_thinking(text: str) -> str:
    """Remove common thinking tags from user-visible text and chat history."""
    cleaned = _THINK_TAG_RE.sub("", text)
    cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    return cleaned.strip()


def split_thinking_text(text: str) -> Tuple[str, str]:
    """Split think-tagged text into (thinking, final_answer)."""
    think_parts: List[str] = []
    answer_parts: List[str] = []
    remaining = text
    in_think = False
    while remaining:
        if not in_think:
            match = _THINK_OPEN_RE.search(remaining)
            if match:
                answer_parts.append(remaining[:match.start()])
                remaining = remaining[match.end():]
                in_think = True
            else:
                answer_parts.append(remaining)
                break
        else:
            match = _THINK_CLOSE_RE.search(remaining)
            if match:
                think_parts.append(remaining[:match.start()])
                remaining = remaining[match.end():]
                in_think = False
            else:
                think_parts.append(remaining)
                break
    return "".join(think_parts).strip(), "".join(answer_parts).strip()


def render_thinking_block(text: str) -> None:
    """Render non-streaming thinking output, if present, in the grey channel."""
    thinking, _answer = split_thinking_text(text)
    if not thinking:
        return
    try:
        from rich.markdown import Markdown
        from rich.panel import Panel
        from rich.text import Text
        from buildup.rendering import console

        try:
            console.print(Panel(Markdown(thinking), title="[grey50]thinking[/grey50]", border_style="grey50"))
        except Exception:
            console.print(Panel(Text(thinking, style="dim"), title="[grey50]thinking[/grey50]", border_style="grey50"))
    except Exception:
        pass


def _first_system_message(messages: List[Dict[str, str]]) -> str:
    for message in messages:
        if message.get("role") == "system":
            return message.get("content", "")
    return ""


def _last_user_message(messages: List[Dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


EMPTY_FINAL_ANSWER = (
    "모델이 최종 답변 없이 내부 추론만 반환했습니다. "
    "같은 요청을 한 번 더 보내면 최종 답변만 다시 생성하겠습니다."
)


def user_visible_answer(text: str) -> str:
    """Return a non-thinking final answer, never raw internal reasoning."""
    cleaned = strip_thinking(text)
    return cleaned if cleaned else EMPTY_FINAL_ANSWER


def stream_user_visible_chunks(
    stream: Generator[str, None, None],
    raw_parts: List[str],
) -> Generator[str, None, None]:
    """Yield only final-answer chunks while collecting raw model output."""
    pending = ""
    in_think = False
    for chunk in stream:
        raw_parts.append(chunk)
        pending += chunk
        while True:
            if not in_think:
                match = _THINK_OPEN_RE.search(pending)
                if match:
                    before = pending[:match.start()]
                    if before:
                        yield before
                    pending = pending[match.end():]
                    in_think = True
                else:
                    cutoff = max(0, len(pending) - 20)
                    if cutoff:
                        safe, pending = pending[:cutoff], pending[cutoff:]
                        yield safe
                    break
            else:
                match = _THINK_CLOSE_RE.search(pending)
                if match:
                    pending = pending[match.end():]
                    in_think = False
                else:
                    cutoff = max(0, len(pending) - 20)
                    if cutoff:
                        pending = pending[cutoff:]
                    break
    if pending and not in_think:
        yield pending


def _needs_final_answer_repair(raw_text: str) -> bool:
    return bool(raw_text.strip()) and not bool(strip_thinking(raw_text))


def _repair_final_answer(
    original_user_text: str,
    system_prompt: str,
    model: str,
    session: requests.Session,
    cfg: BuildupConfig,
    logger: logging.Logger,
    *,
    keep_alive: str,
) -> str:
    """Ask the model once more for final-only output when it returned only thinking."""
    repair_prompt = f"""\
Your previous response contained internal reasoning but no final answer.
Do not include hidden reasoning, chain-of-thought, or <think> tags.
Return only the final user-facing answer. Unless the user requested one
specific language, answer in Korean first and add a concise English Brief.

[Original user request]
{original_user_text}
"""
    repaired = chat(
        session, cfg, model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": repair_prompt},
        ],
        keep_alive=keep_alive,
        logger=logger,
        think=False,
        sanitize_thinking=False,
    )
    return user_visible_answer(repaired)


def answer_query_stream(
    text: str,
    mode: str,
    session: requests.Session,
    cfg: BuildupConfig,
    logger: logging.Logger,
    history: Optional[ConversationHistory] = None,
    assistant_context: str = "",
) -> Tuple[Generator[str, None, None], str, str]:
    """Like answer_query but streams the response.

    Returns (stream_generator, model_name, used_mode).
    The generator records the full response to history when exhausted.
    Note: refine mode falls back to non-streaming (3 sequential calls).
    """
    used_mode = auto_route(text, cfg) if mode == "auto" else mode

    if _is_research_context(assistant_context) and used_mode in {"fast", "main"}:
        system_prompt = _with_assistant_context(system_research(), assistant_context)
        if history:
            history.add("user", text)
            msgs = history.get_messages(system_prompt)
        else:
            msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ]

        def _research_gen() -> Generator[str, None, None]:
            buf = ""
            for chunk in chat_stream(session, cfg, cfg.research_model, msgs, "10m", logger, think=True):
                buf += chunk
                yield chunk
            final_answer = user_visible_answer(buf)
            if _needs_final_answer_repair(buf):
                final_answer = _repair_final_answer(
                    text, system_prompt, cfg.research_model,
                    session, cfg, logger, keep_alive="10m",
                )
                yield final_answer
            if history:
                history.add("assistant", final_answer)

        return _research_gen(), cfg.research_model, "research"

    if used_mode == "fast":
        system_prompt = _with_assistant_context(system_fast(), assistant_context)
        if history:
            history.add("user", text)
            msgs = history.get_messages(system_prompt)
        else:
            msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ]

        def _fast_gen() -> Generator[str, None, None]:
            buf = ""
            for chunk in chat_stream(session, cfg, cfg.fast_model, msgs, "2m", logger, think=True):
                buf += chunk
                yield chunk
            final_answer = user_visible_answer(buf)
            if _needs_final_answer_repair(buf):
                final_answer = _repair_final_answer(
                    text, system_prompt, cfg.fast_model,
                    session, cfg, logger, keep_alive="2m",
                )
                yield final_answer
            if history:
                history.add("assistant", final_answer)

        return _fast_gen(), cfg.fast_model, used_mode

    if used_mode == "main":
        system_prompt = _with_assistant_context(system_main(), assistant_context)
        if history:
            history.add("user", text)
            msgs = history.get_messages(system_prompt)
        else:
            msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ]

        def _main_gen() -> Generator[str, None, None]:
            buf = ""
            for chunk in chat_stream(session, cfg, cfg.main_model, msgs, "10m", logger, think=True):
                buf += chunk
                yield chunk
            final_answer = user_visible_answer(buf)
            if _needs_final_answer_repair(buf):
                final_answer = _repair_final_answer(
                    text, system_prompt, cfg.main_model,
                    session, cfg, logger, keep_alive="10m",
                )
                yield final_answer
            if history:
                history.add("assistant", final_answer)

        return _main_gen(), cfg.main_model, used_mode

    # refine: 3 sequential LLM calls — stream only the final answer
    if used_mode == "refine":
        result, model_name, _ = answer_query(
            text, mode, session, cfg, logger, history,
            assistant_context=assistant_context,
        )

        def _refine_gen() -> Generator[str, None, None]:
            yield result

        return _refine_gen(), model_name, used_mode

    raise ValueError(f"지원하지 않는 mode: {mode}")


def auto_route(text: str, cfg: BuildupConfig) -> str:
    """Select fast vs main model based on task type.

    MODEL SELECTION CRITERIA
    ────────────────────────
    fast — "Format & Structure" tasks
      · Deterministic, single-correct-answer output
      · File rewrite   : mechanical style/grammar/structure transformation
      · Intent parsing : structured JSON from natural language
      · Metadata extract: title/author/year from paper header
      · Format convert : search results → file, log → summary
      · Trivial chat   : greetings, acks, one-liner factual queries

    main — "Reason & Analyze" tasks
      · Requires judgment, synthesis, or deep background knowledge
      · Agent loop     : action selection, multi-step planning
      · Paper analysis : summarization, Q&A, explanation, comparison
      · Search synthesis: extracting insights from raw search results
      · KB Q&A         : Open WebUI knowledge-base question answering
      · Complex chat   : anything needing reasoning over knowledge

    Rule of thumb:
      "Will any reasonable answer differ depending on what the model
       *understands* vs. what it mechanically transforms?"
      YES → main    NO → fast
    """
    lowered = text.lower()
    # Short queries with no analysis keywords → fast
    if len(text) < 80 and not any(kw in lowered for kw in cfg.heavy_keywords):
        return "fast"
    return "main"


def answer_query(
    text: str,
    mode: str,
    session: requests.Session,
    cfg: BuildupConfig,
    logger: logging.Logger,
    history: Optional[ConversationHistory] = None,
    assistant_context: str = "",
) -> Tuple[str, str, str]:
    """Route and answer; optionally uses conversation history.

    Returns (answer_text, model_name, used_mode).
    """
    used_mode = auto_route(text, cfg) if mode == "auto" else mode

    if _is_research_context(assistant_context) and used_mode in {"fast", "main"}:
        system_prompt = _with_assistant_context(system_research(), assistant_context)
        if history:
            history.add("user", text)
            msgs = history.get_messages(system_prompt)
        else:
            msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ]
        raw = chat(
            session, cfg, cfg.research_model, msgs,
            keep_alive="10m", logger=logger, sanitize_thinking=False,
        )
        result = (
            _repair_final_answer(text, system_prompt, cfg.research_model, session, cfg, logger, keep_alive="10m")
            if _needs_final_answer_repair(raw)
            else user_visible_answer(raw)
        )
        if history:
            history.add("assistant", result)
        return result, cfg.research_model, "research"

    if used_mode == "fast":
        system_prompt = _with_assistant_context(system_fast(), assistant_context)
        if history:
            history.add("user", text)
            msgs = history.get_messages(system_prompt)
        else:
            msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ]
        raw = chat(
            session, cfg, cfg.fast_model, msgs,
            keep_alive="2m", logger=logger, sanitize_thinking=False,
        )
        result = (
            _repair_final_answer(text, system_prompt, cfg.fast_model, session, cfg, logger, keep_alive="2m")
            if _needs_final_answer_repair(raw)
            else user_visible_answer(raw)
        )
        if history:
            history.add("assistant", result)
        return result, cfg.fast_model, used_mode

    if used_mode == "main":
        system_prompt = _with_assistant_context(system_main(), assistant_context)
        if history:
            history.add("user", text)
            msgs = history.get_messages(system_prompt)
        else:
            msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ]
        raw = chat(
            session, cfg, cfg.main_model, msgs,
            keep_alive="10m", logger=logger, sanitize_thinking=False,
        )
        result = (
            _repair_final_answer(text, system_prompt, cfg.main_model, session, cfg, logger, keep_alive="10m")
            if _needs_final_answer_repair(raw)
            else user_visible_answer(raw)
        )
        if history:
            history.add("assistant", result)
        return result, cfg.main_model, used_mode

    if used_mode == "refine":
        fast_system = _with_assistant_context(system_fast(), assistant_context)
        main_system = _with_assistant_context(system_main(), assistant_context)
        draft = user_visible_answer(chat(
            session, cfg, cfg.fast_model,
            [{"role": "system", "content": fast_system},
             {"role": "user", "content": text}],
            keep_alive="2m", logger=logger, sanitize_thinking=False,
        ))
        critique = user_visible_answer(chat(
            session, cfg, cfg.main_model,
            [{"role": "system", "content": main_system},
             {"role": "user", "content": f"""아래 초안을 비평하라.
목표: 누락 찾기 / 근거 부족 지적 / 과장·환각 지적 / 구조 제안

[사용자 요청]
{text}

[초안]
{draft}
"""}],
            keep_alive="10m", logger=logger, sanitize_thinking=False,
        ))
        raw_final = chat(
            session, cfg, cfg.main_model,
            [{"role": "system", "content": main_system},
             {"role": "user", "content": f"""아래를 바탕으로 최종 답변을 작성하라.

[사용자 요청]
{text}

[초안]
{draft}

[비평]
{critique}
"""}],
            keep_alive="10m", logger=logger, sanitize_thinking=False,
        )
        final = (
            _repair_final_answer(text, main_system, cfg.main_model, session, cfg, logger, keep_alive="10m")
            if _needs_final_answer_repair(raw_final)
            else user_visible_answer(raw_final)
        )
        if history:
            history.add("user", text)
            history.add("assistant", final)
        return final, f"{cfg.fast_model} -> {cfg.main_model}", used_mode

    raise ValueError(f"지원하지 않는 mode: {mode}")


def _strip_rewrite_wrapping(text: str) -> str:
    """Remove common thinking-model meta-commentary from rewrite output."""
    import re
    lines = text.splitlines()

    # Strip leading meta lines (e.g. "사용자의 지시에 따라...", "[수정 결과]", blank lines)
    meta_head = re.compile(
        r"^(\s*|\[.*?\]|사용자의\s*지시|수정\s*결과|다음을\s*수정|아래.*수정.*:).*$"
    )
    while lines and meta_head.match(lines[0]):
        lines.pop(0)

    # Strip trailing meta lines (e.g. "그대로 반환합니다...", "원문과 편집 지시에만...")
    meta_tail = re.compile(
        r"^(그대로\s*반환|원문과\s*편집|추가\s*설명.*추가하지|위\s*지시에\s*따라)"
    )
    while lines and (meta_tail.match(lines[-1]) or lines[-1].strip() == ""):
        if lines[-1].strip() == "" and len(lines) > 1:
            lines.pop()
        elif meta_tail.match(lines[-1]):
            lines.pop()
        else:
            break

    return "\n".join(lines)


def _build_job_context_block(job_id: str, cfg: BuildupConfig) -> str:
    """Return a short context paragraph about the current job for rewrite prompts."""
    from buildup.paths import list_files
    from buildup.state import job_dir

    label = job_id.split("-", 2)[-1] if job_id.count("-") >= 2 else job_id
    base = job_dir(job_id, cfg)

    try:
        files = list_files(base)
        file_summary = ", ".join(files[:15]) if files else "(없음)"
    except Exception:
        file_summary = "(알 수 없음)"

    # Try to read a README or notes file for richer context
    notes = ""
    for candidate in ("README.md", "notes.md", ".notes.md", "memo.md"):
        p = base / candidate
        if p.exists():
            try:
                notes = p.read_text(encoding="utf-8")[:600].strip()
            except Exception:
                pass
            break

    ctx = f"\n현재 job: {label}  |  파일: {file_summary}\n"
    if notes:
        ctx += f"job 메모: {notes}\n"
    return ctx


def _validate_rewrite_result(
    original: str,
    rewritten: str,
    instruction: str,
    logger: logging.Logger,
) -> None:
    """Warn (via logger) when the rewrite result looks suspicious."""
    if not rewritten.strip():
        raise ValueError("모델이 빈 응답을 반환했습니다. 다시 시도하거나 지시를 구체적으로 바꿔보세요.")

    reduction_keywords = ("요약", "줄여", "짧게", "간결", "압축", "shorten", "summarize", "condense")
    if (
        len(rewritten) < len(original) * 0.3
        and not any(kw in instruction.lower() for kw in reduction_keywords)
    ):
        logger.warning(
            "Rewrite result is much shorter than original (%d → %d chars). "
            "Model may have truncated content.",
            len(original), len(rewritten),
        )

    if rewritten.strip() == original.strip():
        logger.warning("Rewrite result is identical to original — no changes were made.")


def rewrite_file(
    job_id: str,
    relpath: str,
    instruction: str,
    output: Optional[str],
    mode: str,  # kept for API compatibility; rewrite always uses fast model
    session: requests.Session,
    cfg: BuildupConfig,
    logger: logging.Logger,
    show_diff: bool = True,
) -> Tuple[Path, Optional[str]]:
    """Rewrite a file.  Returns (output_path, diff_text_or_None).

    Enforces sandbox: source must be readable inside job, output is
    written as a NEW file inside the same job (never overwrites original).
    """
    from buildup.sandbox import require_read, require_write

    base = job_dir(job_id, cfg)
    src = ensure_within(base / relpath, base)

    # Sandbox: validate read on source
    require_read(src, cfg, context=f"rewrite source: {relpath}")

    if not src.is_file():
        raise ValueError(f"파일을 찾을 수 없습니다: {src}")
    if not is_text_file(src, cfg):
        raise ValueError(f"현재는 텍스트 파일만 지원합니다: {src.suffix}")

    original = read_text_file(src, cfg)

    # Rewrite always uses fast model — editing doesn't benefit from reasoning models
    model = cfg.fast_model
    keep_alive = "2m"

    job_ctx = _build_job_context_block(job_id, cfg)
    prompt = f"""파일명: {src.name}
{job_ctx}
수정 지시:
{instruction}

원본 내용:
{original}

위 지시에 따라 수정된 파일의 최종 내용만 출력하라.
설명, 해설, 머리말, 꼬리말, 코드펜스를 절대 출력하지 마라.
첫 번째 글자부터 파일 내용 그 자체여야 한다."""
    raw = chat(
        session, cfg, model,
        [{"role": "system", "content": SYSTEM_REWRITE},
         {"role": "user", "content": prompt},
         {"role": "assistant", "content": ""}],
        keep_alive=keep_alive, logger=logger,
    )
    rewritten = _strip_rewrite_wrapping(raw)

    # Validate result
    _validate_rewrite_result(original, rewritten, instruction, logger)

    out_name = output or next_output_name(src)
    dst = ensure_within(base / out_name, base)

    # Sandbox: validate write on destination
    require_write(dst, job_id, cfg, context=f"rewrite output: {out_name}")

    # If output already exists, preserve previous content in action log before overwriting
    if dst.exists():
        prev_content = dst.read_text(encoding="utf-8")
        append_action_log(
            job_id, cfg, "rewrite_overwrite_prev",
            f"{dst.name} 이전 내용 보존:\n{prev_content[:2000]}",
        )

    dst.write_text(rewritten, encoding="utf-8")

    diff_text = None
    if show_diff:
        diff_text = compute_diff(original, rewritten, filename=src.name)

    logger.info("Rewrite: %s -> %s  (model=%s)", src.name, dst.name, model)
    append_action_log(job_id, cfg, "rewrite", f"{src.name} -> {dst.name} | {instruction[:100]}")
    return dst, diff_text
