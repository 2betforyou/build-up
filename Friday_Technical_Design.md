# Friday 기술 설계서  
## Semantic Routing + Policy-Gated Workflow Runtime

## 1. 문서 목적

본 문서는 Friday를 단순 대화형 챗봇이 아니라, **연구 및 엔지니어링 작업을 안전하고 재현 가능하게 수행하는 개인 assistant runtime**으로 구축하기 위한 핵심 기술 설계를 정의한다.

특히 다음 문제를 해결하는 것을 목표로 한다.

- 자연어 요청을 단순 rule-based 없이 해석하고 싶다.
- 그렇다고 완전 자율 agent처럼 불안정한 시스템은 원하지 않는다.
- 도구 실행은 통제 가능해야 하며, 실패 원인 추적이 가능해야 한다.
- 모델, 도구, 권한, 메모리를 분리된 계층으로 설계하고 싶다.
- 향후 사용 로그를 기반으로 라우터를 점진적으로 고도화할 수 있어야 한다.

본 설계는 이를 위해 다음 철학을 채택한다.

> Friday는 “자연어 명령어 해석기”가 아니라,  
> **의도 구조화 + 정책 기반 workflow compiler + 안전한 실행 런타임**이다.

---

## 2. 설계 목표

### 2.1 핵심 목표

1. **유연한 자연어 해석**
   - 표현이 조금 달라져도 동일 작업으로 인식할 수 있어야 한다.
   - 단순 키워드 매칭 기반 라우팅을 지양한다.

2. **정책 기반 제어**
   - 어떤 요청이든 먼저 “무엇을 할 수 있는가”를 결정해야 한다.
   - 툴 선택보다 권한 범위를 먼저 제한한다.

3. **재현 가능성**
   - 같은 입력에서 어떤 구조화 결과가 나왔고, 어떤 툴을 골랐고, 무엇을 실행했는지 추적 가능해야 한다.

4. **안전한 실행**
   - 파일 쓰기, 쉘 실행, 외부 연동은 명확한 경계 안에서만 허용한다.

5. **점진적 고도화 가능성**
   - 초기는 LLM + schema + retrieval 기반으로 시작하되,
   - 나중에는 축적된 실행 로그를 바탕으로 router/policy를 학습 가능해야 한다.

### 2.2 비목표

다음은 초기 MVP 범위에서 제외한다.

- 완전 자율 장기 agent loop
- 무제한 tool browsing
- 고급 멀티에이전트 협업
- 음성 비서
- 범용 웹 자동화
- 외부 SaaS 대규모 connector 통합
- 사용자 전체 시스템 권한 접근

---

## 3. 시스템 개요

Friday는 다음 6개 계층으로 구성된다.

1. **Input Layer**
   - 사용자의 자연어 요청 수신

2. **Intent Structuring Layer**
   - 자연어를 구조화된 작업 표현으로 변환

3. **Routing & Policy Layer**
   - 허용 가능한 capability set 및 interaction mode 결정

4. **Planning Layer**
   - 허용된 툴 집합 내에서 실행 가능한 계획 생성

5. **Execution Layer**
   - 실제 툴 호출, 파일 조작, 로그 읽기, 제한적 쉘 실행

6. **Trace & Memory Layer**
   - 모든 중간 상태와 결과를 기록하고, 이후 검색/학습에 활용

---

## 4. 핵심 아키텍처 원칙

### 4.1 자연어를 바로 명령어로 변환하지 않는다

사용자 입력을 곧바로 bash, Python, 함수 호출로 매핑하면 다음 문제가 생긴다.

- 표현 변화에 취약
- 정책 분리가 어려움
- 오작동 시 디버깅 곤란
- 재현성이 낮음
- 고위험 동작을 사전에 차단하기 어려움

따라서 Friday는 다음 중간 표현을 필수로 둔다.

**자연어 → 구조화 의도(JSON) → 정책 게이팅 → 작업 DSL → 실행**

### 4.2 Planner와 Policy를 분리한다

Planner는 “무엇을 어떻게 할지”를 고민하는 계층이고,  
Policy는 “어디까지 허용할지”를 결정하는 계층이다.

이 둘을 섞으면 다음과 같은 문제가 생긴다.

- 라우터가 위험한 실행까지 함께 판단
- explainability 저하
- 테스트 어려움
- 보안 정책 일관성 붕괴

따라서 설계상 다음을 강제한다.

- **Policy가 먼저 capability set을 결정**
- **Planner는 capability set 안에서만 계획 가능**

### 4.3 자유 생성보다 제약된 생성

Friday는 완전 자유형 agent를 지향하지 않는다.  
대신 다음을 사용한다.

- enum 기반 intent class
- schema-constrained JSON
- tool registry 기반 제한된 툴 선택
- 내부 DSL 기반 계획 표현
- state machine 기반 실행 흐름

### 4.4 낮은 확신도일수록 보수적으로 동작

라우터가 애매할 때는 과감해지는 것이 아니라 더 보수적이어야 한다.

예:
- confidence 높음 → inspect + draft 허용
- confidence 중간 → inspect only
- confidence 낮음 → read-only exploratory response

이 원칙은 Friday의 안정성을 좌우한다.

---

## 5. 주요 컴포넌트 설계

### 5.1 Input Layer

#### 역할
- 사용자 자연어 요청 수신
- 세션 정보와 현재 project context 결합
- 이전 대화 및 최근 실행 로그를 참조 가능하게 구성

#### 입력 예시
- “exp3 로그 보고 왜 죽었는지 분석해줘”
- “에러 원인 찾고 patch 초안까지 만들어줘”
- “논문 요약하고 rebuttal 포인트 정리해줘”

#### 출력
- `RawUserRequest`

```json
{
  "request_id": "req_20260403_001",
  "timestamp": "2026-04-03T15:12:00+09:00",
  "session_id": "sess_001",
  "project_id": "fl_bench",
  "user_text": "exp3 로그 보고 왜 죽었는지 분석해줘"
}
```

### 5.2 Intent Structuring Layer

#### 역할
사용자의 자연어 요청을 구조화된 작업 표현으로 바꾼다.

이 단계에서는 아직 툴을 선택하지 않는다.  
핵심은 다음 항목을 추출하는 것이다.

- intent
- entities
- constraints
- desired output
- risk hints
- confidence

#### 권장 구현
- fast model 또는 main model 사용
- JSON schema 강제
- enum 기반 intent 제한
- 자유 텍스트 최소화

#### 예시 intent enum

```text
qa
search
summarize
inspect_logs
inspect_code
code_edit
document_write
experiment_run
debug
workflow_multi_step
```

#### 출력 스키마

```json
{
  "intent": "debug",
  "interaction_mode": "workflow",
  "entities": {
    "project": "FL-bench",
    "artifact": "exp3 logs",
    "target_script": "experiments/exp3_main_results.py"
  },
  "constraints": {
    "allow_write": true,
    "allow_exec": false,
    "workspace_only": true
  },
  "desired_outputs": [
    "failure_cause",
    "patch_draft"
  ],
  "confidence": 0.86,
  "alternate_intents": [
    {"intent": "inspect_logs", "score": 0.09},
    {"intent": "code_edit", "score": 0.05}
  ]
}
```

#### 설계 포인트
- `confidence`는 반드시 남긴다.
- `alternate_intents`를 함께 남겨 후속 분석과 개선에 활용한다.
- `interaction_mode`는 툴 선택보다 먼저 정한다.

#### interaction_mode enum

```text
consult
inspect
draft
execute
workflow
```

### 5.3 Exemplar Retrieval Layer

#### 역할
구조화된 의도와 유사한 과거 작업 템플릿 또는 exemplar를 검색한다.

이 계층은 rule-based 대체의 핵심 보조 장치다.  
자연어 표현이 달라도 비슷한 작업이라면 유사한 실행 패턴을 재사용할 수 있다.

#### 검색 대상
- 이전 successful execution traces
- hand-crafted workflow exemplars
- project-specific task templates

#### exemplar 예시

```json
{
  "exemplar_id": "wf_debug_logs_patch_001",
  "intent": "debug",
  "interaction_mode": "workflow",
  "typical_steps": [
    "find_recent_logs",
    "read_log_tail",
    "extract_traceback",
    "search_related_code",
    "draft_patch_if_confident"
  ],
  "required_tools": [
    "find_logs",
    "read_file",
    "search_code",
    "patch_draft"
  ]
}
```

#### retrieval 방식
- user text + structured intent를 embedding으로 인코딩
- exemplar DB에서 nearest-neighbor 검색
- top-k 후보 반환
- 필요시 reranker 추가

#### 출력 예시

```json
{
  "retrieved_exemplars": [
    {
      "exemplar_id": "wf_debug_logs_patch_001",
      "score": 0.92
    },
    {
      "exemplar_id": "wf_debug_code_only_003",
      "score": 0.81
    }
  ]
}
```

### 5.4 Policy Gate Layer

#### 역할
이 요청이 어떤 수준의 권한과 도구를 사용할 수 있는지 결정한다.

이 계층은 Friday 설계에서 가장 중요하다.  
의도 파악이 맞더라도, 정책상 금지된 행동은 허용되면 안 된다.

#### 입력
- structured intent
- interaction mode
- confidence
- retrieved exemplars
- project policy
- system policy

#### 출력
- allowed capability set
- forbidden operations
- confirmation requirement
- execution scope

#### capability 예시

```text
read_files
search_code
summarize
draft_write
workspace_write
safe_shell
dangerous_shell
external_send
delete_files
```

#### 정책 예시

- `consult` mode → `summarize`만 허용
- `inspect` mode → `read_files`, `search_code`
- `draft` mode → `draft_write`, `workspace_write`
- `execute` mode → `safe_shell`
- `dangerous_shell`, `external_send`, `delete_files` → 기본 차단

#### confidence 기반 보수성 정책

| confidence | 정책 |
|---|---|
| >= 0.85 | inspect + draft 허용 |
| 0.60 ~ 0.85 | inspect 중심, 쓰기 제한적 허용 |
| < 0.60 | read-only exploratory mode |

#### 출력 예시

```json
{
  "allowed_capabilities": [
    "read_files",
    "search_code",
    "summarize",
    "draft_write",
    "workspace_write"
  ],
  "forbidden_capabilities": [
    "dangerous_shell",
    "external_send",
    "delete_files"
  ],
  "confirmation_required": false,
  "execution_scope": {
    "read_roots": [
      "/workspace/projects/FL-bench",
      "/workspace/logs"
    ],
    "write_roots": [
      "/workspace/output",
      "/workspace/scratch"
    ]
  }
}
```

### 5.5 Model Routing Layer

#### 역할
`StructuredIntent`의 신호를 바탕으로 planning에 사용할 모델을 **점수 기반으로 결정**한다.

LLM이 또 다른 LLM을 고르는 구조는 오버킬이다.  
이미 구조화된 신호(intent, mode, confidence)가 있으므로 deterministic scoring으로 충분하다.

#### 구현: `model_router.py`

```python
select_model(structured_intent, cfg) → RoutingDecision(model, tier, score, reasons)
```

#### 점수 계산

| 신호 | 예시 | 점수 |
|---|---|---|
| intent | `qa`, `search` | -2 |
| intent | `file_manage`, `calendar` | -1 |
| intent | `document_write`, `experiment_run` | +2 |
| intent | `code_edit`, `debug` | +3 |
| intent | `workflow_multi_step` | +4 |
| mode | `consult` | -2 |
| mode | `inspect` | -1 |
| mode | `draft` | 0 |
| mode | `execute` | +1 |
| mode | `workflow` | +3 |
| confidence ≥ 0.85 | 높은 확신 → 단순 작업 가능성 | -1 |
| confidence < 0.65 | 낮은 확신 → 보수적 판단 | +2 |

#### 모델 선택 규칙

```
StructuredIntent == None  →  main  (보수적 fallback)
score >= _main_threshold  →  main
code intent + score >= _coder_threshold  →  coder
그 외  →  fast
```

#### 가중치 config 분리

가중치는 코드에 박지 않고 `config.py`의 `ROUTING_WEIGHTS` 딕셔너리에서 관리한다.  
trace 데이터가 쌓이면 이 값만 보정하면 된다.

```python
ROUTING_WEIGHTS: Dict[str, int] = {
    "intent:qa": -2,
    "intent:code_edit": 3,
    "mode:workflow": 3,
    "confidence:high": -1,
    "confidence:low": 2,
    "_main_threshold": 3,
    "_coder_threshold": 3,
}
```

#### RoutingDecision trace 기록

어떤 입력이 어떤 점수/이유로 어떤 모델에 라우팅됐는지 trace에 남긴다.

```json
{
  "routing": {
    "model": "gemma4:e4b",
    "tier": "fast",
    "score": -1,
    "reasons": ["intent:file_manage=-1", "mode:draft=0", "confidence:high(0.92)=-1"]
  }
}
```

### 5.6 Planner Layer

#### 역할
허용된 툴 집합 안에서 실행 가능한 계획을 생성한다.

Planner는 자유로운 chain-of-thought agent가 아니라,  
**typed step list를 생성하는 constrained planner**여야 한다.

#### 구현 위치

별도 `planner.py` 모듈 없이 `state_machine.py`의 `_handle_planning` 상태에 직접 구현한다.  
Planning은 LLM 1회 호출 → JSON 파싱 → 검증의 단순한 흐름이므로 별도 모듈 분리는 과설계다.

#### Planning 모델 선택

`_handle_planning` 진입 시 `model_router.select_model()`을 호출해 모델을 결정한다.  
Planning 자체가 단순한 경우(file_manage, calendar 등)는 fast model로 처리하고,  
복잡한 다단계 작업(workflow, debug 등)만 main model을 사용한다.

#### Thinking 처리

Planning LLM 응답 중 `<think>` 블록은 사용자에게 출력하지 않는다.  
JSON plan 본문은 버퍼에 수집해 파싱에 사용하며, parser 단계에서 think tag를 제거한다.  
→ 내부 추론이 최종 출력이나 plan JSON으로 섞이는 것을 방지한다.

#### 출력 형식

```json
{
  "rationale": "한 문장 이하 간결한 이유",
  "steps": [
    {
      "action": "도구_이름",
      "params": {"relpath": "todo.md"},
      "description": "이 단계의 설명",
      "condition": "step_1.success"
    }
  ]
}
```

#### Planner 제약
- `action`은 반드시 tool registry에 등록된 이름이어야 한다
- 최대 8단계. 단순 요청은 1-2단계
- `answer` 도구 사용 금지
- 이전 단계 결과가 필요하면 `{{step_N_result}}` 플레이스홀더 사용
- 모호한 부분은 가장 합리적인 해석으로 즉시 결정

#### PlanningError fallback

Planning 실패 시(`PlanningError`) 예외를 호출자(`run_agent`)로 전파한다.  
`run_agent`는 이를 받아 legacy ReAct 루프로 자동 fallback한다.

```
PlanningError 발생
  → state_machine raise
  → agent.py except PlanningError
  → run_agent_legacy() (ReAct loop)
```

### 5.7 Tool Registry

#### 역할
Friday가 사용할 수 있는 모든 도구를 typed metadata와 함께 등록한다.

#### 필수 필드
- tool name
- description
- input schema
- output schema
- risk level
- capability requirement
- executor function

#### 예시

```json
{
  "name": "read_file",
  "description": "Read a text file within allowed workspace",
  "risk_level": "low",
  "required_capability": "read_files",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "tail_lines": {"type": "integer"}
    },
    "required": ["path"]
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "content": {"type": "string"},
      "path": {"type": "string"}
    }
  }
}
```

#### risk level enum

```text
low
medium
high
critical
```

#### 초기 MVP tool set 추천

- `read_file`
- `find_logs`
- `search_code`
- `list_dir`
- `extract_traceback`
- `summarize_text`
- `write_file`
- `patch_draft`
- `run_shell_safe`

### 5.8 Execution Layer

#### 역할
Planner가 만든 step들을 순차적으로 실행한다.

#### 실행 원칙
- 각 step 실행 전 policy 재검사 가능
- 모든 step 결과 저장
- 실패 시 명확한 error object 반환
- side effect가 있는 step은 별도 표시
- shell 명령은 allowlist 기반

#### shell 실행 정책 예시

허용:
- `ls`
- `cat`
- `grep`
- `tail`
- `head`
- `python <safe internal script>`
- `git diff`
- `git status`

기본 금지:
- `rm`
- `sudo`
- `curl | bash`
- 임의 외부 네트워크 호출
- 임의 패키지 설치
- credential 접근

#### step 실행 결과 예시

```json
{
  "step_id": "s2",
  "status": "success",
  "tool": "read_file",
  "started_at": "2026-04-03T15:13:02+09:00",
  "finished_at": "2026-04-03T15:13:03+09:00",
  "output": {
    "path": "/workspace/logs/exp3.log",
    "content_preview": "Traceback (most recent call last)..."
  }
}
```

#### 실패 예시

```json
{
  "step_id": "s4",
  "status": "failed",
  "tool": "search_code",
  "error": {
    "type": "FileNotFound",
    "message": "Target path does not exist"
  }
}
```

### 5.9 Trace & Logging Layer

#### 역할
Friday의 모든 의사결정과 실행 결과를 기록한다.

이 레이어는 단순 디버깅용이 아니다.  
향후 라우터 고도화의 핵심 데이터 자산이다.

#### 반드시 기록할 항목

- raw user input
- structured intent (intent, mode, confidence, entities)
- **routing decision (model, tier, score, reasons)**
- policy decision
- plan (plan_id, steps)
- executed steps (action, params, result, duration_ms)
- failures
- final response preview
- success flag

#### 저장 포맷
- JSONL (`~/.friday_traces.jsonl`)
- 향후: trace DB + vector index

#### trace 구조 (`DispatchTrace`)

```json
{
  "trace_id": "a3f2c8d1e4b5",
  "timestamp": "2026-04-04T22:03:14",
  "user_input": "todo.md 파일에 표를 추가해줘",
  "tier": "2",
  "structured_intent": {
    "intent": "file_manage",
    "interaction_mode": "draft",
    "confidence": 0.91
  },
  "routing": {
    "model": "gemma4:e4b",
    "tier": "fast",
    "score": -1,
    "reasons": ["intent:file_manage=-1", "mode:draft=0", "confidence:high(0.91)=-1"]
  },
  "plan_trace": {
    "plan_id": "b7c3a1",
    "steps_planned": 2,
    "steps_executed": 2,
    "requires_confirmation": false
  },
  "steps": [
    {"step": 1, "action": "read_file", "success": true, "duration_ms": 12},
    {"step": 2, "action": "write_file", "success": true, "duration_ms": 8}
  ],
  "success": true,
  "duration_ms": 4820
}
```

#### routing trace 활용

trace에 남긴 `routing.score`와 `routing.reasons`를 집계하면:
- 어떤 intent가 실제로 fast로 충분했는지
- 어떤 경우에 main이 필요했는지
- 가중치 보정 방향을 데이터 기반으로 결정 가능

### 5.10 Memory Layer

#### 역할
세션 문맥, 프로젝트 문맥, 실행 이력을 분리 저장한다.

#### 메모리 종류

1. **Session Memory**
   - 현재 대화 중 필요한 단기 문맥

2. **Project Memory**
   - 특정 프로젝트 구조, 파일 위치, 자주 쓰는 명령, 주요 스크립트

3. **Execution Memory**
   - 과거 실행 결과, 실패 패턴, 성공 workflow

4. **Preference Memory**
   - 사용자의 작업 선호도

#### 분리 이유
메모리를 하나로 합치면 오염된다.  
프로젝트 문맥과 사용자 취향과 일회성 실행 로그는 성격이 전혀 다르다.

---

## 6. 데이터 스키마 제안

### 6.1 Structured Intent Schema

```json
{
  "intent": "debug",
  "interaction_mode": "workflow",
  "entities": {
    "project": "string",
    "artifact": "string",
    "target_files": ["string"]
  },
  "constraints": {
    "allow_write": true,
    "allow_exec": false,
    "workspace_only": true
  },
  "desired_outputs": ["string"],
  "confidence": 0.0,
  "alternate_intents": [
    {"intent": "string", "score": 0.0}
  ]
}
```

### 6.2 Policy Result Schema

```json
{
  "allowed_capabilities": ["string"],
  "forbidden_capabilities": ["string"],
  "confirmation_required": false,
  "execution_scope": {
    "read_roots": ["string"],
    "write_roots": ["string"]
  }
}
```

### 6.3 Plan Schema

```json
{
  "plan_id": "string",
  "goal": "string",
  "steps": [
    {
      "step_id": "string",
      "tool": "string",
      "args": {}
    }
  ]
}
```

### 6.4 Tool Metadata Schema

```json
{
  "name": "string",
  "description": "string",
  "risk_level": "low",
  "required_capability": "string",
  "input_schema": {},
  "output_schema": {}
}
```

---

## 7. 실행 상태 머신

Friday Tier-2는 ReAct 자유 루프 대신 **명시적 FSM(PlanStateMachine)**을 사용한다.

### 3-Tier Dispatch 개요

```
사용자 입력
  │
  ├─ Tier-0: is_chat() → answer_query_stream (스트리밍)
  ├─ Tier-1A: parse_intent_instant() → 즉시 실행 (0 LLM 호출)
  ├─ Tier-1B: EmbedClassifier → intent + confidence → 즉시 실행
  └─ Tier-2: PlanStateMachine (아래 FSM)
```

### FSM 상태 전이

```
IDLE
  → STRUCTURING   자연어 → StructuredIntent (struct_model)
  → PLANNING      model_router → plan_model 선택 → LLM 1회 → ExecutionPlan
  → VALIDATING    plan 검증 및 라우팅
  → CONFIRMING    confirm-always / confirm-git 스텝 존재 시 사용자 승인
  → EXECUTING     스텝 순차 실행
  → RETRYING      스텝 실패 + 재시도 남음
  → COMPLETING    fast_model로 결과 스트리밍 요약
  → DONE

실패 경로:
  PLANNING   → PlanningError raise → agent.py에서 legacy ReAct fallback
  EXECUTING  → FAILED (최대 재시도 초과)
  CONFIRMING → ABORTED (사용자 거부)
```

### 상태 설명

| 상태 | 역할 | 사용 모델 |
|---|---|---|
| `STRUCTURING` | 자연어 → StructuredIntent JSON | `struct_model` (qwen2.5:1.5b) |
| `PLANNING` | model_router로 모델 결정 → ExecutionPlan 생성. thinking 회색 스트리밍 | `model_router` 결정 |
| `VALIDATING` | confirmation 필요 여부 라우팅 | — |
| `CONFIRMING` | confirm 필요한 스텝 사용자 승인 요청 | — |
| `EXECUTING` | 스텝 순차 실행, `{{step_N_result}}` 치환 | — |
| `RETRYING` | 스텝 실패 시 재시도 (max_retries까지) | — |
| `COMPLETING` | 전체 결과 자연어 요약, 스트리밍 출력 | `fast_model` |

### Thinking 처리

PLANNING 단계에서 LLM의 `<think>` 블록은 회색 thinking 채널에 분리 출력한다.  
JSON plan 본문은 버퍼에 수집해 파싱하며, think tag는 제거한다.  
COMPLETING 단계 요약도 `render_streaming_answer`로 실시간 출력하되 thinking을 최종 답변으로 채택하지 않는다.

### PlanningError Fallback

```
PlanningError (LLM 오류, JSON 파싱 실패, 알 수 없는 도구)
  → state_machine이 예외를 raise
  → agent.run_agent() except PlanningError
  → run_agent_legacy() (원본 ReAct 루프)
```

FRIDAY_USE_STATE_MACHINE=0 환경변수로 FSM을 완전히 우회할 수 있다.

### 장점
- 단계별 테스트 가능
- 실패 위치 명확
- 사용자에게 계획 미리 표시 가능 (CONFIRMING)
- agent hallucination 감소 (tool registry 검증)

---

## 8. 실패 처리 전략

### 8.1 종류

1. **Intent ambiguity**
   - 요청이 애매함
   - 대응: inspect-only conservative plan

2. **Policy violation**
   - 허용되지 않은 action 요구
   - 대응: 차단 후 대안 제안

3. **Tool execution failure**
   - 파일 없음, 명령 실패 등
   - 대응: step fail 기록 후 가능한 범위 내 요약

4. **Planning failure**
   - 적절한 tool 조합 생성 실패
   - 대응: simpler fallback plan

5. **Low confidence routing**
   - intent 불확실
   - 대응: read-only mode downgrade

---

## 9. 보안 및 안전성 설계

### 9.1 파일 시스템 경계
Friday는 지정된 workspace 밖의 쓰기를 금지한다.

예:
- 읽기 허용: `/workspace/projects`, `/workspace/logs`
- 쓰기 허용: `/workspace/output`, `/workspace/scratch`
- 금지: 홈 디렉터리 전체, 시스템 디렉터리, credential path

### 9.2 shell allowlist
실행 가능 명령은 명시된 allowlist로 제한한다.

### 9.3 destructive action 차단
초기 MVP에서 삭제/이동/외부 전송은 기본 금지한다.

### 9.4 credential isolation
`.env`, SSH key, API key file 등은 기본 접근 금지다.

### 9.5 auditability
모든 실행과 side effect는 trace에 남아야 한다.

---

## 10. 모델 구성 전략

### 10.1 모델 역할 분리

| 모델 | 환경변수 | 기본값 | 역할 |
|---|---|---|---|
| `struct_model` | `FRIDAY_STRUCT_MODEL` | `qwen2.5:1.5b` | Intent structuring (JSON 변환) |
| `fast_model` | `FRIDAY_FAST_MODEL` | `gemma4:e4b` | 단순 planning, chat, 결과 요약 |
| `main_model` | `FRIDAY_MAIN_MODEL` | `gemma4:31b` | 복잡한 planning, 분석, 추론 |
| `night_model` | `FRIDAY_NIGHT_MODEL` | `qwen3.6:27b` | FRIDAY Night 논문 독해 |
| `reviewer_model` | `FRIDAY_REVIEWER_MODEL` | `deepseek-r1:32b` | FRIDAY Night reviewer pass |
| `coder_model` | `FRIDAY_CODER_MODEL` | `qwen3-coder-30b-instruct` | 코드 수정, 디버깅, 패치 생성 |
| `embed_model` | `FRIDAY_EMBED_MODEL` | `nomic-embed-text` | Exemplar retrieval embedding |

### 10.2 Deterministic Scoring Router

자연어 입력은 `struct_model`로 구조화 후, `model_router.select_model()`이 점수 기반으로 planning 모델을 결정한다.  
추가 LLM 호출 없이 이미 구조화된 신호만으로 결정하므로 빠르고 예측 가능하다.

```
자연어 입력
  → struct_model → StructuredIntent (intent, mode, confidence)
  → model_router.select_model()
      StructuredIntent == None → main (보수적 fallback)
      score 계산 (intent 점수 + mode 점수 + confidence 보정)
      score >= _main_threshold(3) → main
      code intent + score >= _coder_threshold(3) → coder
      그 외 → fast
  → planning
```

### 10.3 가중치 관리

가중치는 `config.py`의 `ROUTING_WEIGHTS` 딕셔너리에서 관리한다.  
코드를 건드리지 않고 값만 수정하면 라우팅 동작이 바뀐다.  
trace에 routing 결과가 기록되므로 실제 사용 데이터를 보고 점진적으로 보정할 수 있다.

### 10.4 Timeout 설정

Ollama 연결 실패를 빠르게 감지하기 위해 connect timeout과 read timeout을 분리한다.

```python
timeout = (ollama_connect_timeout, ollama_timeout)
#          ↑ 10초 (TCP 연결)    ↑ 600초 (응답 대기)
```

connect timeout이 짧으면 Ollama가 죽어있을 때 수십 분 대기 없이 즉시 실패한다.

---

## 11. 초기 구현 우선순위

### Phase 1: Core Routing MVP
구현 범위:
- structured intent JSON
- exemplar retrieval
- policy gate
- basic planner
- read-only tools
- JSONL logging

### Phase 2: Drafting Capability
추가:
- write_file
- patch_draft
- project memory
- confidence-based downgrade

### Phase 3: Safe Execution
추가:
- run_shell_safe
- execution verification
- rollback-like safeguards
- richer trace DB

### Phase 4: Adaptive Improvement
추가:
- trace replay
- routing evaluation set
- learned router / reranker
- project-specific policy tuning

---

## 12. 평가 지표

### 12.1 Routing 품질
- intent accuracy
- interaction_mode accuracy
- tool-family accuracy
- capability gating correctness

### 12.2 Planner 품질
- valid plan rate
- allowed-tool compliance rate
- step completion rate

### 12.3 실행 품질
- task success rate
- execution failure rate
- unsafe action block rate

### 12.4 운영 품질
- trace completeness
- reproducibility
- average latency

---

## 13. 예시 워크플로

### 입력
“exp3 로그 보고 왜 죽었는지 찾고, 수정 가능하면 patch 초안도 만들어줘”

### 1단계: intent structuring

```json
{
  "intent": "debug",
  "interaction_mode": "workflow",
  "entities": {
    "artifact": "exp3 logs"
  },
  "constraints": {
    "allow_write": true,
    "allow_exec": false,
    "workspace_only": true
  },
  "desired_outputs": ["failure_cause", "patch_draft"],
  "confidence": 0.88
}
```

### 2단계: policy gate

```json
{
  "allowed_capabilities": [
    "read_files",
    "search_code",
    "summarize",
    "draft_write",
    "workspace_write"
  ]
}
```

### 3단계: plan

```json
{
  "goal": "Diagnose exp3 failure and prepare patch draft",
  "steps": [
    {"tool": "find_logs", "args": {"pattern": "exp3"}},
    {"tool": "read_file", "args": {"path_from_previous": "latest_log"}},
    {"tool": "extract_traceback", "args": {}},
    {"tool": "search_code", "args": {"query_from_previous": "symbols"}},
    {"tool": "patch_draft", "args": {"condition": "only_if_confident"}}
  ]
}
```

### 4단계: execution
- 로그 읽기
- traceback 추출
- 관련 코드 검색
- 수정 초안 생성

### 5단계: summarize
- 실패 원인
- 관련 코드 위치
- patch 초안 경로
- 실행은 하지 않았음을 명시

---

## 14. 구현상 강한 권고

1. **자연어→bash 직접 변환은 하지 않는다.**
2. **Policy Gate를 Router보다 뒤가 아니라 앞에 둔다.**
3. **Planner는 allowed tool set 밖을 절대 건드리지 못하게 한다.**
4. **모든 중간 산출물을 trace로 남긴다.**
5. **초기에는 자유 agent 대신 state machine을 고수한다.**
6. **좋은 라우터보다 좋은 로그가 먼저다.**
7. **MVP 단계에서 rule-based를 완전히 없애려 하지 말고, 정책 엔진에는 명시 규칙을 사용한다.**
   - 자연어 이해는 semantic하게
   - 안전 정책은 명시적으로

이 마지막이 중요합니다.  
**rule-based를 버려야 하는 곳은 의도 해석이지, 보안 정책이 아닙니다.**  
보안 정책은 오히려 명시적이어야 합니다.

---

## 15. 최종 결론

Friday의 기술 방향은 다음과 같이 정의한다.

> Friday는 자연어 입력을 구조화된 의도로 변환하고,  
> deterministic scoring router가 작업 복잡도에 맞는 모델을 선택한 뒤,  
> FSM 기반 constrained planner로 실행 계획을 생성하고,  
> typed tool registry를 통해 안전하게 수행하며,  
> 전 과정(routing 근거 포함)을 trace로 기록하는 연구자용 assistant runtime이다.

이 시스템의 정체성은 다음 한 문장으로 요약된다.

> **Semantic routing + deterministic model selection + policy-gated planning + auditable execution**

### 구현된 핵심 설계 결정

| 결정 | 이유 |
|---|---|
| struct_model → scoring router → plan_model | LLM이 LLM을 고르는 오버킬 방지 |
| 가중치를 `ROUTING_WEIGHTS` config로 분리 | 코드 수정 없이 trace 기반 보정 가능 |
| PlanningError → legacy ReAct fallback | 플래너 실패해도 작업 완료 가능 |
| planning thinking 스트리밍 (회색) | 긴 reasoning 동안 사용자 대기 경험 개선 |
| connect/read timeout 분리 | Ollama 다운 시 즉시 실패, 정상 시 충분한 대기 |
| routing 결과를 trace에 기록 | 데이터 기반 가중치 보정 경로 확보 |
