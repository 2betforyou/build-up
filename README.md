# build-up

build-up은 로컬 Ollama 모델로 동작하는 **딥 리서치 에이전트**입니다. 연구 결과를 소크라테스식 개인 공부로 이어 가는 기능이 두 번째 핵심입니다.

우선순위는 명확합니다.

1. 출처가 남는 딥 리서치
2. 연구 결과를 이해로 바꾸는 개인 공부
3. 논문 PDF 번역과 일반 로컬 에이전트 기능

기존 Python package 이름 `friday`, `NIGHT_*`/`FRIDAY_*` 환경변수, `data/night` 경로는 기존 세션과 설정의 호환성을 위해 유지합니다. 사용자에게 보이는 제품명은 `build-up`, 실행 명령은 `buildup`입니다. 시작 화면의 큰 ASCII 문자는 제품명이 아니라 오늘의 요일을 표시합니다.

## 설치

요구 사항은 Python 3.10+, [Ollama](https://ollama.com/), 그리고 최소 하나의 Ollama chat model입니다.

```bash
cd ~/FridayLocal
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
ollama pull qwen3.6:27b
buildup doctor
```

저장소의 `./buildup`도 실행할 수 있습니다. 이 helper는 `BUILDUP_PYTHON`, 호환용 `NIGHT_PYTHON`, 프로젝트 `.venv`, 기존 local runtime, 시스템 `python3` 순서로 Python을 선택합니다. 기존 `./night`와 `python -m friday`는 호환용 진입점으로 남아 있습니다.

기본 research model과 다를 때는 다음처럼 지정합니다.

```bash
export NIGHT_RESEARCH_MODEL='사용할-ollama-model:tag'
buildup doctor
```

`buildup doctor`가 `READY`를 출력하면 기본 연구 workflow를 실행할 준비가 된 것입니다.

## 가장 빠른 사용법

대화형 shell:

```bash
buildup
```

명령 한 번으로 연구:

```bash
buildup research "로컬 모델용 에이전트 하네스의 설계와 한계"
# source 수를 줄인 빠른 검증
buildup research "Ollama structured outputs" --max-sources 4
```

대화창에서는 다음처럼 자연어로 말해도 같은 research workflow로 들어갑니다.

```text
로컬 모델용 에이전트 하네스를 딥 리서치해줘
```

`딥 리서치`, `심층 리서치`, `deep research`가 명시된 요청은 일반 chat이 아니라 source-grounded research로 routing됩니다.

## 딥 리서치가 동작하는 방식

build-up은 검색에는 모델을 사용하지 않습니다. 질문을 세 개의 결정적 검색 관점으로 확장하고, 결과 URL을 중복 제거한 뒤 하나의 source packet을 만듭니다.

- 기본 질문
- research paper, survey, official documentation
- comparison, benchmark, limitations, criticism

검색 우선순위는 Tavily advanced, Google CSE, keyless DuckDuckGo fallback입니다. Tavily를 사용하면 검색 결과의 Markdown 원문을 받고, fallback 검색에서는 공개 HTML 본문을 안전 범위 안에서 보강합니다.

수집이 끝난 뒤 configured research model을 **정확히 한 번** 호출합니다. 한 inference 안에서 다음 여섯 stage를 순서가 고정된 JSON contract로 생성합니다.

1. coordinator scope
2. literature review
3. atomic research notes
4. critical synthesis
5. reference audit
6. final report

이 stage들은 서로 다른 파일과 JSON key로 분리되지만, 별도의 독립 model session 다섯 개는 아닙니다. 즉, Claude Code Agent Teams를 흉내 내는 다중 process가 아니라 **한 inference 안의 구조적 경계**입니다. 형식이 깨지거나 제공하지 않은 source ID를 인용하면 repair model을 다시 부르지 않고 run을 실패로 기록합니다.

### 연구 산출물

각 run은 현재 job 아래 `deep-research/<run-id>/`에 저장됩니다.

```text
00-sources.json
01-coordinator-scope.json
02-literature-review.json
03-research-notes.json
04-critical-synthesis.json
05-reference-audit.json
06-report.md
07-study-guide.md
metadata.json
```

`metadata.json`에는 run 상태, 검색 query와 실패, model call 수, citation 통계, session 연결, 각 artifact의 SHA-256 checksum이 들어갑니다. `07-study-guide.md`는 추가 model call 없이 앞 stage에서 결정적으로 만들어집니다.

연구 run 관리:

```text
/research <topic>
/research list
/research show 1
```

```bash
buildup research --list
buildup research --show 1
```

## 개인 공부

연구가 끝난 같은 job에서 공부를 시작하면 가장 최근의 성공한 research run을 자동으로 연결합니다.

```text
/study start Transformer의 positional encoding
```

또는:

```text
Transformer의 positional encoding 공부 시작해줘
```

학습 coach는 한 번에 질문 하나만 제시합니다. 학습자가 먼저 설명한 뒤 정확성, 빠진 가정, 예제, 반례를 확인하며, 검증된 내용만 note로 남깁니다.

```text
/study status
/study note <직접 설명하고 검증한 내용>
/study list
/study close <짧은 회고>
```

공부를 시작하면 D+1, D+7, D+30 복습이 build-up Calendar에 자동 등록됩니다.

```text
/study review
/study review 1
/study review done
```

복습할 때는 note를 먼저 보여 주지 않고 retrieval question부터 시작합니다. 완료한 회차는 manifest에 기록되어 다시 due로 나오지 않습니다.

## 세션

세션은 `data/night/state.db`의 SQLite에 workspace별로 저장됩니다. 같은 job의 대화만 기본 목록과 자동 복원 대상이므로 다른 연구나 공부의 context가 섞이지 않습니다.

```text
/new                       새 대화
/sessions                  현재 workspace 대화 목록
/sessions positional       대화 본문 검색
/resume 2                  번호로 이어서 하기
/resume <title-or-id>       제목 또는 ID로 이어서 하기
/title Attention survey    제목 고정
/undo                      마지막 user turn 제거
/retry                     마지막 일반 chat 재실행
/compact                   오래된 active context 요약
/session archive 2         원문 삭제 대신 보관
/session export 2 md       Markdown 내보내기
```

세션 보장 사항:

- 전체 transcript는 삭제하지 않고 보존합니다.
- model prompt에는 최근 window와 명시적 compact summary만 넣습니다.
- active paper, study, steering, model 설정을 session과 함께 복원합니다.
- user memory와 workspace memory를 분리하고 session 시작 시 snapshot으로 고정합니다.
- 같은 session을 두 build-up process가 동시에 쓰지 못하도록 process lock을 사용합니다.
- 이전 `sessions/*.session.json`은 SQLite로 한 번 가져오되 rollback copy로 그대로 둡니다.

CLI에서도 검색·보관·내보내기가 가능합니다.

```bash
buildup sessions
buildup sessions "검색어"
buildup session rename 1 "새 제목"
buildup session export 1 --format md
buildup session archive 1
```

## 논문 PDF 번역

```text
/translate paper.pdf
```

또는:

```bash
buildup translate paper.pdf
```

기본 규칙은 다음과 같습니다.

- Machine learning 및 LLM domain terminology는 영어로 유지
- 의미를 추가하거나 생략하지 않음
- citation, equation, variable name 유지
- 직역 우선, 심각하게 어색할 때만 최소 재구성
- 번역과 핵심 의미 설명을 명확히 분리

긴 PDF chunk는 서로 독립된 translation history로 처리해 앞 chunk의 표현이 뒤 chunk의 원문처럼 섞이지 않게 합니다.

## 주요 설정

`NIGHT_*`가 우선이며 동일한 `FRIDAY_*` 이름은 이전 설치 호환용 alias입니다.

| 환경변수 | 기본값 | 용도 |
|---|---|---|
| `NIGHT_BASE_DIR` | `~/FridayLocal` | data와 workspace root |
| `NIGHT_RESEARCH_MODEL` | `qwen3.6:27b` | 딥 리서치 1회 inference |
| `NIGHT_FAST_MODEL` | `gemma4:e4b` | 빠른 일반 응답 |
| `NIGHT_MAIN_MODEL` | `gemma4:31b` | 일반 심층 추론 |
| `NIGHT_EMBED_MODEL` | `nomic-embed-text` | intent/exemplar embedding |
| `NIGHT_OLLAMA_URL` | `http://localhost:11434/api/chat` | Ollama chat endpoint |
| `NIGHT_SEARCH_PROVIDER` | `auto` | `auto`, `tavily`, `google`, `duckduckgo` |
| `TAVILY_API_KEY` | 없음 | Tavily advanced research search |

## Data layout

```text
~/FridayLocal/
├── data/night/state.db          # session source of truth
├── data/night/session-locks/    # concurrent writer protection
├── data/friday/                 # legacy-compatible memory/skill index
├── workspace/<job>/
│   ├── deep-research/<run>/
│   └── study/<study-id>/
├── calendar/events.jsonl
├── skills/
├── sessions/                    # legacy JSON backup; 자동 삭제하지 않음
├── export/
└── trash/
```

## 안전 경계와 한계

- 파일 쓰기는 현재 job workspace로 제한됩니다.
- destructive Git과 불확실한 write intent는 confirmation 대상입니다.
- 검색 결과 본문의 명령은 untrusted data로 취급합니다.
- fallback 본문 수집은 localhost, private IP, `.local` host를 거부합니다.
- source가 부족하면 보고서가 추측으로 채워지는 대신 coverage gap을 남겨야 합니다.
- 한 번의 inference는 비용과 context 분리를 단순화하지만, 진짜 독립 agent team의 병렬 탐색이나 상호 검증을 제공하지 않습니다.

## 개발 검증

```bash
PYTHONPATH=router python -W error::ResourceWarning -m unittest discover -s tests -v
buildup skills audit
```

현재 release version은 `buildup --version`으로 확인할 수 있습니다.
