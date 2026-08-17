# Build-up

**Language:** [한국어](README.md) | English

An evidence-first deep-research CLI that connects local models to the live web and builds a verifiable evidence ledger. It decomposes a research question into a contract and subtasks, accepts only evidence that exactly matches the source text, and folds every completed run into an append-only Knowledge Vault.

## Install

Requires Python 3.10+, Ollama, a research/reviewer model pair, and at least one search provider.

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

For local browser search:

```bash
python -m pip install -e '.[browser]'
playwright install chromium
```

The `./buildup` launcher also works without installing.

## Getting started

```bash
buildup
```

Once the shell is open, create a job and run research:

```
/job new agent-harness --tpl research
/research designing a local-LLM deep-research agent harness and its limits
```

Natural-language requests containing "deep research" or "심층 리서치" are routed the same way.

## Deep research

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

- A research contract fixes the purpose, scope, subquestions, source requirements, and success criteria up front.
- The loop repeats `plan → search/read → evidence → claims/gaps → follow-up` until sufficient or the budget runs out.
- Evidence is accepted only when the passage is an exact normalized substring of the stored source text.
- Raw PDF bytes fetched directly by the reader are sealed per run under `raw-sources/` with a SHA-256 manifest.
- Source/Evidence/Claim IDs are derived deterministically from the URL, passage, and claim text.
- The writer cannot search and cannot add a URL or piece of evidence outside the registry.
- A run only completes after passing both a deterministic citation audit and an independent reviewer model's claim-support audit.
- Hard budgets on round, task, search, source, model-call, and runtime counts accumulate in the checkpoint.
- Each phase and task leaves an atomic checkpoint; resuming never repeats completed work.
- A process lease ensures only one process mutates a given run at a time.
- An offline evaluation re-verifies evidence, citation, and artifact hashes without a model or network.

External pages are treated as untrusted data — instructions or commands inside them are never executed.

Shell commands:

```
/research TOPIC
/research --no-wiki TOPIC        Run once without Knowledge Vault ingestion
/research list                   List research runs in this job
/research show 1                 Open a report and its study guide
/research resume 1               Resume from a durable checkpoint
/research eval 1                 Recompute offline quality gates
```

Every run is stored under `deep-research/<run-id>/` in the current job.

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
├── 10-evaluation.json         # created after eval
├── raw-sources/                # raw PDF bytes fetched by the reader
├── state.json                  # resumable source of truth
├── events.jsonl
├── metadata.json
└── manifest.json
```

## Knowledge Vault

A completed deep-research run is folded into the current job's vault automatically, with no extra model calls. The collected source text, original PDFs, and research artifacts remain a sealed ledger; the Wiki is derived knowledge that can always be regenerated from that ledger.

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

- A Wiki page is never itself evidence for a new claim.
- Every claim carries the original research Source/Evidence ID, URL, artifact path, and source/passage SHA-256; PDF sources also track the original byte hash and size.
- Status moves through `draft → supported → verified|disputed|stale|rejected` as append-only events.
- Only a completed run that passes citation audit, claim audit, and manifest checks is auto-ingested as `supported`.
- If a source's hash changes at the same URL, claims that lost their support are marked `stale`.
- Contradictions preserve the claim and the counter-evidence side by side rather than picking a winner automatically.
- A rollback is appended as a cancellation event, not a deletion.
- `verified` is granted only to a supported claim the user explicitly explained or checked.
- A job uses its own vault by default; binding another vault requires explicit confirmation.
- `auto_ingest`, `auto_compile`, and `auto_lint` are on by default. `auto_verify`, destructive edits, and cross-vault sharing always require explicit confirmation.

Shell commands:

```
/wiki status                  Show current job's vault state
/wiki add [RUN]                Ingest a sealed research run
/wiki ask QUERY                Query grounded claims with original citations
/wiki review                   Review conflicts/staleness
/wiki lint                     Audit the vault
/wiki verify CLAIM_ID          Promote a claim via user verification
/wiki reject CLAIM_ID          Reject without deleting history
/wiki rollback PROPOSAL_ID     Append a non-destructive rollback event
/wiki bind VAULT_ID            Explicitly share/bind another vault (confirmation)
```

Natural language such as "add this research to knowledge", "what do I already know about MoE?", or "check the wiki status" is handled the same way.

## Search setup

`BUILDUP_SEARCH_PROVIDER=auto` tries configured providers in this order:

1. Tavily
2. Brave Search API
3. Exa
4. SearXNG
5. Google Custom Search (legacy credential compatibility)
6. opt-in local Chromium
7. OpenAlex fallback

Queries carrying scholarly intent — "paper", "journal", "DOI", "논문", "학술", "데이터셋" — move keyed OpenAlex to the front. You can also pin it with `BUILDUP_SEARCH_PROVIDER=openalex`.

Without an API key, use a self-hosted or trusted SearXNG endpoint, or Chromium browser search over your machine's own internet connection. Browser search is off by default, and a CAPTCHA or consent screen is recorded as a failure and stops the attempt on the spot.

OpenAlex is for paper discovery and activates when a registered `OPENALEX_API_KEY` is set. Its abstracts and metadata are only a search hint — they become evidence only once the reader re-fetches and hashes the original landing page or public PDF.

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

Provider responses are normalized to a common schema and cached locally per query/provider/language. API keys are never written to cache or research artifacts. Set `BUILDUP_SEARCH_CACHE_TTL_HOURS=0` to disable query/page cache writes entirely.

## Jobs and files

A job is a workspace unit. By default, files are created and edited only inside `workspace/<job-id>/`; an existing folder can be bound in place instead of copied in.

```
/job new [name] [--tpl TPL]     Create a job (templates: paper / code / report / research / ops)
/job use JOB_ID                 Switch job
/job rename NEW_NAME             Rename for display
/job bind PATH [--write]         Attach a real directory to the current job (read-only by default)
/job unbind PATH                 Detach a bound directory
/job binds                       List bound directories
/job list                        List all jobs
/job current                     Show current job and its binds
/job log                         Show action log
/job summary                     AI summary of the action log
/templates                       List available templates
```

```
/import PATH                     Copy a file/folder into the current job
/files                           List files in the current job
/read RELPATH                    View a text file
/readpdf RELPATH                 Extract text from a PDF
/rewrite RELPATH :: instruction  Rewrite a file (shows diff)
/diff FILE_A FILE_B               Compare two files
/trash RELPATH                   Move a file to trash
/export [DEST]                   Export the current job
/edit RELPATH :: OLD :: NEW      Precise string replacement in a file (shows diff)
/glob PATTERN                    Glob search in the job folder
/grep PATTERN [-- PATH_GLOB]     Regex search in file contents
```

A bind is read-only by default; pass `--write` to open write access. Bound paths are included in `/files`, `/read`, `/glob`, and `/grep`, and only a writable bind allows saving or editing files there. A bind can never cover Build-up's own data root, all of `$HOME`, the filesystem root, a symlink, or a path outside the bind's own scope.

## Sessions

Running `buildup` automatically resumes the current job's most recent conversation.

```
/new                              Start a new conversation
/sessions                         List conversations in this workspace
/sessions QUERY                   Full-text search over conversation history
/resume                           Pick from recent conversations (across the whole job)
/resume QUERY|NUMBER|latest        Jump straight to a title fragment, number, or the latest
/resume prev                      Go to the previous conversation
/title TITLE                      Name the current conversation
/session rename NUMBER TITLE       Rename a conversation
/session archive NUMBER            Archive a conversation
/session export NUMBER [md|json]  Export a conversation
```

Natural language such as "resume my last conversation", "show my session list", or "start a new conversation" is handled the same way.

## Modes and other features

```
/mode auto|fast|main|refine       Change response mode
데이타임 / 데이모드                 Switch to everyday assistant mode
리서치타임 / 리서치모드             Switch to deep-research-first mode
/paper list|use|current           Browse the paper shelf and pick the active paper
/translate PDF|PAPER_ID            Translate a paper PDF into Korean Markdown
/skills list|search|show|audit     Manage installed Agent Skills
/steer show|list|profile|set        Adjust session-scoped response style
/memory show [--all]                Show durable memory
/memory add [workspace|user] TEXT   Add durable memory
/prefs                             Show user prefs and project context
/intent-debug TEXT                  See how a natural-language input is framed as a task
/history                           Show conversation history
/undo                              Remove only the last chat turn
/retry                             Retry the last ordinary chat turn
/compact                           Summarize old context (full log is kept)
/search QUERY                      Web search + AI answer
```

## Model and budget settings

| Environment variable | Default | Purpose |
|---|---:|---|
| `BUILDUP_BASE_DIR` | `~/.buildup` | Local data root |
| `BUILDUP_RESEARCH_MODEL` | `qwen3.8:27b` | Plan, evidence, assessment, writer |
| `BUILDUP_REVIEWER_MODEL` | `deepseek-r1:32b` | Independent claim audit |
| `BUILDUP_ENGLISH_BRIEF` | `false` | Add an English Brief section after Korean answers |
| `BUILDUP_FAST_MODEL` | `gemma4:e4b` | Quick chat |
| `BUILDUP_MAIN_MODEL` | `qwen3.8:27b` | General reasoning |
| `BUILDUP_CODER_MODEL` | `qwen3.8:27b` | Code tasks |
| `BUILDUP_SEARCH_PROVIDER` | `auto` | Provider selection |
| `OPENALEX_API_KEY` | — | Scholarly search key |
| `BUILDUP_SEARCH_TIMEOUT` | `30` | Network timeout |
| `BUILDUP_SEARCH_CACHE_TTL_HOURS` | `24` | Query/page cache TTL |
| `BUILDUP_RESPECT_ROBOTS_TXT` | `true` | Page-reader robots policy |
| `BUILDUP_RESEARCH_DEPTH` | `auto` | Default depth |
| `BUILDUP_RESEARCH_MAX_ROUNDS` | `3` | Hard round cap |
| `BUILDUP_RESEARCH_MAX_TASKS` | `8` | Hard task cap |
| `BUILDUP_RESEARCH_MAX_SEARCHES` | `24` | Hard query cap |
| `BUILDUP_RESEARCH_MAX_SOURCES` | `30` | Hard source cap |
| `BUILDUP_RESEARCH_MAX_MODEL_CALLS` | `20` | Hard inference cap |
| `BUILDUP_RESEARCH_MAX_RUNTIME_SECONDS` | `1800` | Active runtime cap |
| `BUILDUP_RESEARCH_MIN_COVERAGE` | `0.85` | Standard-depth pass threshold |
| `BUILDUP_KNOWLEDGE_AUTO_INGEST` | `true` | Completed run → append-only proposal |
| `BUILDUP_KNOWLEDGE_AUTO_COMPILE` | `true` | Ledger → regenerated Markdown Wiki |
| `BUILDUP_KNOWLEDGE_AUTO_LINT` | `true` | Automatic schema/citation/staleness checks |

A value of zero, negative, or above the configured ceiling is rejected as an error rather than silently clamped.

## Data and security

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

- The page reader only allows HTTP(S) and blocks localhost, private, link-local, and `.local` targets along with dangerous redirects.
- robots.txt, response size, redirect count, content type, and timeout are all checked.
- Text extracted from PDFs and HTML is length-capped and stored with a hash. A PDF fetched directly by the reader also has its raw bytes preserved inside the run, separately from the extracted text.
- A failed model response is quarantined as a diagnostic file rather than treated as a successful artifact.
- File writes are allowed only inside the current job workspace and any directory explicitly bound with `--write`.
- Web queries and public page requests go to the search provider or engine you configured.
- Pointing the Ollama URL at a remote endpoint sends model prompts to that endpoint too.
- Workspace, API keys, the session DB, cache, and logs are all Git-ignored.

## Development and verification

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=router python -m compileall -q router/buildup tests
PYTHONPATH=router python -W error::ResourceWarning -m unittest discover -s tests -v
ruff check router tests
python -m build
```

Tests require no live model or network. Injected callbacks verify adaptive follow-up, hard budgets, checkpoint/resume, exact-passage matching, citation integrity, provider failover/cache, and Knowledge Vault provenance/status/rollback.

## Known limits

- Search quality depends on the provider and search-engine policy you selected.
- An exact-passage check guarantees the source contains the sentence, not that the sentence is true.
- The reviewer is an independent model pass but doesn't fully remove bias shared within the same model family.
- JavaScript-only pages may leave the reader with too little body text.
- OpenAlex metadata is not the full paper text — it becomes evidence only once the reader secures the original landing page or PDF.
- Raw-byte preservation currently applies only to PDF sources; plain HTML is sealed as normalized text plus URL/hash.
- High-stakes conclusions in law, medicine, or finance need expert review.

## Docs

- Internal module boundaries: [router/buildup/README.md](router/buildup/README.md)
- Changelog: [CHANGELOG.md](CHANGELOG.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security: [SECURITY.md](SECURITY.md)
