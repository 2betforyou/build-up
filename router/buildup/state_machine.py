"""State Machine executor for Build-up's Tier-2 agent (replaces ReAct loop).

States:
  IDLE → PLANNING → VALIDATING → CONFIRMING → EXECUTING → RETRYING → COMPLETING → DONE
                                             ↘ EXECUTING →                        ↘ FAILED
                              ↘ ABORTED (user rejected plan)
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

import requests

from buildup.config import BuildupConfig
from buildup.plan import (
    ExecutionPlan,
    PlanStep,
    PlanningError,
    StepOutcome,
    evaluate_condition,
)

# ── Planner constants ─────────────────────────────────────────────────────────

_CONFIRM_CAPABILITIES = frozenset({"confirm-always", "confirm-git", "confirm-shell"})
_MAX_PLAN_STEPS = 8

_PLANNER_SYSTEM = """\
당신은 build-up 에이전트 플래너다.
사용자 요청을 실행 가능한 도구 단계로 바꾸는 계획 전용 모델이다.
최종 답변을 쓰지 말고, 어떤 도구를 어떤 순서로 호출할지만 결정한다.

현재 컨텍스트:
{context_block}

사용 가능한 도구:
{tools_block}

출력 형식 (JSON 객체만 반환, 다른 텍스트 금지):
{{
  "rationale": "계획 이유를 한 문장 이하로 간결하게",
  "steps": [
    {{
      "action": "도구_이름",
      "params": {{...}},
      "description": "이 단계의 한국어 설명",
      "condition": "step_1.success"
    }}
  ]
}}

제약:
1. action은 반드시 위 도구 목록에 있는 정확한 이름이어야 한다.
2. 최대 {max_steps}단계. 단순 요청은 1-2단계로 충분하다.
3. "answer" 도구는 사용하지 않는다. 최종 자연어 응답은 실행 이후 COMPLETING 단계가 맡는다.
4. 기존 파일을 수정/추가할 때는 반드시 먼저 read_file로 읽고, 그 다음 write_file로 써야 한다.
   올바른 예: step1=read_file, step2=write_file(content="{{{{step_1_result}}}} + 추가내용")
   잘못된 예: step1=write_file(content="{{{{step_1_result}}}}") ← 자기 자신 참조 불가
5. write_file에서 이전 단계 결과가 필요하면 content에 "{{{{step_N_result}}}}" 플레이스홀더를 사용하라. N은 반드시 현재 단계보다 작아야 한다.
6. condition 필드는 선택사항이다. "step_N.success" 또는 "step_N.failure" 형식만 허용.
7. params에는 실제 도구가 요구하는 최소 필드만 넣는다. 설명문이나 모델에게 하는 지시를 params에 섞지 않는다.
8. 도구 결과가 필요한 사실은 계획 단계에서 지어내지 않는다. 읽기/검색/조회 단계로 확인하게 한다.
9. JSON 앞뒤에 Markdown, 주석, `<think>`, 설명문을 붙이지 않는다.
10. 모호한 부분은 현재 컨텍스트, active paper, last_file, job 파일 목록을 기준으로 가장 합리적으로 해석한다.
"""

_BUILDUP_PLANNER_RULES = """\

build-up 논문 모드 전용 규칙:
1. active paper가 선택되어 있고 사용자가 "논문", "paper", "abstract", "초록", "섹션", "리뷰", "요약", "번역", "설명"을 말하면
   반드시 active paper 작업으로 해석한다.
2. active paper의 파일이나 섹션을 읽을 때는 read_file/list_files를 절대 사용하지 않는다.
   반드시 read_paper_file/list_paper_files/paper_qa/explain_concept 중 하나를 사용한다.
3. abstract/초록/서론/방법론/실험/결과/결론 같은 섹션 요청은 read_paper_file(section="...") 한 단계로 충분하다.
   예: "abstract 번역" → read_paper_file(section="abstract")
4. summary.md, review.md, evidence.json, paper.paper.json, notes.md는 논문 라이브러리 파일이다.
   이 파일들은 read_file이 아니라 read_paper_file(relpath="summary.md")처럼 읽어야 한다.
5. read_paper_file 다음에 같은 파일을 read_file로 다시 읽는 단계를 만들지 않는다.
6. 번역/요약/설명/비판 요청은 보통 읽기 1단계만 계획한다. 최종 자연어 답변은 COMPLETING 단계에서 생성한다.
7. 논문에 없는 섹션이나 정보가 있으면 도구 결과를 바탕으로 "논문에서 명시/추출되지 않음"이라고 답하게 둔다.
8. 사용자가 "이 논문의 초록/abstract"처럼 말하면 relpath 대신 section="abstract"를 우선 사용한다.
9. 사용자가 "노트에 남겨", "기억해", "다음에 이어서"라고 말하면 append_paper_note 또는 refresh_paper_memory를 사용한다.
"""


def _extract_plan_json(raw: str) -> Optional[Dict[str, Any]]:
    """Extract first valid JSON object with 'steps' key from LLM output."""
    text = re.sub(r"^```\w*\n?", "", raw.strip(), flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text, flags=re.MULTILINE)
    text = re.sub(
        r"<(?:think|thought|thinking)>[\s\S]*?</(?:think|thought|thinking)>",
        "", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"<(?:think|thought|thinking)>[\s\S]*$", "", text, flags=re.IGNORECASE).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "steps" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            return None
        depth, end = 0, -1
        for i in range(start, len(text)):
            depth += text[i] == "{"
            depth -= text[i] == "}"
            if depth == 0:
                end = i
                break
        if end == -1:
            return None
        candidate = text[start:end + 1]
        for attempt in (candidate, re.sub(r",\s*}", "}", re.sub(r",\s*]", "]", candidate))):
            try:
                parsed = json.loads(attempt)
                if isinstance(parsed, dict) and "steps" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass
        pos = end + 1


# ── States ────────────────────────────────────────────────────────────────────

class MachineState(Enum):
    IDLE        = auto()
    STRUCTURING = auto()   # 5.2: intent structuring (fast model → StructuredIntent)
    PLANNING    = auto()
    VALIDATING  = auto()
    CONFIRMING  = auto()
    EXECUTING   = auto()
    RETRYING    = auto()
    COMPLETING  = auto()
    DONE        = auto()
    FAILED      = auto()
    ABORTED     = auto()


_TERMINAL = frozenset({MachineState.DONE, MachineState.FAILED, MachineState.ABORTED})

# Result strings that indicate tool-level failure
_ERROR_PREFIXES = ("[오류]", "[차단]", "[알 수 없는 도구")


def _is_tool_error(result: str) -> bool:
    return any(result.startswith(p) for p in _ERROR_PREFIXES)


# ── Plan renderer ─────────────────────────────────────────────────────────────

def _render_plan(plan: ExecutionPlan) -> None:
    """Print the execution plan as a Rich panel before confirmation."""
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from buildup.rendering import console

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("step", style="dim", width=4)
    table.add_column("desc")
    table.add_column("flag", width=12)

    for step in plan.steps:
        needs_confirmation = step.required_capability in _CONFIRM_CAPABILITIES
        flag = "[yellow]⚠ confirm[/yellow]" if needs_confirmation else ""
        cond = f" [dim](if: {step.condition})[/dim]" if step.condition else ""
        params = ""
        if needs_confirmation:
            exact_params = json.dumps(
                step.params, ensure_ascii=False, sort_keys=True, default=str,
            )
            params = f"\n[dim]exact params: {escape(exact_params)}[/dim]"
        table.add_row(
            f"{step.step_id}.",
            f"[bold]{step.action}[/bold]  {escape(step.description)}{cond}{params}",
            flag,
        )

    rationale_line = f"[dim]{escape(plan.rationale[:200])}[/dim]\n\n" if plan.rationale else ""
    console.print(Panel(
        rationale_line + table,
        title=f"Plan ({len(plan.steps)} steps)",
        border_style="yellow",
    ))


# ── State machine ─────────────────────────────────────────────────────────────

class PlanStateMachine:
    """Executes an ExecutionPlan via explicit FSM transitions."""

    def __init__(self) -> None:
        self.state = MachineState.IDLE
        self.plan: Optional[ExecutionPlan] = None
        self.current_step_idx: int = 0
        self.outcomes: List[StepOutcome] = []
        self._last_file: Optional[str] = None
        self._step_retries_used: int = 0
        self._error_message: str = ""
        self.final_response: str = ""
        self._structured_intent = None  # StructuredIntent | None
        self._approved_capabilities: frozenset[str] = frozenset()

        # Injected in run()
        self._user_input: str = ""
        self._cfg: Optional[BuildupConfig] = None
        self._session = None
        self._logger: Optional[logging.Logger] = None
        self._mode: str = "auto"
        self._render_status: Callable[[str], None] = print
        self._tracer = None
        self._history = None
        self._task_frame = None
        self._assistant_context: str = ""
        self._active_paper_id: Optional[str] = None

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(
        self,
        user_input: str,
        cfg: BuildupConfig,
        session: requests.Session,
        logger: logging.Logger,
        mode: str,
        history,
        render_status,
        tracer=None,
        exemplar_index=None,
        task_frame=None,
        assistant_context: str = "",
        active_paper_id: Optional[str] = None,
    ) -> str:
        """Run the full state machine and return the final response string."""
        self._user_input = user_input
        self._cfg = cfg
        self._session = session
        self._logger = logger
        self._mode = mode
        self._render_status = render_status
        self._tracer = tracer
        self._history = history
        self._task_frame = task_frame
        self._assistant_context = assistant_context
        self._active_paper_id = active_paper_id

        handlers: Dict[MachineState, Callable[[], MachineState]] = {
            MachineState.STRUCTURING: self._handle_structuring,
            MachineState.PLANNING:    self._handle_planning,
            MachineState.VALIDATING:  self._handle_validating,
            MachineState.CONFIRMING:  self._handle_confirming,
            MachineState.EXECUTING:   self._handle_executing,
            MachineState.RETRYING:    self._handle_retrying,
            MachineState.COMPLETING:  self._handle_completing,
        }

        self._transition(MachineState.STRUCTURING)

        while self.state not in _TERMINAL:
            handler = handlers.get(self.state)
            if handler is None:
                self._error_message = f"Unhandled state: {self.state}"
                self._transition(MachineState.FAILED)
                break
            self._transition(handler())

        # Record to history
        if self.state == MachineState.DONE:
            if history:
                history.add("user", user_input)
                history.add("assistant", self.final_response)
            return ""  # already rendered via render_streaming_answer in _handle_completing
        elif self.state == MachineState.ABORTED:
            return "[Aborted] Plan cancelled."
        else:
            return f"[Agent error] {self._error_message}"

    # ── State handlers ────────────────────────────────────────────────────────

    def _handle_structuring(self) -> MachineState:
        """5.2: Convert raw user text → StructuredIntent using fast_model.

        Failure is non-fatal: proceeds to PLANNING with self._structured_intent = None.
        """
        from buildup.intent_structuring import structure_intent
        self._render_status("Structuring intent...")
        self._structured_intent = structure_intent(
            self._user_input, self._session, self._cfg, self._logger
        )
        if self._structured_intent:
            self._logger.info(
                "Structured intent: %s  mode=%s  confidence=%.2f",
                self._structured_intent.intent,
                self._structured_intent.interaction_mode,
                self._structured_intent.confidence,
            )
            if self._tracer:
                self._tracer.set_structured_intent(self._structured_intent)
        else:
            self._logger.info("Intent structuring returned None — proceeding without it")
        return MachineState.PLANNING

    def _handle_planning(self) -> MachineState:
        from buildup.tool_registry import get_tool, all_tools
        from buildup.context import build_context
        from buildup.agent import TOOL_DESCRIPTIONS

        self._render_status("Planning...")

        # Build context block
        ctx = build_context(self._cfg)
        ctx_lines = [
            f"날짜: {ctx['date_ctx']}",
            f"현재 job: {ctx['current_job']}",
            f"파일 목록: {ctx['file_list']}",
            f"다가오는 일정: {ctx['upcoming']}",
        ]
        if ctx.get("job_prefs"):
            ctx_lines.append(f"\n[프로젝트 컨텍스트 (.buildup.md)]\n{ctx['job_prefs']}")
        context_block = "\n".join(ctx_lines)

        if self._assistant_context:
            context_block = self._assistant_context + "\n\n" + context_block

        if self._task_frame is not None:
            try:
                frame_context = self._task_frame.agent_context()
            except Exception:
                frame_context = ""
            if frame_context:
                context_block = frame_context + "\n\n" + context_block

        try:
            from buildup.skills import build_skills_context
            skills_context, selected_skills = build_skills_context(self._user_input, self._cfg)
        except Exception as exc:
            selected_skills = []
            skills_context = ""
            self._logger.warning("Skill context unavailable: %s", exc)
        if skills_context:
            names = ", ".join(skill.name for skill, _score in selected_skills)
            if names:
                self._render_status(f"Skills: {names}")
            context_block = skills_context + "\n\n" + context_block

        # Prepend structured intent if available
        if self._structured_intent is not None:
            si = self._structured_intent
            si_lines = [
                "[구조화된 의도]",
                f"  intent           : {si.intent}",
                f"  interaction_mode : {si.interaction_mode}",
                f"  confidence       : {si.confidence:.2f}",
            ]
            if si.entities:
                ent = {k: v for k, v in si.entities.items() if v}
                if ent:
                    si_lines.append(f"  entities         : {ent}")
            if si.desired_outputs:
                si_lines.append(f"  desired_outputs  : {si.desired_outputs}")
            if si.constraints:
                si_lines.append(f"  constraints      : {dict(si.constraints)}")
            context_block = "\n".join(si_lines) + "\n\n" + context_block

        system_prompt = _PLANNER_SYSTEM.format(
            context_block=context_block,
            tools_block=TOOL_DESCRIPTIONS,
            max_steps=_MAX_PLAN_STEPS,
        )
        if self._active_paper_id:
            system_prompt += _BUILDUP_PLANNER_RULES

        # Stream the planning response.  Some models may emit <think> tags
        # despite think=False; show them only in the grey thinking channel,
        # while the JSON plan body remains buffered for parsing.
        try:
            from buildup.ollama import chat_stream, render_thinking_block
            from buildup.rendering import _THINK_OPEN_RE, _THINK_CLOSE_RE

            chunks: list[str] = []
            pending = ""
            in_think = False
            think_buf: list[str] = []

            from buildup.model_router import RoutingDecision, select_model
            if self._active_paper_id:
                decision = RoutingDecision(
                    model=self._cfg.research_model,
                    tier="research",
                    score=0,
                    reasons=["build-up active paper → research model planner"],
                )
            else:
                decision = select_model(self._structured_intent, self._cfg)
            self._logger.info(
                "Routing: tier=%s model=%s score=%d reasons=%s",
                decision.tier, decision.model, decision.score, decision.reasons,
            )
            if self._tracer:
                self._tracer.set_routing(decision)

            for chunk in chat_stream(
                self._session, self._cfg, decision.model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": self._user_input},
                ],
                keep_alive="10m",
                logger=self._logger,
                think=False,  # JSON-only output — no reasoning needed
            ):
                chunks.append(chunk)
                pending += chunk

                while True:
                    if not in_think:
                        m = _THINK_OPEN_RE.search(pending)
                        if m:
                            pending = pending[m.end():]
                            in_think = True
                        else:
                            cutoff = max(0, len(pending) - 20)
                            pending = pending[cutoff:]
                            break
                    else:
                        m = _THINK_CLOSE_RE.search(pending)
                        if m:
                            think_chunk = pending[:m.start()]
                            if think_chunk:
                                think_buf.append(think_chunk)
                            pending = pending[m.end():]
                            in_think = False
                            if think_buf:
                                render_thinking_block("<think>" + "".join(think_buf) + "</think>")
                                think_buf = []
                        else:
                            cutoff = max(0, len(pending) - 20)
                            if cutoff:
                                think_buf.append(pending[:cutoff])
                                pending = pending[cutoff:]
                            break

            if in_think and pending:
                think_buf.append(pending)
            if think_buf:
                render_thinking_block("<think>" + "".join(think_buf) + "</think>")

            raw = "".join(chunks)
        except Exception as exc:
            raise PlanningError(f"LLM 호출 실패: {exc}", reason="llm_error") from exc

        plan_data = _extract_plan_json(raw)
        if plan_data is None:
            raise PlanningError(
                f"LLM 출력에서 JSON을 파싱할 수 없습니다: {raw[:300]}",
                reason="parse_failure",
            )

        raw_steps: List[Dict[str, Any]] = plan_data.get("steps", [])
        if not raw_steps:
            raise PlanningError("플래너가 빈 단계 목록을 반환했습니다.", reason="validation_error")

        if len(raw_steps) > _MAX_PLAN_STEPS:
            self._logger.warning("Planner returned %d steps (max %d) — truncating", len(raw_steps), _MAX_PLAN_STEPS)
            raw_steps = raw_steps[:_MAX_PLAN_STEPS]

        all_tool_names = {t.name for t in all_tools()}
        steps: List[PlanStep] = []
        requires_confirmation = False

        for i, raw_step in enumerate(raw_steps):
            action = str(raw_step.get("action", "")).strip()
            if not action:
                raise PlanningError(f"단계 {i + 1}: action이 비어 있습니다.", reason="validation_error")
            if action == "answer":
                continue
            tool = get_tool(action)
            if tool is None:
                fuzzy = [n for n in all_tool_names if action in n or n in action]
                if fuzzy:
                    action = fuzzy[0]
                    tool = get_tool(action)
                    self._logger.warning("Planner fuzzy-matched tool: %r → %r", raw_step.get("action"), action)
                else:
                    raise PlanningError(f"단계 {i + 1}: 알 수 없는 도구 '{action}'", reason="validation_error")
            if tool.required_capability in _CONFIRM_CAPABILITIES:
                requires_confirmation = True
            steps.append(PlanStep(
                step_id=i + 1,
                action=action,
                params=raw_step.get("params") or {},
                description=str(raw_step.get("description") or f"{action} 실행"),
                risk_level=tool.risk_level,
                required_capability=tool.required_capability,
                condition=raw_step.get("condition") or None,
            ))

        if not steps:
            raise PlanningError("유효한 단계가 없습니다.", reason="validation_error")

        steps = self._normalize_research_paper_plan(steps)
        if not steps:
            raise PlanningError("유효한 단계가 없습니다.", reason="validation_error")

        self.plan = ExecutionPlan(
            plan_id=str(uuid.uuid4())[:12],
            user_input=self._user_input,
            rationale=str(plan_data.get("rationale") or ""),
            steps=steps,
            requires_confirmation=requires_confirmation,
        )
        return MachineState.VALIDATING

    def _normalize_research_paper_plan(self, steps: List[PlanStep]) -> List[PlanStep]:
        """Repair common planner slips in build-up active-paper workflows."""
        if not self._active_paper_id:
            return steps

        from pathlib import Path
        from buildup.tool_registry import get_tool

        paper_files = {
            "review.md", "summary.md", "memory.md", "evidence.json",
            "paper.paper.json", "metadata.json", "notes.md",
        }
        abstract_requested = bool(re.search(r"abstract|초록", self._user_input, re.I))

        normalized: List[PlanStep] = []
        seen: set[str] = set()
        saw_paper_read = False

        for step in steps:
            action = step.action
            params = dict(step.params or {})
            basename = Path(str(params.get("relpath") or params.get("path") or "")).name

            if action == "read_file" and (saw_paper_read or basename in paper_files):
                tool = get_tool("read_paper_file")
                if tool:
                    action = "read_paper_file"
                    step = PlanStep(
                        step_id=step.step_id,
                        action=action,
                        params=params,
                        description="활성 논문 디렉토리에서 파일 읽기",
                        risk_level=tool.risk_level,
                        required_capability=tool.required_capability,
                        condition=step.condition,
                        max_retries=step.max_retries,
                    )

            if action == "read_paper_file":
                saw_paper_read = True
                if abstract_requested:
                    params.pop("relpath", None)
                    params.pop("path", None)
                    params["section"] = "abstract"
                    step = PlanStep(
                        step_id=step.step_id,
                        action=action,
                        params=params,
                        description="활성 논문의 abstract 섹션 읽기",
                        risk_level=step.risk_level,
                        required_capability=step.required_capability,
                        condition=step.condition,
                        max_retries=step.max_retries,
                    )

            dedupe_key = json.dumps(
                {"action": step.action, "params": step.params},
                ensure_ascii=False,
                sort_keys=True,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            normalized.append(step)

        for idx, step in enumerate(normalized, start=1):
            step.step_id = idx
        return normalized

    def _handle_validating(self) -> MachineState:
        # Validation already done in build_plan; this state only routes
        if self.plan.requires_confirmation:
            return MachineState.CONFIRMING
        return MachineState.EXECUTING

    def _handle_confirming(self) -> MachineState:
        from buildup.rendering import console
        _render_plan(self.plan)
        console.print("\nProceed with plan? (y/n) ", end="")
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans in ("y", "yes", "예", "ㅛ", "네"):
            self._approved_capabilities = frozenset(
                step.required_capability
                for step in self.plan.steps
                if step.required_capability in _CONFIRM_CAPABILITIES
            )
            return MachineState.EXECUTING
        return MachineState.ABORTED

    def _handle_executing(self) -> MachineState:
        if self.current_step_idx >= len(self.plan.steps):
            return MachineState.COMPLETING

        step = self.plan.steps[self.current_step_idx]

        # Evaluate condition — skip if not met
        if not evaluate_condition(step.condition, self.outcomes):
            self._logger.info(
                "Skipping step %d (%s): condition '%s' not met",
                step.step_id, step.action, step.condition,
            )
            self.outcomes.append(StepOutcome(
                step_id=step.step_id,
                success=True,
                result="(condition not met — skipped)",
                duration_ms=0,
            ))
            self.current_step_idx += 1
            self._step_retries_used = 0
            return MachineState.EXECUTING

        self._render_status(
            f"[{step.step_id}/{len(self.plan.steps)}] {step.action} — {step.description[:60]}"
        )

        resolved_params = self._resolve_templates(step.params, step.step_id)
        outcome = self._execute_step(step, resolved_params)

        if outcome.success:
            self.outcomes.append(outcome)
            self.current_step_idx += 1
            self._step_retries_used = 0
            return MachineState.EXECUTING
        else:
            if self._step_retries_used < step.max_retries:
                return MachineState.RETRYING
            self.outcomes.append(outcome)
            self._error_message = (
                f"Step {step.step_id} ({step.action}) failed: {outcome.result[:200]}"
            )
            return MachineState.FAILED

    def _handle_retrying(self) -> MachineState:
        step = self.plan.steps[self.current_step_idx]
        self._step_retries_used += 1
        self._render_status(
            f"Retrying... ({step.action}, {self._step_retries_used}/{step.max_retries})"
        )

        resolved_params = self._resolve_templates(step.params, step.step_id)
        outcome = self._execute_step(step, resolved_params)
        outcome = StepOutcome(
            step_id=outcome.step_id,
            success=outcome.success,
            result=outcome.result,
            duration_ms=outcome.duration_ms,
            retries_used=self._step_retries_used,
        )

        if outcome.success:
            self.outcomes.append(outcome)
            self.current_step_idx += 1
            self._step_retries_used = 0
            return MachineState.EXECUTING
        else:
            if self._step_retries_used < step.max_retries:
                return MachineState.RETRYING
            self.outcomes.append(outcome)
            self._error_message = (
                f"Step {step.step_id} ({step.action}) max retries exceeded"
            )
            return MachineState.FAILED

    def _completion_model(self) -> str:
        """Return the model used for the final user-facing response."""
        if self._active_paper_id:
            return self._cfg.research_model
        return self._cfg.fast_model

    def _handle_completing(self) -> MachineState:
        """Summarize all outcomes into a natural user-facing response, streamed."""
        from buildup.ollama import chat_stream
        from buildup.prompts import system_fast, system_research
        from buildup.rendering import render_streaming_answer

        # If only skipped steps, give a simple response
        real_outcomes = [o for o in self.outcomes if not o.result.startswith("(")]
        if not real_outcomes:
            self.final_response = "All steps completed."
            return MachineState.DONE

        result_lines: List[str] = []
        for o in self.outcomes:
            if o.step_id > len(self.plan.steps):
                continue
            action = self.plan.steps[o.step_id - 1].action
            limit = 3500 if action in {"read_paper_file", "paper_qa", "explain_concept"} else 800
            result_lines.append(f"단계 {o.step_id} ({action}): {o.result[:limit]}")
        results_text = "\n".join(result_lines)
        prompt = (
            f"다음 실행 결과를 사용자에게 자연스럽게 요약해라. "
            f"완료된 작업과 결과를 명확히 전달하라. "
            f"사용자가 특정 언어만 요청하지 않았다면 한국어 답변 뒤에 짧은 English Brief를 붙여라.\n\n"
            f"사용자 요청: {self._user_input}\n\n"
            f"실행 결과:\n{results_text}"
        )
        completion_model = self._completion_model()
        display_mode = "research" if self._active_paper_id else self._mode
        system_prompt = system_research() if self._active_paper_id else system_fast()
        if self._assistant_context:
            system_prompt = (
                f"{system_prompt}\n\n[build-up execution context]\n{self._assistant_context}"
            )

        try:
            gen = chat_stream(
                self._session, self._cfg, completion_model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                keep_alive="10m" if self._active_paper_id else "2m",
                logger=self._logger,
                think=True,
            )
            self.final_response = render_streaming_answer(gen, completion_model, display_mode)
        except Exception:
            # Fallback: join step results
            self.final_response = "\n\n".join(
                f"✓ {self.plan.steps[o.step_id - 1].description}: {o.result[:200]}"
                for o in self.outcomes
                if o.success and o.step_id <= len(self.plan.steps)
            ) or "작업 완료"

        return MachineState.DONE

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _execute_step(self, step: PlanStep, params: Dict[str, Any]) -> StepOutcome:
        """Execute one plan step via the tool registry."""
        from buildup.tool_registry import execute_tool, ToolContext

        ctx = ToolContext(
            cfg=self._cfg,
            session=self._session,
            logger=self._logger,
            mode=self._mode,
            last_file=self._last_file,
            active_paper_id=self._active_paper_id,
            approved_capabilities=self._approved_capabilities,
        )

        t0 = time.monotonic()
        try:
            result, new_last_file = execute_tool(step.action, params, ctx)
            duration_ms = int((time.monotonic() - t0) * 1000)
            if new_last_file:
                self._last_file = new_last_file
            success = not _is_tool_error(result)
        except Exception as exc:
            result = f"[오류] {exc}"
            duration_ms = int((time.monotonic() - t0) * 1000)
            success = False
            self._logger.warning("Step %d tool exception: %s", step.step_id, exc)

        if self._tracer:
            self._tracer.add_step(
                step.step_id, step.action, params,
                result, success, duration_ms,
            )

        return StepOutcome(
            step_id=step.step_id,
            success=success,
            result=result,
            duration_ms=duration_ms,
        )

    def _resolve_templates(self, params: Dict[str, Any], current_step_id: int = 0) -> Dict[str, Any]:
        """Replace {{step_N_result}} placeholders with actual outcomes.

        Placeholders referencing the current or future steps are left as-is and
        logged as warnings — they indicate a bad plan (circular/forward reference).
        """
        resolved: Dict[str, Any] = {}
        for k, v in params.items():
            if isinstance(v, str) and "{{" in v:
                def _replacer(m: re.Match) -> str:
                    n = int(m.group(1))
                    if n >= current_step_id:
                        if self._logger:
                            self._logger.warning(
                                "Planner bug: step %d references {{step_%d_result}} "
                                "(circular or forward reference) — placeholder kept as-is",
                                current_step_id, n,
                            )
                        return m.group(0)
                    for o in self.outcomes:
                        if o.step_id == n:
                            return o.result
                    return m.group(0)
                v = re.sub(r"\{\{step_(\d+)_result\}\}", _replacer, v)
            resolved[k] = v
        return resolved

    def _transition(self, new_state: MachineState) -> None:
        if self._logger:
            self._logger.debug(
                "StateMachine: %s → %s", self.state.name, new_state.name
            )
        self.state = new_state
