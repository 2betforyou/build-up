# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running Friday

```bash
# Interactive shell (default)
python3 ~/FridayLocal/router/friday/__main__.py
# or via helper script
python3 ~/FridayLocal/router/run_friday.py

# One-shot subcommands
python3 -m friday ask "질문"
python3 -m friday job new my-task
python3 -m friday cal list
python3 -m friday search "검색어"
```

All source lives in `router/friday/`. Run from that package root or add it to `PYTHONPATH`.

## Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `FRIDAY_BASE_DIR` | `~/FridayLocal` | Data root (workspace, logs, state) |
| `FRIDAY_FAST_MODEL` | `qwen3.5:9b` | Tier-0 chat / quick answers |
| `FRIDAY_MAIN_MODEL` | `exaone-deep:32b` | Deep reasoning |
| `FRIDAY_CODER_MODEL` | `qwen3-coder-30b-instruct` | Code tasks |
| `FRIDAY_EMBED_MODEL` | `nomic-embed-text` | Embedding classifier |
| `FRIDAY_STRUCT_MODEL` | `qwen2.5:1.5b` | Intent structuring (5.2 layer) — replace with fine-tuned model as traces accumulate |
| `FRIDAY_OLLAMA_URL` | `http://localhost:11434/api/chat` | Ollama LLM endpoint |
| `FRIDAY_OLLAMA_EMBED_URL` | `http://localhost:11434/api/embed` | Ollama embed endpoint |
| `TAVILY_API_KEY` | — | Web search (Tavily) |
| `OPENWEBUI_TOKEN` | — | Open WebUI KB integration |

## Architecture: 3-Tier Dispatch

`shell.py:_dispatch()` is the central dispatch hub. Each user message flows through tiers in order:

```
User input
  │
  ├─ Tier-0: is_chat() [intent.py]
  │    Heuristic: greetings, pure questions → answer_query_stream → render_streaming_answer
  │
  ├─ Tier-1a: parse_intent_instant() [intent.py]
  │    Regex rules → immediate action (0 LLM calls, ~1ms)
  │
  ├─ Tier-1b: IntentEmbedClassifier.classify_and_extract() [embed_classifier.py]
  │    Cosine similarity vs ~130 labelled examples → intent + confidence
  │    Falls back silently to Tier-2 if embed model unavailable
  │
  └─ Tier-2: run_agent() [agent.py]
       ReAct loop: Think → Act → Observe (max 8 steps)
       Tools dispatched through tool_registry.py
```

**Confidence → Policy**: `permission.py:evaluate_intent_policy()` uses intent + confidence to decide auto/confirm/deny. Confidence < 0.85 on write intents triggers confirmation prompt.

## Agent Loop (`agent.py`)

- `run_agent()` builds a system prompt with tool descriptions + exemplar context, then loops: LLM → `_parse_action()` → `execute_tool()` → inject observation
- `_parse_action()` scans all `{` positions in LLM output (model may wrap JSON in `<thought>` prose); requires `"action"` key
- Each step is timed; `tracer.add_step()` records it
- After Tier-2 success: `tracer.flush()` → `exemplar_index.rebuild_if_needed()`

## Tool Registry (`tool_registry.py`)

All 18 agent tools are registered as `ToolDef(name, description, risk_level, required_capability, executor)`. Adding a new tool = one `register()` call + executor function. `execute_tool()` is the single dispatch point used by `_execute_tool()` in `agent.py`.

## Exemplar System (`exemplar.py`)

On startup, `ExemplarIndex.build()` loads successful traces from `~/.friday_traces.jsonl`, embeds their `user_input` with `nomic-embed-text`, and stores `(embedding, trace)` pairs. At query time, `retrieve(query)` finds top-3 similar past tasks (cosine ≥ 0.78) and injects a formatted block into the agent system prompt as `{exemplar_context}`.

## Trace Recording (`trace.py`)

Every dispatch writes a `DispatchTrace` (JSONL) to `~/.friday_traces.jsonl`:
- Fields: tier, intent, confidence, policy decision, step sequence, success, duration
- Used by `ExemplarIndex` to learn from successful past executions
- Never raises — all writes are silently swallowed on error

## Embedding Classifier (`embed_classifier.py`)

- `INTENT_EXAMPLES`: ~130 Korean phrases across 14 labels
- Cache stored at `{base_dir}/.embed_cache.json` keyed by `md5(examples) + model`
- Ollama < 0.1.26 uses `/api/embeddings` (single-text, `"prompt"` key); auto-fallback handles this
- `CONFIDENCE_THRESHOLD = 0.75`: below this, falls through to Tier-2

## Safety Model

- `sandbox.py`: all file access validates paths stay within job workspace
- `permission.py`: intent → auto/confirm/deny policy; confidence gating for writes
- `git_mgr.py`: `RequiresConfirmation` raised for destructive patterns (force push, reset --hard, etc.); shell.py shows y/n prompt
- Shell commands in `/shell` use `_ALLOWED_COMMANDS` allowlist + `_BLOCKED_PATTERNS` blocklist

## Streaming Output (`rendering.py`, `ollama.py`)

- `chat_stream()` yields chunks from Ollama with `"stream": True`
- `render_streaming_answer()` detects `<think>/<thought>/<thinking>` tags in real-time:
  - Thinking text → grey dim panel (shown while streaming)
  - Answer text → buffered then shown in cyan panel after completion

## Data Layout

```
~/FridayLocal/
  workspace/<job-id>/   # sandboxed per-job files
  inbox/                # import staging area
  export/               # exported archives
  trash/                # soft-deleted files
  logs/                 # rotating log files
  calendar/             # ICS + JSON calendar data
  state.json            # current job + session state
  .friday_traces.jsonl  # execution trace log (exemplar source)
  .embed_cache.json     # embedding cache
  .friday_history       # REPL input history
```
