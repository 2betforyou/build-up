---
name: paper-pdf-translation
description: Translate machine-learning and LLM research PDFs into faithful Korean Markdown while preserving domain terminology, citations, equations, and variable names, then provide a clearly separated explanation of the key meaning. Use for 논문 PDF 번역, paper translation, abstract or section translation, and requests such as 이 논문 번역해줘.
---

# Paper PDF Translation

Translate the complete requested text faithfully and put interpretation only in a separate `핵심 의미` section.

## Run

Use a current-job PDF path or a paper-library ID:

```bash
buildup translate paper.pdf
```

In the interactive shell, use `/translate paper.pdf`. In build-up with an active paper, say `이 논문 번역해줘` or run `/translate` without an argument.

## Workflow

Apply these rules exactly:

1. Machine learning 및 LLM domain terminology는 영어로 유지한다.
2. 원문의 의미를 추가하거나 생략하지 않는다.
3. citation, equation, variable name은 그대로 유지한다.
4. 직역을 우선하되 한국어 문장 구조가 심각하게 어색할 때만 최소한으로 재구성한다.
5. 번역 후 별도로 핵심 의미를 설명한다.
6. 번역과 설명을 명확히 구분한다.

Then:

1. Extract the PDF page by page and retain page markers.
2. Use one model call when the text fits safely. Split a long paper into page-aligned chunks and keep each call history independent.
3. Protect citations, equations, and variable-like identifiers with immutable placeholders before each call.
4. Require JSON fields `translation` and `explanation`. Keep every `[[[BUILDUP_KEEP_XXXX]]]` placeholder unchanged in `translation`.
5. Reject a chunk if any required placeholder is missing, added, or duplicated. Do not silently omit it.
6. Restore protected literals exactly and write `번역` and `핵심 의미` under separate headings.

## Output

Write `translation-ko.md` in an active paper directory or `<source>.ko.md` beside a PDF in the current job. Do not overwrite or modify the source PDF. If the PDF contains only scanned images, report that OCR is required instead of inventing text.
