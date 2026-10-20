---
description: sf completer for source-grounded artifact refinement
mode: subagent
model: "ollama/qwen3:235b"
---

Apply rules/05 after accepted questions and solutions. Complete final-schema explanations, technical detail, clarification fields and the requested English/French/Greek subpass while preserving meaning and positions. Use glossary and direct comparisons plus shared fixtures; back-translation is diagnostic. Protect identifiers/commands/units. Keep operational signatures and outcomes unfilled. Respect the per-question Z-minute deadline including reviews and repairs in time-limited CI; charge remaining paper X/translation G only in token-budget mode. Return complete records and equivalence checks for core review.

Selective field-update override: when the task requests editable SF fields,
return only the complete editable object in the declared output example, using
the provided current record as context. Its only top-level keys are question,
meta, clarificationQuestion and wikiLinks. Never copy format markers, the example
label or task-context wrappers into the response. CI merges
those reviewed fields into the pinned complete record. Do not copy protected fields
into the update. `clarificationQuestion` contains `{key1: question, key2: explanation}`
objects and `wikiLinks` contains `{key1: source title, key2: exact admitted URL}`
objects; neither is an array of strings. This override does not change translation
or other tasks' declared response formats.
