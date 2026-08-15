# Build-up

> 로컬 모델과 실제 웹을 연결해, 검증 가능한 근거 원장과 재개 가능한 실행 기록을 남기는 evidence-first 딥 리서치 CLI.

Build-up은 Ollama 위에서 동작하는 local-first 연구 에이전트입니다. 연구 질문을 계약과 하위 과제로 분해하고, 원문과 정확히 대조된 evidence만 채택하며, 부족한 근거는 후속 검색으로 보충합니다. 작성기는 검색 권한 없이 검증된 claim/evidence ledger만 보고 보고서를 만들고, 결정론적 인용 검사와 별도 reviewer model의 주장 감사를 모두 통과해야 실행이 완료됩니다.

## 핵심 보장

- Research contract: 목적, 범위, 하위 질문, 출처 요구 사항, 성공 조건을 먼저 고정합니다.
- Adaptive loop: `plan → search/read → evidence → claims/gaps → follow-up`을 충분하거나 예산이 끝날 때까지 반복합니다.
- Exact evidence: passage가 저장된 source 본문의 정규화된 부분문자열일 때만 Evidence로 채택합니다.
- Original PDF seal: reader가 직접 가져온 PDF 바이트를 run별 `raw-sources/`에 보존하고 SHA-256 manifest로 봉인합니다.
- Stable identity: URL, passage, claim에서 안정적인 Source/Evidence/Claim ID를 만듭니다.
- Writer isolation: 작성기는 검색을 하지 않으며 registry 밖 URL이나 근거를 추가할 수 없습니다.
- Two audits: 코드 기반 citation audit와 독립 reviewer model의 claim-support audit를 실행합니다.
- Hard budgets: round, task, search, source, model call, runtime 한도를 checkpoint에 누적합니다.
- Durable resume: phase와 task마다 원자적 checkpoint를 남기고 완료된 작업을 반복하지 않고 재개합니다.
- Single writer: 같은 run을 두 프로세스가 동시에 변경하지 못하도록 process lease를 사용합니다.
- Offline evaluation: 모델과 네트워크 없이 evidence·citation·artifact hash를 다시 검증합니다.
- Compounding knowledge: 완료 run을 append-only Knowledge Vault로 자동 반영하고 원본 citation까지 추적합니다.

Build-up은 prompt 안에서 여러 역할을 연기한 것을 “멀티 에이전트”라고 부르지 않습니다. 검색 fan-out은 병렬로 실행할 수 있지만 model call과 상태 병합은 기본적으로 직렬화됩니다.

## 설치

요구 사항은 Python 3.10 이상, Ollama와 research/reviewer model, 그리고 검색 공급자 하나 이상입니다.

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

선택적 로컬 브라우저 검색:

```bash
python -m pip install -e '.[browser]'
playwright install chromium
```

설치형 command 대신 `./buildup` launcher를 직접 사용할 수도 있습니다.

## 검색 설정

DuckDuckGo 의존성은 없습니다. `BUILDUP_SEARCH_PROVIDER=auto`는 일반 query에서 설정된 공급자를 다음 순서로 시도합니다.

1. Tavily
2. Brave Search API
3. Exa
4. SearXNG
5. Google Custom Search (기존 고객용 legacy compatibility)
6. opt-in local Chromium
7. OpenAlex fallback

`paper`, `journal`, `DOI`, `논문`, `학술`, `데이터셋`처럼 학술 의도가 명시된 query에서는 keyed OpenAlex를 맨 앞으로 이동합니다. `BUILDUP_SEARCH_PROVIDER=openalex`로 고정할 수도 있습니다.

API 없이 사용할 수 있는 방법은 직접 운영하거나 신뢰하는 SearXNG endpoint와, 노트북의 일반 인터넷 연결을 쓰는 local Chromium입니다. 브라우저 검색은 기본적으로 꺼져 있습니다. CAPTCHA 또는 동의 화면을 만나면 실패로 기록하고 중단하며 stealth·proxy rotation·challenge bypass를 구현하지 않습니다.

OpenAlex는 논문 discovery용입니다. Build-up은 현재 공식 정책에 맞춰 등록된 `OPENALEX_API_KEY`가 있을 때만 활성화하며, free key의 일일 allowance는 [OpenAlex authentication 문서](https://developers.openalex.org/api-reference/authentication)에서 확인할 수 있습니다. OpenAlex abstract와 metadata는 검색 힌트일 뿐 evidence 본문으로 채택하지 않습니다. 연구 reader가 원 논문 landing page 또는 공개 PDF를 다시 읽고 hash한 경우에만 claim 근거가 될 수 있습니다.

2026년 8월 공식 요금 기준으로 [Tavily](https://www.tavily.com/pricing)는 카드 없이 월 1,000 credits, [Exa](https://exa.ai/pricing)는 월 $10 credits, [Brave](https://brave.com/search/api/)는 결제 수단 등록 후 월 $5 credits를 제공합니다. 무료 조건은 공급자가 바꿀 수 있으므로 배포 전 링크에서 다시 확인하세요. Google Custom Search JSON API는 신규 고객 가입이 닫혔고 2027년 1월 1일 종료 예정이므로 기존 자격 증명 호환용으로만 남겨 두었습니다.

```bash
# API providers
export TAVILY_API_KEY='...'
export BRAVE_SEARCH_API_KEY='...'
export EXA_API_KEY='...'

# Scholarly discovery: registered free key, limited daily allowance
export OPENALEX_API_KEY='...'
export BUILDUP_SEARCH_PROVIDER=openalex

# Self-hosted SearXNG
export BUILDUP_SEARXNG_URL='http://127.0.0.1:8080'
export BUILDUP_SEARCH_PROVIDER=searxng

# Optional local browser: google, brave, or bing
export BUILDUP_BROWSER_SEARCH_ENABLED=true
export BUILDUP_BROWSER_SEARCH_ENGINE=google
export BUILDUP_SEARCH_PROVIDER=browser
```

공급자 응답은 공통 schema로 정규화되고 query/provider/language별 로컬 cache에 저장됩니다. API key는 cache나 연구 artifact에 저장하지 않습니다.
공급자 계약상 응답 저장이 허용되지 않거나 cache를 원하지 않으면
`BUILDUP_SEARCH_CACHE_TTL_HOURS=0`으로 query/page cache 쓰기까지 완전히 끌 수 있습니다.

## 빠른 시작

```bash
buildup job new agent-harness --tpl research
buildup research "로컬 LLM용 deep-research agent harness의 설계와 한계"
```

깊이와 hard budget을 직접 정할 수 있습니다. CLI의 budget 값은 환경 설정에 둔 상한을 낮출 수만 있으며, 0·음수·상한 초과 값은 조용히 보정하지 않고 오류로 거부합니다.

```bash
buildup research "주제" \
  --depth deep \
  --max-rounds 3 \
  --max-searches 20 \
  --max-sources 24 \
  --max-results-per-search 5
```

특정 run을 지식에 넣지 않으려면 `--no-wiki`를 붙입니다. 이미 자동 반영된 최근 proposal은 `buildup wiki rollback latest`로 원장을 삭제하지 않고 비활성화할 수 있습니다.

`--depth auto`는 질문의 복잡도에 따라 `shallow`, `standard`, `deep` 중 하나를 선택합니다.

확인, 재개, 오프라인 평가는 다음과 같습니다.

```bash
buildup research --list
buildup research --show 1
buildup research --resume 1
buildup research --evaluate 1
```

Interactive shell:

```text
/research 조사할 주제
/research list
/research show 1
/research resume 1
/research eval 1
```

`딥 리서치`, `심층 리서치`, `deep research`가 명시된 자연어 요청은 일반 chat보다 먼저 deterministic rule로 routing됩니다.

## Compounding Knowledge Vault

완료된 Deep Research는 추가 model call 없이 현재 job의 기본 vault에 자동 반영됩니다. 수집·정규화한 source 본문과 원 PDF, research artifact는 source of truth로 봉인된 채 유지되고, Wiki는 언제든 원장에서 재생성할 수 있는 파생 지식입니다. 이 구조는 raw source와 누적 Wiki를 분리하고 domain별 schema·ingest·query·lint를 두라는 [Karpathy의 LLM Wiki 방법론](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)을 Build-up의 evidence ledger에 맞게 구현한 것입니다.

```text
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

핵심 불변식은 다음과 같습니다.

- Wiki 페이지 자체를 새 claim의 근거로 사용할 수 없습니다.
- 모든 claim은 원 research Source/Evidence ID, URL, artifact 경로, source/passage SHA-256을 가집니다. PDF source는 원 PDF 경로·byte hash·크기까지 추적합니다.
- `draft → supported → verified|disputed|stale|rejected` 상태를 append-only event로 기록합니다.
- citation audit, claim audit, manifest를 모두 통과한 완료 run만 `supported`로 자동 반영합니다.
- 같은 URL의 source hash가 바뀌면 재지지되지 않은 기존 claim을 `stale`로 표시합니다.
- contradiction의 승자를 자동으로 결정하지 않고 claim과 counter-evidence를 함께 보존합니다.
- 기존 지식은 자동 삭제·대체하지 않습니다. rollback도 삭제가 아니라 취소 event입니다.
- `verified`는 사용자가 직접 설명하거나 확인한 supported claim에만 명시적으로 부여됩니다.
- job은 기본적으로 자기 vault만 사용하며 다른 vault bind는 별도 확인이 필요합니다.

```bash
buildup wiki status
buildup wiki add latest
buildup wiki ask "MoE routing"
buildup wiki review
buildup wiki lint
buildup wiki verify C0123456789ABCD
buildup wiki reject C0123456789ABCD --reason "원문과 불일치"
buildup wiki rollback KP0123456789ABCDEFGHIJ
buildup wiki bind shared-research --confirm
```

Interactive shell에서는 `/wiki add`, `/wiki ask`, `/wiki review`, `/wiki lint`를 쓸 수 있고, `이 리서치를 지식에 반영해줘`, `내가 MoE에 대해 지금까지 뭘 알고 있지?`, `위키 상태 점검해줘`도 deterministic rule로 처리합니다. Study 안에서 직접 검증했다면 `/study verify CLAIM_ID`로 승격합니다.

기본 자동화는 `auto_ingest=on`, `auto_compile=on`, `auto_lint=on`입니다. `auto_verify`, destructive edit, cross-vault sharing은 설정으로도 자동 활성화할 수 없습니다.

## 연구 루프

```text
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

외부 page는 untrusted data로 취급됩니다. Source 안의 prompt나 명령은 실행하지 않으며 writer와 auditor가 참조할 수 있는 ID도 checkpoint에 저장된 값으로 제한합니다.

## 실행 산출물

모든 run은 현재 job의 `deep-research/<run-id>/`에 남습니다.

```text
deep-research/<run-id>/
├── 00-sources.json          # 이전 Build-up artifact reader용 snapshot
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
├── 10-evaluation.json       # evaluate 실행 후
├── raw-sources/              # reader가 가져온 원 PDF bytes (해당하는 경우)
├── state.json               # resumable source of truth
├── events.jsonl
├── metadata.json
└── manifest.json
```

Source ID는 canonical URL, Evidence ID는 source ID와 exact passage, Claim ID는 atomic claim text의 SHA-256 prefix로 결정됩니다. `manifest.json`은 최종 artifact와 보존된 원 PDF의 크기·SHA-256을 봉인합니다.

## 모델과 budget 설정

| Environment variable | 기본값 | 용도 |
|---|---:|---|
| `BUILDUP_BASE_DIR` | `~/.buildup` | local data root |
| `BUILDUP_RESEARCH_MODEL` | `qwen3.8:27b` | plan, evidence, assessment, writer |
| `BUILDUP_REVIEWER_MODEL` | `deepseek-r1:32b` | independent claim audit |
| `BUILDUP_ENGLISH_BRIEF` | `false` | 한국어 답변 뒤 English Brief 섹션 추가 |
| `BUILDUP_FAST_MODEL` | `gemma4:e4b` | quick chat |
| `BUILDUP_MAIN_MODEL` | `qwen3.8:27b` | general reasoning |
| `BUILDUP_CODER_MODEL` | `qwen3.8:27b` | code tasks |
| `BUILDUP_SEARCH_PROVIDER` | `auto` | provider selection |
| `OPENALEX_API_KEY` | — | keyed scholarly discovery (limited free daily allowance) |
| `BUILDUP_SEARCH_TIMEOUT` | `30` | network timeout |
| `BUILDUP_SEARCH_CACHE_TTL_HOURS` | `24` | query/page cache TTL |
| `BUILDUP_RESPECT_ROBOTS_TXT` | `true` | page-reader robots policy |
| `BUILDUP_RESEARCH_DEPTH` | `auto` | default depth |
| `BUILDUP_RESEARCH_MAX_ROUNDS` | `3` | hard round cap |
| `BUILDUP_RESEARCH_MAX_TASKS` | `8` | hard task cap |
| `BUILDUP_RESEARCH_MAX_SEARCHES` | `24` | hard query cap |
| `BUILDUP_RESEARCH_MAX_SOURCES` | `30` | hard source cap |
| `BUILDUP_RESEARCH_MAX_MODEL_CALLS` | `20` | hard inference cap |
| `BUILDUP_RESEARCH_MAX_RUNTIME_SECONDS` | `1800` | active runtime cap |
| `BUILDUP_RESEARCH_MIN_COVERAGE` | `0.85` | standard depth gate |
| `BUILDUP_KNOWLEDGE_AUTO_INGEST` | `true` | completed run → append-only proposal 적용 |
| `BUILDUP_KNOWLEDGE_AUTO_COMPILE` | `true` | ledger → Markdown Wiki 재생성 |
| `BUILDUP_KNOWLEDGE_AUTO_LINT` | `true` | schema·citation·staleness 자동 검사 |

제품 설정은 `BUILDUP_*` 이름을 사용하고, 검색 API secret은 각 공급자의 표준 환경변수 이름을 유지합니다.

## 일반 기능

- workspace-scoped SQLite conversation과 FTS 검색
- 논문 library, page-aware PDF 추출과 한국어 번역
- 연구 결과를 잇는 Socratic study와 D+1/D+7/D+30 review
- 원본 citation을 보존하는 append-only Knowledge Vault와 Study 기반 verified 승격
- file/calendar/job 관리
- Tier-0/Tier-1/Tier-2 intent dispatch, constrained plan state machine, ReAct fallback
- workspace path sandbox, soft delete, exact-argument process/Git confirmation
- local `SKILL.md` discovery와 audit

| Command | 기능 |
|---|---|
| `buildup` | interactive shell |
| `buildup doctor` | model, storage, search, skill readiness |
| `buildup research QUERY` | adaptive deep research |
| `buildup wiki status|add|ask|review|lint` | grounded compounding knowledge |
| `buildup search QUERY` | lightweight web search + answer |
| `buildup translate PDF` | Korean paper translation |
| `buildup study start TOPIC` | personal study workflow |
| `buildup sessions` | current-workspace transcripts |
| `buildup skills audit` | packaged/local skill validation |

## Data와 보안

```text
~/.buildup/
├── data/buildup/
│   ├── state.db
│   ├── traces.jsonl
│   └── search-cache/
├── workspace/<job-id>/
├── knowledge/vaults/<vault-id>/
├── library/papers/
├── calendar/
├── export/
└── trash/
```

- Page reader는 HTTP(S)만 허용하고 localhost, private, link-local, `.local` target과 위험한 redirect를 차단합니다.
- robots.txt, response size, redirect count, content type, timeout을 검사합니다.
- PDF와 HTML에서 추출한 본문은 길이 제한 후 hash와 함께 저장합니다. reader가 직접 받은 PDF는 추출문과 별도로 run 내부에 원 byte를 보존하며 PDF cache를 재사용하지 않습니다.
- 실패한 model response는 성공 artifact로 취급하지 않고 diagnostic 파일로 격리합니다.
- 에이전트의 process/Git 도구는 실행 전 정확한 인수를 표시하고 승인을 요구합니다. Process 실행은 `shell=True`를 쓰지 않으며 파이프·리다이렉션·명령 치환을 거부하고 시간·출력 크기를 제한합니다.
- 사용자가 직접 입력하는 `/shell`은 명시적인 power-user 기능입니다. 단일 허용 명령만 실행하지만 OS 수준 컨테이너가 아니므로, 그 명령 자체가 접근할 수 있는 로컬 파일·네트워크 권한까지 격리하지는 않습니다.
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

테스트는 live model이나 network를 요구하지 않습니다. Callback을 주입해 adaptive follow-up, hard budget, checkpoint/resume, exact passage, citation integrity, provider failover/cache, Knowledge Vault provenance/status/rollback, OpenAlex normalization을 검증합니다.

## 의도적인 한계

- 검색 품질과 사용 가능성은 선택한 공급자 및 검색 엔진 정책에 영향을 받습니다.
- API 응답 저장 권한과 browser 검색 자동화 허용 범위는 각 공급자·검색 엔진의 현재 약관을 확인해야 합니다.
- Exact passage 검사는 출처가 문장을 포함함을 보장하지만 문장이 참이라는 것까지 보장하지는 않습니다.
- Reviewer는 독립 model pass이지만 같은 model family의 편향을 완전히 제거하지 못합니다.
- JavaScript-only page는 일반 reader에서 본문이 부족할 수 있습니다.
- OpenAlex metadata는 원 논문 전문이 아니며, reader가 원 landing page/PDF를 확보하지 못하면 evidence로 채택되지 않습니다.
- 일반 HTML은 정규화된 본문과 URL/hash를 봉인합니다. 원 byte 보존은 현재 PDF source에만 적용합니다.
- 법률·의료·금융처럼 고위험인 결론에는 전문가 검토가 필요합니다.

## 문서

- 내부 모듈 경계: [router/buildup/README.md](router/buildup/README.md)
- 변경 내역: [CHANGELOG.md](CHANGELOG.md)
- 기여 방법: [CONTRIBUTING.md](CONTRIBUTING.md)
- 보안 제보: [SECURITY.md](SECURITY.md)
