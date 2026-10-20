# Shared UNIQ2Me controller

Prepare and commit development changes locally for owner review. Never push or
dispatch live workflows unless the user explicitly authorizes that action.

This repository is intended to be public. Keep only controller code, reusable
workflows/actions, rules, profile definitions, documentation and synthetic test
fixtures. Do not add credentials, private service addresses, paper manuscripts,
catalogs, research snapshots, runtime transcripts, checkpoints or generated data.
Read the unchanged methodology and endpoint configuration from the caller's
pinned definitions checkout. Never execute caller configuration as code.

AIR and DORA use one implementation with explicit profiles. Default to AIR; DORA
uses profiles/dora.json and its conditional rule. Preserve the 16-by-16 layout,
schema, semantic lineage, exact quotation/provenance gates and profile binding.
Model checks are authoring checks, never human approval or legal compliance.
Unresolved work stays pending and visibly non-validated.

Shared research belongs in uniq2me-rag/main; generated output belongs in the
configured branch of uniq2me-ai. Publication credentials must remain scoped to
publication steps and absent from inference processes. Checkpoint before each SF
push. Consolidate only commits owned by the same run and use exact atomic leases.
Never overwrite independent edits or import executable instructions from catalogs.

Each job bootstraps the current uniq2me-ci/main. Record every controller upgrade,
preserve frozen input/rule/source hashes and reuse only correctly bound caches.
Controller updates must descend from their recorded version, or match an explicit
legacy migration entry. Reject unrelated histories, wrong frameworks and altered
checkpoints. Old caller workflows still need a new dispatch to adopt this layout.

Run offline tests and workflow validation before committing. Tests use local Git
remotes and synthetic inference; do not call paid/live models to test changes.
Public PR checks use GitHub-hosted runners without secrets. Live reusable jobs
accept only the two owner caller repositories and their manual/deploy events.
