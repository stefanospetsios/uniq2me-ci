# Catalog-derived student models and scientific evidence

The current user-requested deployment leaves stage 6 as **TODO**. Set distillation.enabled=false, preserve a concrete distillation handoff and never claim a trained/evaluated student. The procedures below apply only to a later explicitly configured training run with a real backend, student base, data splits and evaluation references.

Build checked teaching cases from the final accepted catalog plus varied system evidence. Include satisfied, failed, pending, stale, contradictory, out-of-scope and legitimately alternative implementations. Keep synthetic, laboratory and real observations labeled. Source/solution guidance alone is not proof that a service passed.

Record teacher/base-student checkpoint digests, accepted catalog and source snapshots, examples/targets, acceptance/rejection decisions, exclusions, languages, scenario-family lineage, adaptation/retrieval settings, resources and model card. Group translations, snapshots and variants of one seed/system into one train/development/test partition. Maintain an independent held-out reference set; do not use teacher agreement unchanged as test truth.

Train the student through a declared optimizer/training stack. Ollama serves inference and does not alone document optimization. A pipeline instruction is not an executed training job. A checkpoint manifest requires an existing verified artifact. Observable responses can support response-based distillation; unavailable logits or internal traces cannot be claimed.

The fixed authoring model receives accepted artifacts in successive runs. It does not become the previous distilled student. If a user selects another model/base/training regime, record it as an explicit configuration change and separate its effect from artifact refinement.

Optional R1 reasoning review, Coder-Next implementation review, cross-generator catalog comparisons and previous/new student comparisons are independently enabled. Pin common artifacts/sources and reviewer digests, hide labels where feasible, reverse pairwise order, and preserve disagreements. Same-model roles are not independent experts; cross-model consensus is not correctness.

Old/new student comparison has a common-catalog/evidence arm for model differences and a mapped native-catalog arm for combined effects. Missing predecessors are unavailable, not fabricated. Core held-out evaluation remains required for an evaluated student output even when agent comparison is omitted.

Report unsupported-pass fraction and false-pass rate with distinct denominators, missing-evidence behavior, citation support, valid alternatives, multilingual fidelity, latency and memory. MASK analyses identify selected scope and pending status; preserve original targets and resource/preservation constraints in migration tests. Only fresh observed postconditions establish migration success.

Quantify cold versus forward-fed runs under identical current sources, model/rules and resource conditions; vary expert corrections and empty/existing masks independently. Record chronological source changes, retained corrections, new/reintroduced defects, source update latency and acceptance costs. Sampling units are runs/chains/scenario families rather than 256 correlated SFs. Include null/adverse effects and incomplete configurations. Paper conclusions and red evidence fields are filled only from actual measured records and analysis, never plausible numbers or assumed adoption.

Maintain an exposure ledger across releases. Any evaluation case admitted as expert/model feedback, a regression example, retrieval context or training data is no longer held out in later runs. Use fresh prospective cases for new independent claims and retain the old case as explicitly exposed development evidence. Do not count a renamed or translated exposed case as a new test.
