# Security Policy

## Supported version

Security fixes target the latest released minor version.

## Reporting a vulnerability

Please use the repository's private GitHub Security Advisory flow when available. Do not disclose an unpatched vulnerability in a public issue. Include the affected version, reproduction steps, impact, and suggested mitigation.

## Security boundaries

Build-up treats web pages and model output as untrusted. It restricts public-page targets, redirects, response size, content type, identifiers, citations, and workspace writes. These controls reduce risk but do not make arbitrary third-party content trustworthy.

API keys belong in environment variables and must never be committed. A remote Ollama URL sends prompts and supplied evidence to that endpoint. Browser search is opt-in and does not bypass CAPTCHA or consent challenges.

Knowledge Vault Markdown is untrusted derived output, not evidence. Supported
claims must resolve to a sealed research artifact inside the owning job and pass
source/passage hash checks. Vault writes are limited to the dedicated
`knowledge/` root; cross-vault binding is explicit; automatic verification,
destructive replacement, and automatic contradiction resolution are disabled.
Editing a sealed research artifact makes its manifest fail and affected Wiki
claims stale—it does not silently update their provenance.

PDFs fetched by the research reader are archived inside that run and sealed in
the same manifest. Knowledge citations retain the PDF path, byte count, and
SHA-256. PDF extraction caches are not reused as substitutes for original bytes.
