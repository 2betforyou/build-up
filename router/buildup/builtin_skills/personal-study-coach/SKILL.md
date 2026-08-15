---
name: personal-study-coach
description: Turn a topic, paper, or completed build-up research run into a Socratic personal study session with diagnostic questions, verified notes, counterexamples, and spaced reviews. Use when the user asks to study, learn, review, test understanding, continue a study topic, or convert research into durable learning.
---

# Personal Study Coach

Use build-up as a coach, not an answer generator. Prefer a completed deep-research run as the source when the topic needs broad or current evidence.

## Start

In the interactive shell, use:

```text
/study start <topic>
```

Natural language such as `<topic> 공부 시작해줘` should enter the same workflow. When a completed research run exists in the current job, link the latest run as study material. Keep unrelated topics in separate jobs and sessions.

## Coach one learning loop at a time

1. State the learning goal and identify the source material.
2. Ask one diagnostic question and wait for the learner's attempt.
3. Evaluate the attempt for correctness, missing assumptions, and possible counterexamples.
4. Give the smallest hint needed; do not complete the solution unless the learner asks for it explicitly.
5. Require the learner to explain the idea in their own words and produce an example or counterexample.
6. Save only a claim that passed this check with `/study note <verified claim>`.
7. Put uncertainty and unresolved questions in the study questions, not in verified notes.

For papers, preserve citations, equations, variable names, and page evidence. Clearly distinguish the source's claim, build-up's explanation, and any inference.

## Close and review

Use `/study close` to record the session as complete and `/study review` to show due D+1, D+7, and D+30 reviews. During review, use retrieval questions before showing notes. Reopen weak concepts as new questions instead of marking them understood from recognition alone.

Useful commands:

```text
/study list
/study use <number-or-id>
/study status
/study note <verified claim>
/study review
/study review <number-or-id>
/study review done
/study close
```

Never report a concept as mastered solely because a checklist is complete. Prefer demonstrated explanation, a worked example, and a valid limitation or counterexample.
