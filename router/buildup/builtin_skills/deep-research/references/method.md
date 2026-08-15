# Method and provenance

Build-up uses an original local orchestration layer informed by public research-agent patterns: explicit planning, isolated subtasks, shared evidence state, gap-driven follow-ups, and a writer that cannot search. It does not claim that prompt roles are independent agents.

## State topology

```text
ResearchContract
  -> ResearchTask[]
  -> SourceRegistry
  -> EvidenceLedger (exact passages only)
  -> Claim[] + ResearchGap[]
  -> follow-up ResearchTask[] or stop
  -> evidence-only Writer
  -> CitationAudit + independent ClaimAudit
```

Network searches may fan out concurrently. Local model calls default to serial execution to avoid model-memory contention and nondeterministic state writes. Each task owns its query/source/evidence references while the coordinator merges only validated records.

## Evidence and citation contract

- Source IDs are stable hashes of canonical URLs.
- Evidence IDs are stable hashes of source ID plus normalized exact passage.
- Claim IDs are stable hashes of atomic claim text.
- A passage is rejected if it is fuzzy, paraphrased, too short, too long, or refers to an unknown source.
- Reports cite Evidence IDs adjacent to factual text.
- The Sources section is generated from the registry and includes only sources reached through cited evidence.
- Unknown citations, unsupported critical claims, non-registry URLs, or a failed independent audit prevent completion.

## Durability

`state.json` is the resumable source of truth. Materialized JSON/JSONL/Markdown files make runs inspectable without Python. Directly fetched PDF bytes are preserved under `raw-sources/`. Atomic replacement prevents partial checkpoints, `.run.lock` prevents concurrent writers, `events.jsonl` records transitions, and `manifest.json` seals final artifacts plus original PDFs.

The implementation intentionally avoids a mandatory orchestration framework. Its domain models and callback boundaries allow a future graph runtime without changing the on-disk contract.

## Compounding Wiki boundary

The completed run remains immutable source of truth. A separate Knowledge Vault
uses immutable proposals plus append-only source/claim/status ledgers, following
the raw-source-versus-generated-Wiki separation in
[Karpathy's LLM Wiki note](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
Markdown pages and `index.md` are compiled views. They cannot ground another
claim. Source hash changes mark affected claims stale; contradicting evidence is
retained without choosing a winner; only explicit user review can mark a claim
verified or rejected.
