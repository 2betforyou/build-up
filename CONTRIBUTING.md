# Contributing

Thanks for improving Build-up.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Before opening a pull request:

```bash
PYTHONPATH=router python -m compileall -q router/buildup tests
PYTHONPATH=router python -W error::ResourceWarning -m unittest discover -s tests -v
ruff check router tests
python -m build
```

Research and Knowledge Vault tests must be deterministic and offline. Inject search, reader, and chat callbacks. Include a regression test for each bug and verify persisted state or artifacts rather than matching full prompt text.

Keep changes scoped. Do not weaken source-ID allowlists, exact-passage validation, URL safety, hard budgets, sandbox gates, completion quality gates, append-only knowledge events, or original-source provenance merely to accept a model response.

## Pull requests

- Explain the observable behavior and compatibility impact.
- List the commands used for verification.
- Call out schema or artifact changes.
- Never include API keys, local workspace data, model output containing private data, cache files, or session databases.
