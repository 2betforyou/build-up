# build-up package architecture

사용자 문서와 설치 방법은 repository root의 [`README.md`](../../README.md)를 기준으로 합니다. 이 문서는 `router/friday/` 내부 구현을 변경할 때 지켜야 할 경계를 설명합니다. Python package 이름 `friday`는 기존 설치와 import 호환성을 위해 유지합니다.

## Product order

1. `deep_research.py`: source-grounded deep research
2. `study.py`: research 결과를 이어받는 personal study
3. paper translation, general chat, file/calendar utilities

새 기능이 이 순서를 흐리거나 일반 chat이 명시적 딥 리서치 요청을 가로채면 안 됩니다.

## Request flow

```text
user input
  ├─ slash command / exact natural-language rule
  ├─ Tier 0 chat heuristic
  ├─ Tier 1 regex intent
  ├─ Tier 1 embedding classifier
  └─ Tier 2 ReAct agent
```

`딥 리서치`, `심층 리서치`, `deep research`는 `intent.py`의 deterministic rule에서 `deep_research`로 분류합니다. 대화형 실행은 `shell.py:_cmd_research()`, one-shot 실행은 `cli.py`가 모두 `run_deep_research()`로 수렴합니다.

## Deep research invariants

`deep_research.py`는 다음 순서를 갖습니다.

```text
deterministic query portfolio
  → web search and public-page hydration (0 model calls)
  → normalized source packet
  → one Ollama JSON-schema inference
  → strict local validation
  → atomic artifacts + metadata + checksums
```

반드시 지킬 조건:

- model call은 성공·실패를 포함해 최대 1회입니다.
- 자동 repair, critique, formatting model call을 추가하지 않습니다.
- stage key의 순서와 schema가 다르면 실패합니다.
- supplied source ID 외 인용과 citation 없는 final report는 실패합니다.
- source 본문은 untrusted data이며 내부 명령을 실행하지 않습니다.
- stage는 구조적으로 분리되지만 독립 process/model session이라고 표현하지 않습니다.
- `metadata.json`은 retrieval 전에 `running`으로 만들고 failure phase를 남깁니다.

## Sessions

`session_store.py`의 SQLite가 source of truth입니다.

```text
data/night/state.db
├── sessions
├── messages
└── messages_fts (FTS5 available일 때)
```

세션 invariant:

- `workspace_key`는 job 우선, job이 없으면 repository root/cwd입니다.
- 자동 복원, 번호, title resolution, 검색은 기본적으로 현재 workspace에 한정합니다.
- `ConversationHistory.max_turns`는 model prompt window만 제한하며 transcript를 자르지 않습니다.
- memory는 `user`와 `workspace` scope만 허용하고 session 시작 시 snapshot으로 고정합니다.
- session 전환은 destination lock을 먼저 얻고 current session을 lock 상태에서 저장합니다.
- UI 삭제는 hard delete가 아니라 archive입니다.
- `sessions/*.session.json`은 한 번 import한 뒤 원본을 유지합니다.

## Study

`study.py`는 job-scoped manifest와 Markdown 파일을 만듭니다.

```text
workspace/<job>/study/<study-id>/
├── study.json
├── README.md
├── notes.md
├── questions.md
└── progress.md
```

최신 completed research run이 있으면 source로 연결합니다. Verified note는 학습자의 설명·예제·반례를 확인한 뒤 명시적으로 저장합니다. D+1/D+7/D+30 review는 idempotent calendar key로 등록하며, 완료 회차는 `completed_reviews`에 남겨 due 목록에서 제외합니다.

## Important modules

| Module | Responsibility |
|---|---|
| `cli.py` | argparse and one-shot command routing |
| `shell.py` | interactive REPL, session lifecycle, user commands |
| `deep_research.py` | one-inference research contract and artifacts |
| `study.py` | Socratic study state and spaced review |
| `session_store.py` | SQLite schema, migration, FTS, export, process lease |
| `conversation.py` | durable transcript plus bounded active context |
| `conversation_memory.py` | scoped cross-session memory |
| `search.py` | Tavily, Google, DuckDuckGo, safe page hydration |
| `paper_translation.py` | faithful page/chunk PDF translation |
| `intent.py` | deterministic and model-assisted intent parsing |
| `agent.py` / `tool_registry.py` | general ReAct agent and tool dispatch |
| `sandbox.py` / `permission.py` | path and action policy |
| `skills.py` | local `SKILL.md` discovery and injection |
| `doctor.py` | runtime readiness and SQLite integrity checks |

## Configuration and compatibility

`config.py:load_config()` reads `NIGHT_*` first and legacy `FRIDAY_*` second. Runtime code should use the immutable `FridayConfig` object rather than reading environment variables directly. These prefixes are compatibility interfaces from before the build-up rename.

The root is `NIGHT_BASE_DIR` (default `~/FridayLocal`). File writes must remain in the current job workspace or an explicitly managed build-up data directory.

## Add a tool

Register a `ToolDef` in `tool_registry.py` with a description, risk level, required capability, and executor. Do not bypass `execute_tool()` or the permission/sandbox layers.

## Add a command

For a slash command:

1. add the handler to `InteractiveShell._commands`;
2. add completion in `COMMANDS`;
3. update `rendering.help_text()`;
4. add equivalent CLI behavior when the operation is useful non-interactively;
5. add a regression test.

Natural-language shortcuts must be deterministic and narrow. Ambiguous requests should continue through the normal router.

## Verification

From the repository root:

```bash
PYTHONPATH=router python -m py_compile router/friday/*.py tests/*.py
PYTHONPATH=router python -W error::ResourceWarning -m unittest discover -s tests -v
buildup doctor
buildup skills audit
```

Tests must not require a live model or network. Inject `search_fn` and `chat_fn` into deep research tests and assert the one-call contract directly.
