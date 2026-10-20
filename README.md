# Shared UNIQ2Me CI

One controller for AIR and DORA. Start a run from **Actions on uniq2me-air or
uniq2me-dora**, using the existing 25-field form. Each caller has one catalog job
that invokes `.github/workflows/catalog.yml@main` here. Catalog data remains in
uniq2me-ai; shared research remains in uniq2me-rag/main.

## Loading fixes on retry

The shared workflow keeps separate preflight, preparation, primary authoring,
three optional retry-pass jobs and final consolidation. Each job explicitly checks
out **stefanospetsios/uniq2me-ci, ref main**, then executes the local
`.github/actions/stage/action.yml` from that checkout. All phase steps, SFS/SF
loops, validation, publication and checkpoint code are loaded from the current
controller even when a failed job is rerun. Actual SHAs and upgrades are recorded.

GitHub pins reusable-workflow YAML to its original SHA for **Re-run failed jobs**,
even when the caller says `@main`. Merely moving the old reusable workflow would
keep the original problem. The runtime composite avoids that pin for controller
code and phase steps. Changes to the bootstrap itself or overall job graph still
need a new dispatch with the checkpoint selected, or **Re-run all jobs** where
GitHub resolves the reusable reference again. See
[GitHub's rerun rules](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations#behavior-of-reusable-workflows-when-re-running-jobs).

Publish shared-controller changes first, then the thin callers. Runs created
before this bootstrap cannot acquire it by rerunning their old jobs. Start one
new run on the updated caller, enter the previous run ID in **checkpoint**, and
retain its original settings. Later failed-job retries load CI main directly.

## Checkpoints and migration

The checkpoint field accepts blank for an ordinary run/retry, a numeric source
run ID in the same caller repository, or `fresh` to regenerate selected work.
Artifacts remain attached to the caller run; they do not move into this public
repository. CI verifies their file hashes, frozen sources, methodology, rules,
profile, task/model selections, reference commits and expected destination.
Checked task caches retain their original bindings.

Current-main updates must descend from the recorded CI commit. Migration from
unrelated legacy AIR/DORA Git histories requires an explicit checkpoint selection
and a source commit listed in `controller-compatibility.json`. Unknown commits,
wrong profiles, altered checkpoints and independent destination changes are
rejected. The registry contains identifiers, not private Git history. Extend it
only after reviewing the corresponding source controller.

A manually published catalog recovery is output, not an execution checkpoint.
If its push moved the original destination, exact resume correctly refuses to
overwrite it. That published commit can instead supply the base for a new run
through the AI commit input.

## Profiles and publication

AIR keeps the original sixteen areas. DORA explicitly selects profiles/dora.json
and its conditional override: sixteen authored DORA areas, each containing sixteen
SFs. Bases, caches and outputs retain their framework/taxonomy bindings. DORA's
allocation is an authored taxonomy, not an official regulatory classification.

Both callers retain commit lists from uniq2me and uniq2me-ai, model slots A/B/C,
SFS 0–F/all, dry/live, retrieval mode and six checkbox/model pairs. The last
eligible AI commit supplies the base, otherwise the last eligible curated commit;
other commits remain reference data. Tasks are a1 questions and fields, a2 fields
with preserved questions, c AI prompts, d canonical solutions, e French, f Greek.
a1/a2 are mutually exclusive. The primary catalog pass finishes before optional
whole-catalog retry passes, avoiding repeated model loading.

Bounded candidate failures and explicitly retryable provider failures defer work
to later model passes. Authentication, identity, integrity and unclassified
infrastructure errors remain fatal. Unresolved work is visibly non-validated.
Task/model checks imply no human approval, operational assessment or compliance.

A model that advertises tools but answers the native probe in prose can still
perform tool-optional authoring and review. CI records the failed probe, disables
tools, supplies admitted passages inline and checks the actual OpenCode JSON
route before authoring. Model identity, context, provenance and review gates still
apply. Preparation that requires live web searches continues to require verified
native tool calls; transport and authentication failures do not trigger this
fallback. Probe errors name the model that was actually tested.

All 16 SFSs and 256 positions exist in the final JSON/readable-page layout at each
publication. CI checkpoints before each SF push and consolidates only commits
owned by this run, using exact atomic leases. Independent output edits remain
protected. The destination owner's deployment infrastructure is preserved.

## Public/private boundary

The public tree contains code, reusable workflows/actions, rules, the DORA
profile, documentation and synthetic fixtures. It contains no private manuscript,
operational MCP addresses, credentials, live research, catalog, checkpoint or
model transcript. Public endpoint examples use loopback addresses.

Each caller retains its unchanged methodology/main.tex and private opencode.json.
The bootstrap reads them from that caller's original github.sha into
catalog-definition/ without executing caller code or actions. The methodology is
copied into the frozen inputs. Only the six MCP URL strings are taken from caller
configuration; its agents, commands, instructions and provider settings are
ignored. Provider configuration still comes from caller variables. Missing live
methodology fails explicitly; the public methodology fixture is for dry tests.
Existing checkpoints retain their original frozen methodology.

Keep existing secrets in **each caller**, not here:

| Secret | Use |
| --- | --- |
| UNIQ2ME_TOKEN_WRITE | Selected output publication; absent from inference |
| uniq2me_token | Optional curated/reference read access |
| SOURCE_CACHE_TOKEN | Shared RAG read/write; output token remains the fallback |
| MCP_HEADERS_JSON | Optional per-MCP authentication headers |

Secrets are passed by explicit name rather than secrets: inherit. Existing caller
variables remain applicable, including OLLAMA_BASE_URL, INPUT_REF, context and
deadline settings, and cache controls. UNIQ2ME_RUNNER optionally selects a JSON
label array such as `["self-hosted", "your-private-label"]`; otherwise the caller's
self-hosted pool is used. Make the caller's runner and the public reusable workflow
available in Actions settings.

Live reusable jobs admit only the owner AIR/DORA callers and their manual/deploy
events. Public PR validation uses GitHub-hosted runners without private secrets
or live inference. Source-download retries have one implementation here: AIR
provides the daily/manual thin caller; DORA provides a manual caller. Download
success never admits a quotation or supplies missing coverage.

## Offline checks

```sh
python -m pip install -r requirements-ci.txt
python ci/audit_public.py --history
CATALOG_PROFILE='' python -m unittest discover -s agent-rules/tests
```

Tests use synthetic responses and temporary local Git remotes. The public audit
checks current files and reachable history for credential patterns, private
infrastructure addresses and runtime artifacts. Pattern scanning cannot prove
that every sensitive value is detectable. Existing Git history retains the old
prototype workflow as history; it is no longer active. Review the prepared commits
and visibility before publishing. Development changes stay local for the owner
to push.
