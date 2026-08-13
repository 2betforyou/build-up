# Friday Project 정리

작성일: 2026-05-20  
기준 위치: `/Users/2betforyou/FridayLocal`

## 1. 프로젝트를 시작한 목적

Friday는 처음부터 단순한 챗봇이라기보다, 로컬에서 연구와 엔지니어링 작업을 실제로 처리하는 개인 assistant runtime을 목표로 시작했다. 핵심 문제의식은 명확했다. 자연어로 편하게 말하고 싶지만, 그 자연어가 곧바로 파일 수정, 셸 실행, Git 명령, 외부 업로드 같은 위험한 실행으로 이어지는 시스템은 불안하다. 반대로 모든 것을 수동 명령으로만 처리하면 개인 비서로서의 장점이 사라진다.

그래서 Friday의 출발점은 세 가지였다.

1. 로컬 우선: 데이터, 세션, 작업 파일, 논문 라이브러리, 실행 로그가 모두 `~/FridayLocal` 안에 남는다.
2. 빠른 일상 작업: 파일 읽기, 일정 조회, 검색, 간단한 질문은 불필요하게 거대한 에이전트 루프를 타지 않는다.
3. 안전한 실행: 쓰기, 삭제, 셸, Git, 외부 연동은 정책과 샌드박스 경계를 먼저 통과해야 한다.

초기 설계 문서인 `Friday_Technical_Design.md`의 표현을 빌리면, Friday는 "자연어 명령어 해석기"가 아니라 "의도 구조화 + 정책 기반 workflow compiler + 안전한 실행 런타임"을 지향한다. 지금 코드도 이 방향으로 자라났다.

## 2. 현재까지 진행된 정도

현재 Friday는 Python 패키지 `router/friday/` 중심으로 구성되어 있고, 핵심 소스는 약 15,000줄 규모다. 단일 스크립트가 아니라 라우팅, 모델 호출, 상태머신, 도구 레지스트리, 논문 처리, 세션, 메모리, 스킬 색인 등이 모듈로 분리되어 있다.

현재 구현된 큰 축은 다음과 같다.

- CLI/REPL 실행: `router/friday/__main__.py`, `router/run_friday.py`, `router/friday/shell.py`
- 3-tier 자연어 디스패치: `shell.py`, `intent.py`, `embed_classifier.py`
- Tier-2 상태머신 플래너: `state_machine.py`, `plan.py`, `agent.py`
- 도구 실행 레이어: `tool_registry.py`
- 정책/샌드박스: `permission.py`, `sandbox.py`, `paths.py`, `git_mgr.py`
- 실행 추적/학습 기반 개선 준비: `trace.py`, `exemplar.py`
- 논문 독해 모드: `paper.py`, `paper_source.py`, `paper_library.py`, `paper_memory.py`
- 세션/장기 메모리: `session_store.py`, `conversation_memory.py`
- 스킬 색인: `skills.py`, `data/friday/skills.index.json`
- 런타임 steering: `steering.py`
- Open WebUI 연동: `openwebui.py`

실제 데이터도 이미 쌓이고 있다.

- 현재 활성 job: `20260401-221032-todolist`
- workspace job 예시: todolist, logit-margin-score, llm-safety
- 실행 trace: `.friday_traces.jsonl` 63건
- 세션 파일: `sessions/*.session.json` 2개
- conversation memory: `data/friday/conversation_memory.jsonl`, `conversation_memory.md`
- indexed skills: 241개
- 논문 라이브러리: `library/papers/00688-aaai26.hahmd-ali/`

즉, 현재 상태는 "프로토타입 아이디어"를 넘어서 실제 REPL에서 여러 작업을 처리하고 흔적을 남기는 로컬 assistant runtime이다. 다만 모든 기능이 같은 완성도는 아니다. 일반 파일/일정/검색/논문 요약/세션/메모리는 꽤 형태가 잡혔고, 상태머신 플래너와 Night 논문 워크플로는 사용 중 드러난 edge case를 계속 보수하는 단계다.

## 3. 전체 아키텍처

현재 가장 중요한 구조는 `shell.py:_dispatch()`의 3-tier 디스패치다.

```text
사용자 입력
  -> TaskFrame: 대상 파일, 위험도, 누락 슬롯을 먼저 추정
  -> Tier-0: 순수 대화면 바로 스트리밍 답변
  -> Tier-1a: regex 규칙으로 즉시 실행 가능한 intent 처리
  -> Tier-1b: embedding classifier로 유사 표현 처리
  -> Tier-2: 복합 작업은 상태머신 플래너 또는 legacy ReAct로 처리
```

이 구조에서 중요한 점은 모든 입력을 에이전트로 보내지 않는다는 것이다. "일정 보여줘", "파일 목록", "todo.md 읽어줘" 같은 요청은 즉시 처리하고, "이번 주 일정 보고 파일로 정리한 뒤 문체도 다듬어줘" 같은 다단계 요청만 Tier-2로 넘어간다.

Tier-2는 현재 기본적으로 `PlanStateMachine`을 사용한다. 실패하면 기존 `run_agent_legacy()` ReAct 루프로 fallback한다. 환경변수 `FRIDAY_USE_STATE_MACHINE=0`으로 legacy 루프를 강제할 수도 있다.

## 4. 구현 중 했던 주요 고민과 선택

### 4.1 모든 요청을 에이전트에게 맡길 것인가, 단계적으로 라우팅할 것인가

가능한 선택지는 크게 두 가지였다.

- 모든 자연어 입력을 ReAct agent에게 넘긴다.
- 단순 작업은 빠른 경로로 처리하고, 복잡한 작업만 agent에게 넘긴다.

Friday는 두 번째를 골랐다. 이유는 속도와 안정성 때문이다. 모든 요청을 agent로 보내면 간단한 파일 읽기에도 LLM 추론, JSON action parsing, tool observation loop가 필요해진다. 응답이 느려지고, 단순 명령에서도 LLM이 엉뚱한 도구를 고를 여지가 생긴다.

그래서 `Tier-0 -> Tier-1a -> Tier-1b -> Tier-2` 구조를 만들었다. 결과적으로 순수 대화는 스트리밍 답변으로 빠르게 처리되고, 규칙으로 잡히는 명령은 LLM 없이 실행된다. embedding classifier가 추가되면서 "출력해줘", "띄워줘", "프린트해줘"처럼 규칙만으로는 놓치기 쉬운 표현도 처리할 수 있게 되었다.

부작용도 있다. 휴리스틱이 너무 넓으면 사실은 도구를 써야 할 요청이 Tier-0 대화로 빠질 수 있다. 실제 trace에도 active paper가 선택된 상태에서 논문 scope 질문이 Tier-0으로 처리된 사례가 있다. 이 문제는 Night mode에서는 `TaskFrame`과 active paper context를 Tier-0보다 먼저 더 강하게 반영하거나, `is_chat()` 판정을 모드별로 보수화하는 식으로 개선할 여지가 있다.

### 4.2 규칙 기반 intent만 쓸 것인가, embedding classifier를 둘 것인가

초기 규칙 기반 parser는 빠르고 예측 가능하다. 하지만 한국어 자연어 표현은 매우 다양하다. "읽어줘", "보여줘", "열어봐", "출력해줘"는 사용자의 의도는 같지만 regex를 계속 늘려야 한다.

대안은 LLM parser를 매번 호출하는 것이었다. 하지만 이는 느리고, 단순 intent에서도 JSON 오류나 환각 가능성이 생긴다.

그래서 중간 해법으로 `IntentEmbedClassifier`를 넣었다. 약 130개 예시 문장을 `nomic-embed-text`로 임베딩하고, 사용자 입력과 cosine similarity를 비교한다. confidence가 0.75 미만이면 Tier-2로 넘기고, 쓰기 계열 작업은 confidence가 0.85 미만이면 자동 실행하지 않고 확인을 요구한다.

결과는 좋은 균형이다. 규칙보다 유연하지만 LLM parser보다 가볍다. 또한 cache(`.embed_cache.json`)를 두어 첫 실행 이후 비용을 줄였다. 모델이 없거나 embedding API가 실패하면 조용히 rule-only routing으로 내려간다.

### 4.3 실행 권한을 planner에게 맡길 것인가, 별도 policy로 뺄 것인가

LLM planner에게 "위험하면 조심해"라고만 말하는 방식은 간단하지만, 시스템적으로는 약하다. 모델이 잘못 판단하면 곧장 실행으로 이어질 수 있기 때문이다.

Friday는 `permission.py`와 `sandbox.py`를 분리했다.

- `permission.py`: intent 단위로 auto/confirm/deny에 가까운 실행 정책을 판단한다.
- `sandbox.py`: 실제 파일 경로가 허용 범위 안에 있는지 검증한다.
- `git_mgr.py`: 위험한 Git 명령을 확인 또는 차단한다.

이 선택 덕분에 planner가 어떤 계획을 만들든, 실행 계층은 다시 한번 경계를 확인한다. 현재 정책은 읽기 전용, local create/edit/rewrite, shell, Git, trash, import/export, Open WebUI 등을 구분한다. 파일 쓰기는 현재 job workspace 안으로 제한하고, 논문 라이브러리 쓰기는 `library/papers/`로 별도 제한한다. 삭제는 직접 삭제가 아니라 trash 이동만 허용한다.

결과적으로 Friday는 "자연어로 편하지만, 파일 시스템 전체를 막 만지는 도구는 아닌" 쪽으로 정체성이 잡혔다.

### 4.4 ReAct 루프를 유지할 것인가, State Machine + Planner로 바꿀 것인가

초기 Tier-2는 전형적인 ReAct loop였다.

```text
LLM -> action JSON -> 도구 실행 -> observation -> LLM -> ...
```

이 방식의 장점은 유연성이다. 중간 결과를 보고 다음 행동을 바꿀 수 있다. 하지만 단점도 컸다.

- 실행 전에 전체 계획을 사용자에게 보여주기 어렵다.
- 실패 복구 정책이 흐릿하다.
- LLM이 존재하지 않는 도구를 부를 수 있다.
- 테스트와 trace 해석이 어렵다.
- 정책 게이트가 실행 흐름 중간에 섞인다.

그래서 `PlanStateMachine`을 도입했다.

```text
STRUCTURING
  -> PLANNING
  -> VALIDATING
  -> CONFIRMING
  -> EXECUTING
  -> RETRYING
  -> COMPLETING
  -> DONE / FAILED / ABORTED
```

현재 Tier-2는 먼저 fast model로 `StructuredIntent`를 만들고, 그 결과와 현재 job, 파일 목록, 일정, active paper, skills, task frame을 planner prompt에 넣는다. planner는 최대 8단계의 JSON plan을 만든다. action 이름은 `tool_registry`에 등록된 이름과 검증되고, confirm이 필요한 capability가 있으면 실행 전에 plan을 보여준다.

이 선택의 결과는 실행 흐름이 훨씬 관찰 가능해졌다는 것이다. trace에는 structured intent, routing decision, step 결과가 남는다. 실패 시에는 legacy ReAct fallback도 있어 완전히 막히지 않는다.

대신 상태머신은 ReAct보다 즉흥적인 적응력이 낮다. 그래서 `condition`, retry, `{{step_N_result}}` placeholder, fallback ReAct를 보완 장치로 넣었다. 아직 planner prompt tuning과 trace 기반 few-shot 개선은 남은 과제다.

### 4.5 도구 실행을 if-else로 둘 것인가, registry로 만들 것인가

초기 agent 구현에서는 action별 실행이 커다란 if-else로 늘어날 수 있었다. 기능이 5개일 때는 괜찮지만, 파일, 논문, 검색, Git, shell, paper memory까지 늘어나면 수정 비용과 실수 가능성이 커진다.

그래서 `tool_registry.py`를 만들고 모든 도구를 `ToolDef`로 등록했다.

각 도구는 다음 정보를 가진다.

- name
- description
- risk_level
- required_capability
- executor

현재 registry에는 calendar, file read/write/edit/rewrite, glob/grep, shell, Git, search, deep_search, trash, job 생성, paper source loading, paper QA, concept explanation, paper comparison, paper note/memory 관련 도구가 등록되어 있다.

이 선택의 결과는 좋다. 새 도구를 추가할 때 planner prompt, 권한 분류, executor 연결이 한 곳에서 보인다. State Machine도 action name을 registry로 검증할 수 있다. 다만 `TOOL_DESCRIPTIONS`가 아직 `agent.py`의 큰 문자열로 남아 있어, registry metadata와 prompt description을 더 일원화할 여지는 있다.

### 4.6 모델을 하나만 쓸 것인가, 작업별로 나눌 것인가

하나의 큰 모델을 모든 작업에 쓰면 단순하다. 하지만 Friday의 작업은 "짧은 대답", "JSON intent 구조화", "계획", "논문 독해", "코드 작업"처럼 성격이 다르다.

그래서 현재 config는 모델을 역할별로 나눈다.

- fast model: 빠른 답변, 구조화, 단순 작업
- main model: 복잡한 추론과 일반 planning
- coder model: 코드 관련 planning 후보
- night model: FRIDAY Night 논문 독해
- reviewer model: 더 비판적인 논문 리뷰 모드
- embed model: intent classifier와 exemplar retrieval
- struct model: 향후 intent structuring 전용으로 확장할 자리

`model_router.py`는 `StructuredIntent`의 intent, interaction mode, confidence를 점수화해 fast/main/coder를 고른다. active paper가 있는 Night mode에서는 night model을 강제한다.

결과적으로 단순 작업은 가볍게, 논문 작업은 더 강한 모델로 처리하는 구조가 생겼다. 다만 문서와 config 사이에 모델 이름이 일부 다르다. 현재 실제 기본값은 `config.py` 기준이며, 환경변수로 덮어쓸 수 있다. 앞으로는 README, AGENTS, config의 모델 표를 한 번 맞추는 정리가 필요하다.

### 4.7 논문 기능을 job 파일 안에 둘 것인가, 별도 paper library로 둘 것인가

처음에는 PDF를 job workspace 안에서 읽고 요약하는 방식이면 충분해 보인다. 하지만 논문 독해는 일반 파일 작업과 성격이 다르다.

- 같은 논문을 여러 세션에서 다시 읽는다.
- summary, review, evidence, notes, memory가 함께 쌓인다.
- active paper 개념이 필요하다.
- context window에는 전체 원문을 계속 넣을 수 없다.

그래서 FRIDAY Night는 `library/papers/<paper-id>/`를 canonical store로 사용한다. `paper_source.py`는 arXiv URL/ID, PDF URL, 로컬 파일을 감지하고, 안전하게 library 안으로 복사 또는 다운로드한다. `paper.py`는 PDF를 page-aware로 추출하고 section parsing, summary, evidence ledger, senior review pipeline을 제공한다. `paper_library.py`는 paper shelf와 active paper context를 만든다.

이 선택의 결과, 논문 작업은 일반 workspace 작업과 분리된 장기 지식 베이스가 되었다. 실제로 `00688-aaai26.hahmd-ali` 논문이 library에 저장되어 있고, `summary.md`, `metadata.json`, `paper.paper.json`, `paper.pdf`가 남아 있다.

사용 중 발견한 문제도 있었다. 어떤 trace에서는 `read_paper_file(section="abstract")`가 처음에는 parsed section에서 abstract를 찾지 못했다. 이후 fallback extraction을 통해 PDF/full_text에서 abstract를 찾아 반환하는 로직이 들어갔다. 또 review.md/evidence.json이 없을 때 summary.md로 fallback하는 흐름도 있다. 이 덕분에 사용자 경험은 끊기지 않지만, 최종적으로는 library artifact completeness를 더 엄격히 표시할 필요가 있다.

### 4.8 전체 대화를 계속 들고 갈 것인가, 세션과 장기 메모리를 나눌 것인가

대화 히스토리를 메모리에만 들고 있으면 프로세스 종료 시 맥락이 사라진다. 반대로 모든 과거 대화를 매번 prompt에 넣으면 context가 금방 비대해진다.

그래서 두 층으로 나누었다.

- `sessions/*.session.json`: 실제 대화 기록을 세션 단위로 저장한다.
- `data/friday/conversation_memory.jsonl`: 중요한 선호, 프로젝트 결정, 모델 결정, 논문 워크플로만 요약해 장기 메모리로 저장한다.

`session_store.py`는 자동 저장, 최근 세션 복원, 세션 목록/전환/삭제/rename을 제공한다. `conversation_memory.py`는 중요한 user/assistant pair만 골라 JSONL과 사람이 읽을 수 있는 Markdown projection으로 남긴다.

결과적으로 Friday는 "방금 대화"와 "오래 남길 결정"을 분리하게 되었다. 현재 실제로 2개의 session file과 conversation memory가 존재한다. 다만 memory capture는 아직 heuristic 기반이라, 중요하지 않은 내용이 들어가거나 중요한 결정을 놓칠 수 있다. 향후에는 사용자가 명시적으로 pin/unpin하거나, memory candidate를 보여주고 승인받는 흐름이 있으면 더 안정적이다.

### 4.9 외부 skill을 자동 실행할 것인가, instruction-only로 둘 것인가

`vendor/skills/` 아래에는 많은 third-party skill이 있다. 현재 색인된 skill은 241개다. 이 skill들은 유용한 지침과 스크립트를 포함할 수 있지만, 외부 스크립트를 자동 실행하는 것은 위험하다.

그래서 현재 Friday의 skill 모드는 instruction-only다. `skills.py`는 `SKILL.md`를 색인하고, 사용자 요청과 매칭되는 skill description을 planner context에 넣는다. 스크립트가 있는 skill도 자동 실행하지 않고, "스크립트는 있지만 현재 Friday skill mode에서는 disabled"로 취급한다.

이 선택은 보수적이지만 적절하다. Friday의 핵심 철학이 로컬 안전 실행이기 때문이다. 나중에 특정 skill script를 실행하려면, registry에 명시적인 tool로 승격하고 permission/sandbox를 통과시키는 편이 맞다.

### 4.10 모델 thinking을 어떻게 보여줄 것인가

일부 reasoning 모델은 `<think>`, `<thought>`, `<thinking>` 블록이나 Ollama의 `thinking` field를 반환한다. 그대로 사용자 답변에 섞이면 읽기 불편하고, JSON parser도 깨질 수 있다.

그래서 `ollama.py`와 `rendering.py` 쪽에서 thinking을 분리 표시하고, planner/intent parser는 think block을 제거한 뒤 JSON을 찾도록 했다. `state_machine.py:_extract_plan_json()`과 `intent_structuring.py:_extract_json()`도 think tag와 markdown fence를 제거한 뒤 JSON object를 스캔한다.

이 선택은 실제 버그 수정으로 이어졌다. `REDESIGN_LOG.md`에도 planner parse failure가 기록되어 있다. 모델이 JSON 앞에 thinking을 붙이면 기존 parser가 잘못된 `{}`를 먼저 잡아 실패했는데, think block 제거 후 JSON scan 방식으로 완화했다.

## 5. 사용자 경험 차원에서 만들어진 모드

현재 Friday는 Daytime과 Nighttime이라는 두 가지 작업 정체성을 가진다.

Daytime은 일정, 파일, 검색, 가벼운 요약, 코드 작업, 일반 질의응답 중심이다. 답변은 실용적이고 짧게 유지하는 편이다.

Nighttime은 논문/PDF/arXiv를 읽는 연구 모드다. active paper, paper shelf, 논문별 memory, reviewer mode가 결합된다. 이 모드에서는 사용자가 "이 논문", "abstract", "초록", "리뷰", "요약"처럼 말하면 active paper를 기본 대상으로 해석한다.

이 모드 분리는 꽤 중요한 결정이었다. 일반 비서 모드와 연구 독해 모드는 좋은 답변의 기준이 다르다. 일반 모드는 속도와 실행성이 중요하고, Night mode는 근거, 한계, 수치, section, 논문에 없는 내용의 명시가 중요하다.

## 6. 현재 관찰된 결과

현재까지 보이는 성과는 다음과 같다.

- 단순 명령과 복합 작업을 분리하는 기본 구조가 동작한다.
- 실행 trace가 남기 때문에 나중에 라우팅과 planner 개선에 쓸 수 있다.
- state machine 도입으로 Tier-2 실행 계획이 더 검증 가능해졌다.
- policy/sandbox 분리 덕분에 자연어 실행의 위험도를 낮췄다.
- 논문 library 구조가 생겨 paper artifact를 job보다 오래 유지할 수 있다.
- active paper와 Night mode 덕분에 논문 후속 질문의 사용자 경험이 좋아졌다.
- session과 conversation memory가 도입되어 프로세스 종료 후에도 맥락 일부를 복원할 수 있다.
- skill index가 생겨 외부 skill 지침을 자동 context로 넣을 수 있다.

동시에 아직 드러난 미완성/개선 포인트도 있다.

- Tier-0 chat heuristic이 active paper 질문을 너무 빨리 chat으로 처리할 수 있다.
- State Machine의 streamed final response는 trace의 `final_response_preview`에 빈 문자열로 남는 경우가 있다.
- `review.md`, `evidence.json`, `memory.md`가 없는 paper library 항목에서 summary fallback이 일어나므로 artifact 상태를 더 명확히 보여줄 필요가 있다.
- planner prompt와 tool registry metadata가 완전히 일원화되어 있지는 않다.
- README, AGENTS, config의 기본 모델명이 일부 달라 현재 실행 기준을 헷갈릴 수 있다.
- embedding classifier와 memory capture는 heuristic과 예시 데이터에 크게 의존한다.
- 테스트 suite가 별도로 보이지 않아, regression을 trace와 수동 확인에 많이 기대고 있다.

## 7. 앞으로의 좋은 다음 단계

가장 먼저 하면 좋은 일은 안정성 중심의 정리다.

1. README/AGENTS/config의 모델명과 실행 방법을 일치시키기
2. Night mode에서는 active paper 관련 질문이 Tier-0으로 빠지지 않도록 `is_chat()` 앞뒤 조건 조정
3. State Machine completion 결과가 trace에 남도록 `run_agent()`/`TraceRecorder` 흐름 보강
4. `tool_registry` metadata에서 planner tool description을 자동 생성하도록 정리
5. paper artifact 상태를 `complete`, `summary_only`, `parsed_only`, `reviewed`처럼 명확히 표시
6. 핵심 라우팅과 sandbox에 대한 최소 단위 테스트 추가
7. `.friday_traces.jsonl`의 성공 trace를 이용해 planner few-shot 예시를 갱신

이 순서가 좋은 이유는, 새 기능을 더 붙이기 전에 Friday의 핵심 약속인 "자연어로 편하지만 안전하고 추적 가능하다"를 더 단단히 만들기 때문이다.

## 8. 한 줄 회고

Friday Project는 로컬 LLM 챗봇에서 출발했지만, 지금은 자연어 입력을 구조화하고, 정책으로 제한하고, 상태머신으로 실행하며, trace와 memory로 다음 실행을 더 좋게 만들 준비를 갖춘 개인 작업 런타임으로 성장했다. 아직 거친 부분은 있지만, 방향은 분명하다. Friday는 "대답하는 프로그램"에서 "내 로컬 작업을 안전하게 이어받는 실행 동료" 쪽으로 이동하고 있다.
