# Pass 1: formal nontechnical questions

Generate or revise exactly 16 SFs per inherited SFS, 256 total, at unique hexadecimal positions 0--F. Read the pinned area names/order and concern inventory from the admitted methodology; do not rename topics, move conditions, invent a seventeenth slot, or derive the coverage inventory only from the generated catalog.

Each question asks one assessable condition in at most 512 characters. Use formal, direct, nontechnical terminology understandable by a lawyer, judge or ordinary reader. Avoid unexplained acronyms, product-specific jargon, double negatives, vague 'appropriate/adequate' without supporting criteria, and several independently assessable conditions combined to fill a slot. Do not simplify away exceptions, responsibility, time limits, or source meaning.

Return a `question-candidate` stage artifact with identity, parent/base reference, wording, source/passage links, applicable actor/entity/service scope, evidence period, provisional positive/negative boundaries, missing-evidence handling, valid alternatives, mapped concern IDs, unresolved issues and stage provenance. This is not a full-schema final SF. Explanations, clarifications, translations and canonical solutions are completed in later passes.

Set top-level `recordType` to the exact string `"question-candidate"` in every
response and repair. Use the exact identity supplied by CI, including language.
`contract.requiredEvidence` and `contract.alternatives` are nonempty arrays of
substantive strings; describe valid equivalents or explicitly state when no
alternative satisfies the criterion. Do not invent an exception to fill an array.

Include `parentQuestionDigest`, `semanticRelation` and `changeReason` in every
complete candidate, including repairs. Lineage compares with the prior accepted
position supplied by CI, never a rejected attempt. Without an accepted parent,
return `parentQuestionDigest: null` and `semanticRelation: "initial"` on every
attempt. With an accepted parent, retain its exact digest and use `"preserved"`
when the criterion stays the same (including clearer wording), or `"changed"`
when authoritative evidence requires a criterion correction. Explain current
source checks and any criterion change in `changeReason`.

Example wording: 'Has the required recovery test succeeded during the review period?' The supporting contract identifies the service, time period, functional/integrity checks, and test evidence. A schedule alone does not establish a completed test; missing evidence is pending rather than an automatically accepted negative result.

Question acceptance checks single meaning, source support, nontechnical clarity, scope/evidence consistency, duplicates, contradictions and independent concern coverage. Retain accepted earlier meanings only where current source and scope checks support them. Existing curated text is candidate knowledge, not current authoritative legal/technical truth.

A repair records failed rule, supporting passage/example, parent/child digest and remaining allowance. Question changes invalidate solutions, explanations, fixtures, translations and mask mappings that depend on the changed meaning. In time-limited CI, the question deadline is X minutes and completion has its separate Z-minute deadline; a retry does not reset either. Reserve the declared portion of paper X only in an explicitly token-budgeted run.

For the user's catalog timeout policy, all attempts and review share one additional
catalogGraceSeconds finalization window (default 30 seconds). Finalize the current
question using already available evidence rather than starting more discovery.
CI preserves an unfinished candidate as pending, continues draft stage publication,
and retains hashed failure feedback for a later run. Pending placeholders are not
accepted questions; dependent completion/translation and legal-mask acceptance
must not pretend those questions passed their checks.

X=0 disables the question deadline; REVIEW_MINUTES=0 disables the separate review
cap, while any finite parent deadline still applies. MAX_ATTEMPTS remains bounded.
If a structurally checked question exhausts semantic review attempts before its
deadline, preserve it as pending-review-rejected and continue draft publication.
Retain the review's failed rule IDs, rationales and unresolved findings and use
them in repair prompts. Incorrect artifact bindings and invalid question contracts
retain their failure gates. Review rejection never creates an accepted cache entry.
