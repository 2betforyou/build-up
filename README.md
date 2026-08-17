# Build-up

**Language:** 한국어 | [English](README.en.md)

로컬 모델과 실제 웹을 연결해 검증 가능한 근거 원장을 쌓는 evidence-first 딥 리서치 CLI입니다. 연구 질문을 계약과 하위 과제로 분해하고, 원문과 정확히 대조된 evidence만 채택하고, 완료된 run을 append-only Knowledge Vault로 자동 반영합니다.

## 설치

Python 3.10 이상, Ollama, research/reviewer model, 검색 공급자 하나 이상이 필요합니다.

```bash
git clone https://github.com/2betforyou/build-up.git
cd build-up
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

ollama pull qwen3.8:27b
ollama pull deepseek-r1:32b
buildup doctor
```

로컬 브라우저 검색을 쓰려면:

```bash
python -m pip install -e '.[browser]'
playwright install chromium
```

설치 없이 `./buildup` launcher로 바로 실행할 수도 있습니다.

## 시작하기

```bash
buildup
```

셸이 뜨면 아래처럼 job을 만들고 리서치를 실행합니다.

```
/job new agent-harness --tpl research
/research 로컬 LLM용 deep-research agent harness의 설계와 한계
```

`딥 리서치`, `심층 리서치`, `deep research`가 들어간 자연어 요청도 같은 워크플로로 라우팅됩니다.

## 딥 리서치

```
user request
  │
  ├─ ResearchContract + depth + hard budget
  ├─ independent ResearchTask[]
  │      └─ concurrent query fan-out
  │             └─ safe page reader / PDF extraction
  ├─ SourceRegistry
  │      └─ exact-passage EvidenceLedger
  ├─ Claim[] + contradiction + ResearchGap[]
  │      ├─ sufficient ───────────────┐
  │      └─ follow-up ResearchTask ───┘
  ├─ evidence-only Writer
  ├─ deterministic CitationAudit
  ├─ independent ClaimAudit
  └─ bounded correction → final report + manifest
```

- Research contract가 목적, 범위, 하위 질문, 출처 요구 사항, 성공 조건을 먼저 고정합니다.
- `plan → search/read → evidence → claims/gaps → follow-up`을 충분하거나 예산이 끝날 때까지 반복합니다.
- Evidence는 저장된 source 본문의 정규화된 부분문자열과 정확히 일치하는 passage만 채택됩니다.
- reader가 직접 가져온 PDF 원본 바이트는 run별 `raw-sources/`에 SHA-256 manifest로 봉인됩니다.
- Source/Evidence/Claim ID는 URL, passage, claim 텍스트에서 결정론적으로 만들어집니다.
- Writer는 검색 권한이 없고 registry 밖 URL이나 근거를 추가할 수 없습니다.
- 코드 기반 citation audit와 독립 reviewer model의 claim-support audit를 모두 통과해야 완료 처리됩니다.
- round, task, search, source, model call, runtime에 hard budget이 적용되며 checkpoint에 누적됩니다.
- phase와 task마다 원자적 checkpoint를 남기고, 재개 시 완료된 작업을 반복하지 않습니다.
- 같은 run은 process lease로 한 번에 한 프로세스만 수정합니다.
- 모델과 네트워크 없이 evidence·citation·artifact hash를 다시 검증하는 오프라인 평가를 제공합니다.

외부 page는 untrusted data로 취급되며, 그 안의 지시문이나 명령은 실행되지 않습니다.

셸 명령:

```
/research 조사할 주제
/research --no-wiki 조사할 주제       Knowledge Vault에 반영하지 않고 1회 실행
/research list                       현재 job의 run 목록
/research show 1                     보고서와 study guide 열기
/research resume 1                   checkpoint에서 재개
/research eval 1                     오프라인 품질 게이트 재계산
```

모든 run은 현재 job의 `deep-research/<run-id>/`에 남습니다.

```
deep-research/<run-id>/
├── 00-sources.json
├── 01-contract.json
├── 02-plan.json
├── 03-sources.jsonl
├── 04-evidence.jsonl
├── 05-claims.jsonl
├── 05-gaps.json
├── 06-report.md
├── 07-study-guide.md
├── 08-citation-audit.json
├── 09-claim-audit.json
├── 10-evaluation.json         # eval 실행 후 생성
├── raw-sources/                # reader가 가져온 원 PDF bytes
├── state.json                  # resumable source of truth
├── events.jsonl
├── metadata.json
└── manifest.json
```

## Knowledge Vault

완료된 딥 리서치는 추가 model call 없이 현재 job의 vault에 자동 반영됩니다. 수집한 source 본문, 원 PDF, research artifact는 봉인된 원장으로 유지되고 Wiki는 그 원장에서 언제든 재생성되는 파생 지식입니다.

```
~/.buildup/knowledge/vaults/<vault-id>/
├── schema.yaml
├── vault.json
├── sources.jsonl          # append-only source-version registry
├── claims.jsonl           # append-only claim/status/rollback events
├── log.jsonl
├── proposals/             # immutable research → Wiki proposals
├── index.md               # deterministic derived view
└── pages/
    ├── concepts/
    ├── methods/
    ├── models/
    ├── datasets/
    ├── papers/
    ├── claims/
    ├── contradictions/
    ├── open-questions/
    └── synthesis/
```

- Wiki 페이지 자체는 새 claim의 근거로 쓰이지 않습니다.
- 모든 claim은 원 research Source/Evidence ID, URL, artifact 경로, source/passage SHA-256을 가지며, PDF source는 원 byte hash와 크기까지 추적합니다.
- 상태는 `draft → supported → verified|disputed|stale|rejected`를 append-only event로 기록합니다.
- citation audit, claim audit, manifest를 모두 통과한 완료 run만 `supported`로 자동 반영됩니다.
- 같은 URL의 source hash가 바뀌면 재지지되지 않은 기존 claim은 `stale`로 표시됩니다.
- contradiction은 승자를 자동으로 정하지 않고 claim과 counter-evidence를 함께 보존합니다.
- rollback은 삭제가 아니라 취소 event로 append됩니다.
- `verified`는 사용자가 직접 설명하거나 확인한 supported claim에만 부여됩니다.
- job은 기본적으로 자기 vault만 사용하며, 다른 vault를 bind하려면 확인이 필요합니다.
- 기본 자동화는 `auto_ingest`, `auto_compile`, `auto_lint`가 켜져 있습니다. `auto_verify`와 destructive edit, cross-vault sharing은 항상 명시적 확인을 거칩니다.

셸 명령:

```
/wiki status                  현재 job vault 상태
/wiki add [RUN]                sealed research run을 반영
/wiki ask 질문                 원 citation과 함께 grounded claim 조회
/wiki review                   conflict/staleness 검토
/wiki lint                     vault 감사
/wiki verify CLAIM_ID          사용자 검증으로 승격
/wiki reject CLAIM_ID          이력은 남기고 반려
/wiki rollback PROPOSAL_ID     비파괴 rollback event 추가
/wiki bind VAULT_ID            다른 vault를 명시적으로 공유/연결 (확인 필요)
```

`이 리서치를 지식에 반영해줘`, `내가 MoE에 대해 지금까지 뭘 알고 있지?`, `위키 상태 점검해줘` 같은 자연어도 같은 동작으로 처리됩니다.

## 검색 설정

`BUILDUP_SEARCH_PROVIDER=auto`는 설정된 공급자를 다음 순서로 시도합니다.

1. Tavily
2. Brave Search API
3. Exa
4. SearXNG
5. Google Custom Search (기존 자격 증명 호환용)
6. opt-in local Chromium
7. OpenAlex fallback

`paper`, `journal`, `DOI`, `논문`, `학술`, `데이터셋`처럼 학술 의도가 담긴 query에서는 keyed OpenAlex를 맨 앞으로 옮깁니다. `BUILDUP_SEARCH_PROVIDER=openalex`로 고정할 수도 있습니다.

API 키 없이는 직접 운영하거나 신뢰하는 SearXNG endpoint, 또는 로컬 인터넷 연결을 쓰는 Chromium 브라우저 검색을 사용합니다. 브라우저 검색은 기본적으로 꺼져 있고, CAPTCHA나 동의 화면을 만나면 그 자리에서 실패로 기록하고 멈춥니다.

OpenAlex는 논문 discovery용이며 등록된 `OPENALEX_API_KEY`가 있을 때 활성화됩니다. abstract와 metadata는 검색 힌트로만 쓰이고, reader가 원 논문 landing page나 공개 PDF를 다시 읽고 hash한 경우에만 evidence로 채택됩니다.

```bash
# API providers
export TAVILY_API_KEY='...'
export BRAVE_SEARCH_API_KEY='...'
export EXA_API_KEY='...'

# Scholarly discovery
export OPENALEX_API_KEY='...'
export BUILDUP_SEARCH_PROVIDER=openalex

# Self-hosted SearXNG
export BUILDUP_SEARXNG_URL='http://127.0.0.1:8080'
export BUILDUP_SEARCH_PROVIDER=searxng

# Local browser: google, brave, or bing
export BUILDUP_BROWSER_SEARCH_ENABLED=true
export BUILDUP_BROWSER_SEARCH_ENGINE=google
export BUILDUP_SEARCH_PROVIDER=browser
```

공급자 응답은 공통 schema로 정규화되어 query/provider/language별 로컬 cache에 저장됩니다. API key는 cache나 연구 artifact에 저장되지 않습니다. `BUILDUP_SEARCH_CACHE_TTL_HOURS=0`으로 query/page cache 쓰기를 완전히 끌 수 있습니다.

## Job과 파일

job은 워크스페이스 단위입니다. 기본적으로 `workspace/<job-id>/` 안에서만 파일을 만들고 고치며, 이미 가지고 있는 폴더는 복사하지 않고 그 자리에서 bind해서 다룹니다.

```
/job new [이름] [--tpl TPL]     job 생성 (paper / code / report / research / ops 템플릿)
/job use JOB_ID                 job 전환
/job rename 새이름               표시 이름 변경
/job bind PATH [--write]        실제 디렉터리를 현재 job에 연결 (기본 읽기 전용)
/job unbind PATH                bind 해제
/job binds                      bind 목록
/job list                       전체 job 목록
/job current                    현재 job과 bind 상태
/job log                        action log
/job summary                    action log AI 요약
/templates                      템플릿 목록
```

```
/import PATH                    파일/폴더를 현재 job으로 복사
/files                          현재 job 파일 목록
/read RELPATH                   텍스트 파일 보기
/readpdf RELPATH                PDF 텍스트 추출
/rewrite RELPATH :: 지시문       파일 재작성 (diff 표시)
/diff FILE_A FILE_B             두 파일 비교
/trash RELPATH                  파일을 trash로 이동
/export [DEST]                  현재 job 내보내기
/edit RELPATH :: OLD :: NEW     파일 내 정확한 문자열 치환 (diff 표시)
/glob PATTERN                   job 폴더 glob 검색
/grep PATTERN [-- PATH_GLOB]    파일 내용 정규식 검색
```

bind는 기본이 읽기 전용이고 `--write`를 붙여야 쓰기가 열립니다. bind된 경로는 `/files`, `/read`, `/glob`, `/grep`의 탐색 범위에 들어가고, 쓰기 가능한 bind에서만 파일을 저장·수정할 수 있습니다. bind는 Build-up 데이터 루트, `$HOME` 전체, 파일시스템 루트, 심볼릭 링크, bind 범위를 벗어나는 경로에는 걸 수 없습니다.

## 세션

`buildup`을 실행하면 현재 job의 최근 대화가 자동으로 이어집니다.

```
/new                             새 대화 시작
/sessions                        이 워크스페이스의 대화 목록
/sessions 검색어                  대화 내용 전문검색
/resume                          최근 대화 중에서 고르기 (job 전체 대상)
/resume 검색어|번호|latest         제목 일부, 번호, 또는 최신으로 바로 전환
/resume prev                     직전 대화로
/title 제목                       현재 대화 이름 지정
/session rename 번호 제목          대화 이름 변경
/session archive 번호             대화 보관
/session export 번호 [md|json]   대화 내보내기
```

`이전 대화 이어줘`, `세션 목록 보여줘`, `새 대화` 같은 자연어도 같은 동작으로 처리됩니다.

## 모드와 그 외 기능

```
/mode auto|fast|main|refine     응답 모드 전환
데이타임 / 데이모드               일상 어시스턴트 모드로 전환
리서치타임 / 리서치모드           딥 리서치 우선 모드로 전환
/paper list|use|current         논문 서가 조회와 active paper 선택
/translate PDF|PAPER_ID          논문 PDF를 한국어 Markdown으로 번역
/skills list|search|show|audit  설치된 Agent Skill 관리
/steer show|list|profile|set     세션 단위 응답 스타일 조정
/memory show [--all]             durable memory 조회
/memory add [workspace|user] 내용 durable memory 추가
/prefs                          사용자 설정과 프로젝트 컨텍스트 표시
/intent-debug 텍스트             자연어가 어떤 task로 해석되는지 확인
/history                        대화 기록 보기
/undo                           마지막 대화 턴만 제거
/retry                          마지막 일반 대화 턴 재시도
/compact                        오래된 컨텍스트 요약 (전체 로그는 유지)
/search 질문                     웹 검색 + AI 답변
```

## 모델과 budget 설정

| 환경 변수 | 기본값 | 용도 |
|---|---:|---|
| `BUILDUP_BASE_DIR` | `~/.buildup` | 로컬 데이터 루트 |
| `BUILDUP_RESEARCH_MODEL` | `qwen3.8:27b` | plan, evidence, assessment, writer |
| `BUILDUP_REVIEWER_MODEL` | `deepseek-r1:32b` | 독립 claim audit |
| `BUILDUP_ENGLISH_BRIEF` | `false` | 한국어 답변 뒤 English Brief 섹션 추가 |
| `BUILDUP_FAST_MODEL` | `gemma4:e4b` | 빠른 채팅 |
| `BUILDUP_MAIN_MODEL` | `qwen3.8:27b` | 일반 추론 |
| `BUILDUP_CODER_MODEL` | `qwen3.8:27b` | 코드 작업 |
| `BUILDUP_SEARCH_PROVIDER` | `auto` | 검색 공급자 선택 |
| `OPENALEX_API_KEY` | — | 학술 검색용 키 |
| `BUILDUP_SEARCH_TIMEOUT` | `30` | 네트워크 timeout |
| `BUILDUP_SEARCH_CACHE_TTL_HOURS` | `24` | query/page cache TTL |
| `BUILDUP_RESPECT_ROBOTS_TXT` | `true` | page reader의 robots 정책 |
| `BUILDUP_RESEARCH_DEPTH` | `auto` | 기본 depth |
| `BUILDUP_RESEARCH_MAX_ROUNDS` | `3` | round 상한 |
| `BUILDUP_RESEARCH_MAX_TASKS` | `8` | task 상한 |
| `BUILDUP_RESEARCH_MAX_SEARCHES` | `24` | query 상한 |
| `BUILDUP_RESEARCH_MAX_SOURCES` | `30` | source 상한 |
| `BUILDUP_RESEARCH_MAX_MODEL_CALLS` | `20` | inference 상한 |
| `BUILDUP_RESEARCH_MAX_RUNTIME_SECONDS` | `1800` | 실행 시간 상한 |
| `BUILDUP_RESEARCH_MIN_COVERAGE` | `0.85` | standard depth 통과 기준 |
| `BUILDUP_KNOWLEDGE_AUTO_INGEST` | `true` | 완료 run → append-only proposal 반영 |
| `BUILDUP_KNOWLEDGE_AUTO_COMPILE` | `true` | ledger → Markdown Wiki 재생성 |
| `BUILDUP_KNOWLEDGE_AUTO_LINT` | `true` | schema·citation·staleness 자동 검사 |

값이 0, 음수, 상한 초과이면 조용히 보정하지 않고 오류로 거부합니다.

## 데이터와 보안

```
~/.buildup/
├── data/buildup/
│   ├── state.db
│   ├── traces.jsonl
│   └── search-cache/
├── workspace/<job-id>/
├── knowledge/vaults/<vault-id>/
├── library/papers/
├── export/
└── trash/
```

- Page reader는 HTTP(S)만 허용하고 localhost, private, link-local, `.local` target과 위험한 redirect를 차단합니다.
- robots.txt, response size, redirect count, content type, timeout을 검사합니다.
- PDF와 HTML에서 추출한 본문은 길이 제한 후 hash와 함께 저장됩니다. reader가 직접 받은 PDF는 추출문과 별도로 run 내부에 원 byte를 보존합니다.
- 실패한 model response는 diagnostic 파일로 격리되며 성공 artifact로 취급되지 않습니다.
- 파일 쓰기는 현재 job workspace와, `--write`로 직접 bind한 디렉터리에서만 허용됩니다.
- Web query와 공개 page request는 선택한 외부 공급자 또는 검색 엔진으로 전송됩니다.
- Ollama URL을 원격 endpoint로 바꾸면 model prompt도 그 endpoint로 전송됩니다.
- Workspace, API key, session DB, cache, log는 Git ignore 대상입니다.

## 개발과 검증

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=router python -m compileall -q router/buildup tests
PYTHONPATH=router python -W error::ResourceWarning -m unittest discover -s tests -v
ruff check router tests
python -m build
```

테스트는 live model이나 network 없이, callback을 주입해 adaptive follow-up, hard budget, checkpoint/resume, exact passage, citation integrity, provider failover/cache, Knowledge Vault provenance/status/rollback을 검증합니다.

## 알려진 한계

- 검색 품질은 선택한 공급자와 검색 엔진 정책에 영향을 받습니다.
- Exact passage 검사는 출처가 문장을 포함함을 보장하지만, 문장이 참이라는 것까지 보장하지는 않습니다.
- Reviewer는 독립 model pass이지만 같은 model family의 편향을 완전히 제거하지는 못합니다.
- JavaScript-only page는 reader에서 본문이 부족할 수 있습니다.
- OpenAlex metadata는 원 논문 전문이 아니며, reader가 원 landing page/PDF를 확보하지 못하면 evidence로 채택되지 않습니다.
- 원 byte 보존은 현재 PDF source에만 적용되고, 일반 HTML은 정규화된 본문과 URL/hash만 봉인합니다.
- 법률·의료·금융처럼 고위험인 결론에는 전문가 검토가 필요합니다.

## 문서

- 내부 모듈 경계: [router/buildup/README.md](router/buildup/README.md)
- 변경 내역: [CHANGELOG.md](CHANGELOG.md)
- 기여 방법: [CONTRIBUTING.md](CONTRIBUTING.md)
- 보안 제보: [SECURITY.md](SECURITY.md)
