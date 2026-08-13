# Method and provenance

The workflow is an original local adaptation inspired by revfactory's Apache-2.0 research-assistant harness:

- Harness overview: https://github.com/revfactory/harness/blob/main/README_KO.md
- Research assistant package: https://github.com/revfactory/harness-100/tree/main/ko/63-research-assistant

The upstream package uses Claude Code Agent Teams. This local adaptation does not use `TeamCreate`, `TaskCreate`, `SendMessage`, or persistent Claude teammates. It replaces them with one structured Ollama inference and deterministic post-processing.

## Boundary contract

The six top-level keys are both execution order and trust boundaries. Validate their exact order and types before writing normal artifacts. Store an invalid raw response only for diagnosis, never as a successful role artifact.

The coordinator appears at the beginning as `01_coordinator_scope` and returns at the end through `06_final_report`. This yields five conceptual roles and six ordered stages without extra model calls.
