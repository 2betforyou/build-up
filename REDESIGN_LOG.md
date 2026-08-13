# Friday 재설계 로그

> 작성일: 2026-04-04  
> 대상 버전: FridayLocal / router/friday/

---

## 개요

두 가지 작업을 동시에 진행했다.

1. **Tier-2 에이전트 재설계** — 기존 ReAct 루프를 State Machine + Constrained Planner로 교체
2. **세션 관리** — 세션 간 대화 내역 영속성 및 세션 전환 기능 추가

---

## 1. State Machine + Constrained Planner

### 배경

기존 Tier-2 (`run_agent()`)는 매 스텝마다 LLM이 다음 행동을 결정하는 ReAct 루프였다.

```
User input → LLM → {"thought":…, "action":…, "params":…} → 도구 실행 → 반복 (최대 8회)
```

**문제점:**
- 실행 전에 무엇을 할지 알 수 없음 → 사용자에게 계획 미리 보여주기 불가
- 스텝 실패 시 복구 전략 없음
- 정책 게이트가 실행 중에 적용됨
- LLM이 존재하지 않는 도구를 환각할 수 있음
- 단위 테스트 불가 (LLM 없이는 테스트 안 됨)

### 새 아키텍처

3-tier dispatch는 그대로 유지. **Tier-2만 교체.**

```
Tier 0: chat      → 변경 없음
Tier 1A: 규칙     → 변경 없음
Tier 1B: 임베딩   → 변경 없음
Tier 2: ReAct     → State Machine + Constrained Planner 로 교체
```

**두 단계로 분리:**

```
[Planning phase]  LLM 1회 호출 → ExecutionPlan (PlanStep 목록) 생성 + 검증
[Execution phase] StateMachine이 계획을 단계별로 결정론적으로 실행
```

### FSM 상태 전이

```
IDLE
  → PLANNING      (사용자 입력 수신)
  → VALIDATING    (plan 생성 완료)
  → CONFIRMING    (confirm-always/confirm-git 스텝 존재 시)
  → EXECUTING     (자동 실행 or 사용자 승인)
  → RETRYING      (스텝 실패 + 재시도 남음)
  → COMPLETING    (전체 스텝 완료 → fast model로 결과 요약)
  → DONE

  실패 경로:
  PLANNING   → FAILED  (LLM 오류, JSON 파싱 실패, 알 수 없는 도구)
  EXECUTING  → FAILED  (최대 재시도 초과)
  CONFIRMING → ABORTED (사용자 거부)
```

### 계획 표시 (CONFIRMING 상태)

confirm이 필요한 스텝이 있을 때 실행 전에 계획을 보여준다:

```
┌─ 계획 (3단계) ─────────────────────────────────────────────────┐
│ 파일을 읽고 수정 지시에 따라 덮어쓴다                            │
│                                                                 │
│  1.  read_file    notes.md 파일 현재 내용 확인                  │
│  2.  rewrite_file 수정된 내용으로 파일 덮어쓰기      ⚠ confirm  │
│  3.  (완료 메시지 생성은 fast model이 담당)                      │
└─────────────────────────────────────────────────────────────────┘

실행하시겠습니까? (y/n)
```

### PlaceholderTemplate

데이터 의존 스텝에서 LLM 환각을 방지하기 위해 `{{step_N_result}}` 플레이스홀더를 지원한다.

```json
{
  "action": "write_file",
  "params": {"relpath": "output.md", "content": "{{step_1_result}}"},
  "description": "검색 결과를 파일로 저장"
}
```

실행 시 `_resolve_templates()`가 이전 스텝 결과로 치환한다.

### Fallback

`PlanningError` 발생 시 또는 `FRIDAY_USE_STATE_MACHINE=0` 환경변수 설정 시 기존 ReAct 루프(`run_agent_legacy()`)로 자동 fallback된다.

---

## 2. 세션 관리

### 배경

기존 `ConversationHistory`는 메모리에만 존재했다. 프로세스 종료 시 대화 내역이 소멸되고, 이전 세션으로 돌아가는 방법이 없었다.

### 설계

- **저장 위치:** `~/FridayLocal/sessions/<session_id>.session.json`
- **Auto-save:** `ConversationHistory.add()` 호출마다 자동 저장 (on_change 훅)
- **시작 시:** 가장 최근 세션 자동 복원
- **전환:** `/session load <id>` 명령으로 다른 세션으로 이동

### 세션 파일 구조

```json
{
  "session_id": "a3f2c8d1e4b5",
  "created_at": "2026-04-04T10:00:00",
  "updated_at": "2026-04-04T23:10:00",
  "job_id": "20260404-101500-drafts",
  "title": "draft.md 수정해줘",
  "turn_count": 7,
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

### 명령어

| 명령 | 설명 |
|---|---|
| `/session info` | 현재 세션 ID·턴 수 확인 |
| `/session list` | 모든 세션 최신순 목록 |
| `/session load <id>` | 이전 세션으로 전환 (ID 앞부분만 입력 가능) |
| `/session new` | 새 세션 시작 (현재 세션 저장 후) |
| `/session delete <id>` | 세션 삭제 (현재 세션은 삭제 불가) |

---

## 변경 파일 목록

### 신규 생성

| 파일 | 역할 |
|---|---|
| `router/friday/plan.py` | `PlanStep`, `ExecutionPlan`, `StepOutcome`, `PlanningError`, `evaluate_condition()` |
| `router/friday/planner.py` | LLM 1회 호출 → JSON 파싱 + 검증 → `ExecutionPlan` 반환 |
| `router/friday/state_machine.py` | FSM 실행기 (`PlanStateMachine`) |
| `router/friday/session_store.py` | 세션 저장/로드/목록/삭제 (`~/FridayLocal/sessions/`) |

### 수정

| 파일 | 변경 내용 |
|---|---|
| `router/friday/agent.py` | `run_agent()` → `run_agent_legacy()` 리네임; 새 `run_agent()`가 SM → fallback 순으로 실행 |
| `router/friday/trace.py` | `PlanTrace` dataclass 추가; `DispatchTrace`에 `plan_trace` optional 필드 추가 |
| `router/friday/conversation.py` | `set_on_change()`, `load_messages()`, `raw_messages()` 추가 |
| `router/friday/shell.py` | 세션 import; 시작 시 최근 세션 자동 복원; `_autosave_session()`; `/session` 명령 핸들러; readline completer 업데이트 |
| `router/friday/rendering.py` | help text에 세션 명령어 섹션 추가 |

---

## 환경변수

| 변수 | 값 | 효과 |
|---|---|---|
| `FRIDAY_USE_STATE_MACHINE` | `0` | Tier-2를 구 ReAct 루프로 강제 |
| `FRIDAY_USE_STATE_MACHINE` | `1` (기본) | State Machine + Planner 사용 |

---

## 향후 작업 (미완)

- [x] `ExemplarIndex` — `PlanTrace` 형식 트레이스도 학습에 활용되도록 업데이트  
  `_load_successful_traces()`: `plan_trace`만 있고 `steps`가 비어있는 트레이스도 포함.  
  `retrieve()`: SM 트레이스에서 step `description` 필드 활용, `[SM]` 태그 표시.
- [ ] Planner 프롬프트 튜닝 — 실제 사용 로그 기반으로 few-shot 예시 추가
- [x] `/session` 탭 자동완성 — 세션 ID prefix 완성  
  completer에서 `/session load`, `/session delete`, `/session rename` 입력 시 세션 ID 목록 자동완성.
- [x] 세션 제목 편집 명령 (`/session rename <id> <title>`)  
  `session_store.rename_session()` 추가; shell.py `_cmd_session()` 핸들러 연결.
- [x] `context.py` 분리 — `agent.py`의 `_build_context()`를 별도 모듈로 이동해 planner와 공유  
  `router/friday/context.py` 신규 생성; `planner.py`의 순환 임포트(`from friday.agent`) 제거.

---

## 버그 수정

- [x] **Planner parse_failure** — LLM이 `<thought>/<thinking>/<think>` 블록을 JSON 앞에 출력할 때  
  `_extract_json()`이 블록 내부의 `{}`를 먼저 파싱 시도해 실패하는 문제 수정.  
  `planner.py:_extract_json()`에서 think 블록을 정규식으로 제거 후 JSON 스캔.

---

## 인터페이스 영어화

모든 시스템 UI 문자열(상태 메시지, 패널 타이틀, 오류 메시지, help text)을 영어로 전환.  
LLM 시스템 프롬프트 및 LLM 응답 내용은 변경하지 않음.

변경 파일:
- `router/friday/shell.py` — render_info/render_status/console.print 한국어 문자열 전체
- `router/friday/state_machine.py` — 계획 패널, 상태 메시지, 오류 메시지
- `router/friday/agent.py` — 오류 메시지
- `router/friday/rendering.py` — `help_text()` 전체, `render_diff()` "(변경 없음)"
