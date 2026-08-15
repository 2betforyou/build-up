# Changelog

All notable changes are documented here. Versions follow Semantic Versioning.

## 0.2.0 — Unreleased

### Added

- Adaptive contract/task/evidence/claim/gap research loop.
- Stable Source, Evidence, and Claim IDs with exact-passage validation.
- Tavily, Brave, Exa, OpenAlex, SearXNG, legacy Google CSE, and opt-in Chromium providers.
- Safe HTML/text/PDF reader with robots, redirect, network-target, size, and cache controls.
- Hard persisted budgets, atomic checkpoints, process lease, event log, and resume.
- Evidence-only report writer, deterministic citation audit, independent claim-support audit, and one bounded correction.
- Offline run evaluation and SHA-256 artifact manifest.
- Research depth and budget CLI options.
- Fail-closed checkpoint invariant validation and cache-integrity checks.
- Confirmation-gated direct-process runner with no shell expansion, bounded output/time, and restricted Git execution.
- Append-only per-job Knowledge Vaults with immutable proposals, original source/evidence/hash provenance, deterministic Markdown compilation, query, lint, review, and rollback.
- Automatic completed-research ingestion, contradiction preservation, source-hash staleness, explicit cross-vault binding, and Study-only user verification promotion.
- Per-run archival of directly fetched original PDF bytes, with source metadata and final-manifest SHA-256 verification propagated into Knowledge Vault citations.

- Opt-in `job bind` for working on real directories in place, read-only by
  default, with the data root, `$HOME`, symlinks, and path escapes refused.
- Bottom-anchored live status line reporting the current research step, budget
  counters, and elapsed time, so long model calls are distinguishable from a hang.
- `doctor` checks for stale pre-rename environment variables, a base directory
  whose jobs live somewhere else, and research/reviewer weights that exceed
  available memory; failing checks now name the command that fixes them.
- `BUILDUP_ENGLISH_BRIEF` and `/english` to toggle the English companion
  section, which is now off by default.

### Changed

- Product package, command, environment variables, paths, and public documentation now use Build-up naming.
- Deep research is a multi-phase adaptive workflow instead of a single prompt with simulated roles.
- Default data root is `~/.buildup`.
- Resume reuses the persisted depth, budget, and per-query result policy without drift.
- A zero search-cache TTL disables cache reads and writes for provider-policy compliance.

### Removed

- Legacy product launchers and package paths.
- DuckDuckGo package and keyless-search dependency.
- Legacy product environment-variable aliases.
