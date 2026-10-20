# Record contracts and completion checklist

Every call envelope records run/call ID, stage, artifact IDs, attempt, parent digest, requested/resolved model, substituted prompt digest, source/passage refs, context requested/effective, decoding, raw response, parsed payload if valid, stop/error status, timestamps, usage interpretation/allocation and remaining budget. Preserve invalid text before parsing and unavailable usage with reason.

Source record: source ID/type, origin/publisher, exact URL/version/language, jurisdiction and applicability, original dates/precision, retrieval UTC, admitted cutoff, byte hash, snapshot path, passage refs, status, supports-claim list, and redistribution limits. Stage candidate: explicit candidate type, identity, parent/base refs, sources, minimal stage contract, decisions and unresolved issues. Full final records use the pinned repository schema; do not change it merely to pass validation.

Review record: artifact digest, reviewer/model/human scope, check/rule IDs, sources/fixtures, outcome, rationale, conflict, timestamps, execution-validation state and human-supervision record refs. Expert correction: reviewer competence and attribution, original/changed criterion, accepted decision, dependencies, later-regression checks. Model record: checkpoint artifact, teacher/base/dataset/catalog/split bindings, actual optimization/evaluation records and model card.

Release index groups exact SF/solution/language/mask paths with digests, semantic mappings, base/source/rules/model provenance, budget ledger, expert records, accepted/candidate distinctions, required deliverables and omitted/failed variants. Next-base record points to the immutable accepted release and validated corrections; source updates are separately identified.

Before claiming completion verify:
- 256 unique positions and 16 per fixed area; complete readable binary meanings and evidence rules.
- Question -> solution -> explanation/language stage order and dependency invalidations recorded.
- Current authoritative source support and cutoff; no fabricated citations or execution outcomes.
- Checked canonical routes/alternatives; actual execution labeled separately.
- English/Greek/French full fields and equivalence fixtures; individual language digests.
- Requested mask types/bindings/applicability and gaps; proper review status.
- Expert scope, actual interventions/corrections and persistent accepted decisions.
- All core gates on exact bytes; requested optional gates and missing inputs explicit.
- Configured time deadlines/retry limits reconcile and all attempts remain in the archive. Numeric token budgets reconcile only in an explicitly token-budgeted run; unconfigured token ceilings are reported as such.
- Correct output repository/branch and parent commit; no template-repository writes.
- Verified published commit/checksums and next-base pointers, or an explicit publication failure.
- Verified trained/evaluated checkpoint only when actual training/evaluation records exist.

Final run report gives concrete artifact/commit links, acceptance and execution scope, selected MASK measures, actual costs, optional variant status, unresolved limitations, and next-run input pointers. Do not stop at a plan when the configured work is authorized and feasible. When blocked, identify the actual missing parameter/tool/evidence and preserve completed independent work.

Review citation requirements are explicit in CI's `checksRequiringSourceRefs`.
Checks of the bound candidate, pinned task inputs and fixtures (wording, schema,
contract/field consistency, translation equivalence, bit order and honest draft
status) may declare `sourceRefs: []`; the artifact digest, rationale, check result
and fixture outcomes remain mandatory. Source support, scope and
concern checks still need actual frozen references when retrieval is enabled.
Every supplied citation, including an optional one, must resolve to a frozen
source and passage. This does not turn repository prose into verified law.
