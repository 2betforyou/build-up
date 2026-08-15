# Build-up package architecture

Public setup and usage live in the repository [README](../../README.md). This file defines implementation boundaries for `router/buildup/`.

## Product order

1. evidence-first deep research
2. append-only compounding Knowledge Vault
3. research-linked personal study
4. paper translation and active-paper analysis
5. general chat, code, file, calendar, and job utilities

Explicit deep-research requests must not fall through to ordinary chat.

## Dispatch

```text
slash command / deterministic natural-language rule
  -> Tier 0 chat heuristic
  -> Tier 1 regex intent
  -> Tier 1 embedding classifier
  -> Tier 2 constrained PlanStateMachine
       -> bounded ReAct fallback on planning failure
```

`딥 리서치`, `심층 리서치`, and `deep research` are recognized by a narrow deterministic rule. Interactive and one-shot entry points converge on `run_deep_research()`.

## Research package

| Module | Responsibility |
|---|---|
| `research/models.py` | durable Contract, Task, Budget, Source, Evidence, Claim, Gap, State |
| `research/providers.py` | query-aware Tavily, Brave, Exa, OpenAlex, SearXNG, Google CSE, browser normalization/cache/failover |
| `research/reader.py` | SSRF-aware redirects, robots, size caps, HTML/PDF extraction, page cache, original-PDF bytes |
| `research/citations.py` | exact-passage verification, citation integrity, deterministic bibliography |
| `research/prompts.py` | strict phase schemas and untrusted-source boundaries |
| `research/engine.py` | adaptive rounds, task isolation, budgets, writer and reviewer |
| `research/store.py` | atomic checkpoint/artifacts, raw-PDF archive, event log, single-writer lease, manifest |
| `research/evaluation.py` | offline recomputation of release quality gates |
| `deep_research.py` | stable CLI/shell facade and run selection |

## Knowledge package

| Module | Responsibility |
|---|---|
| `knowledge/models.py` | public status, ingest, query, lint, and summary values |
| `knowledge/store.py` | dedicated sandbox gate, append-only ledgers, immutable proposals, lease, compiled views |
| `knowledge/service.py` | sealed-run ingestion, provenance validation, staleness, contradiction preservation, query, review, rollback |

Knowledge ingestion may read only a completed research run whose citation audit, independent claim audit, and exact artifact manifest pass. It never changes the research run. Wiki Markdown is a deterministic view; only source and claim event ledgers can ground it. When a source is a directly fetched PDF, its archived byte path/hash is carried into the source registry. User verification, rejection, rollback, and cross-vault binding are explicit transitions.

## Research invariants

- The research contract is created before retrieval.
- Search requests and source/model/runtime consumption never exceed persisted hard caps.
- Search fan-out may run concurrently; model calls and state merges are serialized by default.
- Every accepted Evidence passage is an exact normalized substring of stored Source content.
- IDs supplied by a model are intersected with local registries; unknown IDs are discarded or fail a gate.
- A writer sees claims/evidence only and has no search callback.
- A Sources appendix is built in code from evidence actually cited by the report.
- Completion requires deterministic citation integrity plus an independent claim-support pass.
- A failed quality gate leaves inspectable artifacts and does not masquerade as a completed run.
- Resume reuses completed tasks and stable IDs; a process lease prevents concurrent mutation.
- Tests never require live network or models.

## Other important modules

| Module | Responsibility |
|---|---|
| `cli.py` | argparse and one-shot routing |
| `shell.py` | interactive session lifecycle and commands |
| `study.py` | Socratic study state and spaced review |
| `session_store.py` | SQLite schema, FTS, migration, export, process lease |
| `conversation.py` | durable transcript and bounded prompt context |
| `paper*.py` | paper library, extraction, memory, review, translation |
| `state_machine.py` / `agent.py` / `tool_registry.py` | constrained planning, guarded tools, bounded ReAct fallback |
| `sandbox.py` / `permission.py` | job/library/knowledge path gates and action policy |
| `command_runner.py` / `git_mgr.py` | approved direct-process and restricted Git execution |
| `skills.py` | packaged and local Agent Skill discovery |
| `doctor.py` | runtime, model, storage, search, and skill readiness |

## Configuration

`config.py` exposes only `BUILDUP_*` product variables. The default data root is `~/.buildup` and managed state lives under `data/buildup`. Do not read environment variables directly outside config loading.

File writes must pass the relevant sandbox gate. Research artifacts live inside the owning job workspace; Knowledge Vault writes live only below `knowledge/`; shared cache and session state live only in explicit Build-up data directories.

A job may also reach one or more directories the user bound to it with `job bind`. Binds are read-only unless created with `--write`, resolve through `jobs.resolve_job_file()`, and widen `sandbox.job_read_roots()` / `sandbox.job_write_roots()`. A bind may never cover the data root or `$HOME`.

## Verification

```bash
PYTHONPATH=router python -m compileall -q router/buildup tests
PYTHONPATH=router python -W error::ResourceWarning -m unittest discover -s tests -v
ruff check router tests
python -m build
```

For research tests inject `search_fn`, `reader_fn`, and `chat_fn`. Assert state transitions and persisted artifacts, not prompt wording.
