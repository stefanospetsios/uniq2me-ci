#!/usr/bin/env python3
"""Validate configuration boundaries; runtime records do not verify themselves."""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

MODELS = {
    'primary': 'qwen3:235b',
    'reasoning': 'deepseek-r1:70b-llama-distill-q8_0',
    'implementation': 'qwen3-coder-next:q8_0',
}
MCP = frozenset(('searxng', 'websearch', 'github', 'git', 'playwright', 'filesystem'))
ORDER = ['questions-and-contracts', 'canonical-solutions',
         'explanations-clarifications-and-languages']


def validate(data, allow_unconfigured=False, require_runtime=False,
             stage='core'):
    errors, unresolved = [], []

    def check(condition, message):
        if not condition:
            errors.append(message)

    def required(value, label, predicate=lambda value: isinstance(value, str) and bool(value)):
        if value is None:
            (unresolved if allow_unconfigured else errors).append(label + ' is unconfigured')
        elif not predicate(value):
            errors.append(label + ' has an invalid value')

    def integer(value):
        return type(value) is int and value > 0

    def digest(value):
        return isinstance(value, str) and bool(re.fullmatch(r'(?:sha256:)?[a-fA-F0-9]{64}', value))

    if not isinstance(data, dict):
        return {'status': 'invalid', 'errors': ['configuration must be an object'], 'unconfigured': []}
    check(data.get('recordType') == 'run-configuration', 'recordType must be run-configuration')
    repos = data.get('repositories', {})
    check(repos.get('input') == 'stefanospetsios/uniq2me', 'input repository must be stefanospetsios/uniq2me')
    check(repos.get('output') == 'stefanospetsios/uniq2me-ai', 'output repository must be stefanospetsios/uniq2me-ai')
    pub = data.get('publication', {})
    branch = pub.get('branch', '')
    check(isinstance(branch, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', branch))
          and not any(part in branch for part in ('..', '@{', '//'))
          and not branch.endswith(('/', '.', '.lock')), 'publication branch is not a safe Git ref')
    check(pub.get('allowForcePush') is False, 'force-push must remain disabled')
    check(pub.get('acceptedReleasesImmutable') is True, 'accepted releases must remain immutable')
    mcp = data.get('mcp', {})
    def safe_endpoint(url):
        if not isinstance(url, str):
            return False
        try:
            parsed = urlparse(url)
            return (bool(parsed.hostname) and not parsed.username and not parsed.password
                    and not parsed.query and not parsed.fragment and
                    (parsed.scheme == 'https' or parsed.scheme == 'http' and
                     parsed.hostname in ('127.0.0.1', 'localhost', '::1')))
        except ValueError:
            return False
    check(isinstance(mcp, dict) and set(mcp) == MCP and all(safe_endpoint(url) for url in mcp.values()),
          'MCP names/endpoints differ from the declared deployment')
    refinement = data.get('refinementPlan')
    if refinement:
        tasks = ('rework_questions', 'complete_fields', 'ai_prompts', 'canonical_solutions', 'translate_fr', 'translate_el')
        configured_tasks = refinement.get('tasks', {})
        check(set(configured_tasks) == set(tasks), 'Refinement must declare the six task switches')
        check(all(type(row.get('enabled')) is bool and row.get('model') in ('a','b','c')
                  for row in configured_tasks.values()), 'Invalid refinement task/model configuration')
        selected = [task for task in tasks if configured_tasks.get(task, {}).get('enabled')]
        check(bool(selected), 'Refinement needs at least one selected task')
        check(not {'rework_questions','complete_fields'} <= set(selected), 'Refinement question tasks are mutually exclusive')
        check(data.get('authoringOrder') == selected, 'Refinement authoring order differs from the selected tasks')
        models = refinement.get('models', {})
        retry_models = refinement.get('retryModels', [])
        check(isinstance(retry_models, list) and all(v in ('a','b','c') for v in retry_models)
              and len(retry_models) == len(set(retry_models)), 'Invalid refinement retry model order')
        check(set(models) == {'a','b','c'} and all(isinstance(tag,str) and
              bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*',tag)) and '..' not in tag
              for tag in models.values()), 'Invalid refinement model slots')
        scopes = refinement.get('sfs', [])
        check(isinstance(scopes,list) and bool(scopes) and all(isinstance(s,str) and len(s)==1 and
              s in '0123456789ABCDEF' for s in scopes) and len(scopes)==len(set(scopes)), 'Invalid refinement SFS selection')
    else:
        check(data.get('authoringOrder') == ORDER, 'question/solution/completion order must be preserved')
    required(data.get('methodologyRef'), 'methodologyRef')
    required(data.get('methodologyDigest'), 'methodologyDigest', digest)
    required(data.get('runId'), 'runId')
    for key in ('inputCommit', 'outputBaseCommit'):
        required(repos.get(key), key, lambda value: isinstance(value, str) and bool(re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', value)))
    inputs = data.get('inputs', {})
    references = inputs.get('referenceCommits', {})
    check(isinstance(references, dict), 'referenceCommits must be an object')
    if isinstance(references, dict) and references:
        required(references.get('manifestRef'), 'reference commit manifest ref')
        required(references.get('manifestDigest'), 'reference commit manifest digest', digest)
        requested = references.get('requestedCommits', {})
        check(isinstance(requested, dict), 'requested reference commits must be an object')
        requested = requested if isinstance(requested, dict) else {}
        check(set(requested) == {'stefanospetsios/uniq2me', 'stefanospetsios/uniq2me-ai'},
              'reference commits must use the two declared repositories')
        for repository, commits in requested.items():
            check(isinstance(commits, list) and all(isinstance(commit, str) and
                  re.fullmatch(r'[a-f0-9]{40}', commit) for commit in commits),
                  'reference commits must contain full commit SHAs: ' + repository)
        snapshots = references.get('snapshots', [])
        check(isinstance(snapshots, list) and bool(snapshots), 'reference commit snapshots are required')
        if isinstance(snapshots, list):
            for snapshot in snapshots:
                check(isinstance(snapshot, dict), 'reference commit snapshot must be an object')
                if isinstance(snapshot, dict):
                    repository = snapshot.get('repository')
                    commits = requested.get(repository, []) if isinstance(repository, str) else []
                    check(isinstance(commits, list) and snapshot.get('commit') in commits,
                          'reference snapshot does not match its requested repository/commit')
                    required(snapshot.get('snapshotDigest'), 'reference snapshot digest', digest)
            expected_revisions = {(repository, commit) for repository, commits in requested.items()
                                  if isinstance(commits, list) for commit in commits if isinstance(commit, str)}
            pinned_revisions = {(snapshot['repository'], snapshot['commit']) for snapshot in snapshots
                                if isinstance(snapshot, dict) and isinstance(snapshot.get('repository'), str)
                                and isinstance(snapshot.get('commit'), str)}
            check(pinned_revisions == expected_revisions, 'Every requested reference commit needs its exact snapshot')
    catalog = inputs.get('catalog', {})
    check(catalog.get('mode') in ('empty', 'existing'), 'catalog mode must be empty or existing')
    if catalog.get('mode') == 'existing':
        required(catalog.get('ref'), 'existing catalog ref')
        required(catalog.get('digest'), 'existing catalog digest', digest)
    else:
        check(catalog.get('ref') is None and catalog.get('digest') is None, 'empty catalog must not silently carry an existing base')
    masks = inputs.get('masks', {})
    check(masks.get('mode') in ('empty-library', 'existing'), 'mask mode must be empty-library or existing')
    bindings = masks.get('definingCatalogRefs', [])
    if masks.get('mode') == 'existing':
        required(masks.get('ref'), 'existing masks ref')
        check(isinstance(bindings, list) and len(bindings) > 0, 'existing masks require defining catalog bindings')
        if isinstance(bindings, list):
            for binding in bindings:
                if not isinstance(binding, dict):
                    errors.append('mask binding must be an object')
                else:
                    required(binding.get('ref'), 'mask defining catalog ref')
                    required(binding.get('digest'), 'mask defining catalog digest', digest)
    else:
        check(masks.get('ref') is None and bindings == [], 'empty mask library must not hide incoming bindings')
    sources = inputs.get('sources', {})
    rag = data.get('rag')
    if rag is not None:
        check(isinstance(rag, dict), 'RAG configuration must be an object')
    if isinstance(rag, dict):
        mode = rag.get('mode')
        check(mode in ('disabled', 'repo-only', 'repo-web'), 'Invalid RAG mode')
        check(rag.get('enabled') == (mode != 'disabled'), 'RAG enabled flag disagrees with mode')
        check(rag.get('webResearch') == (mode == 'repo-web'), 'RAG webResearch disagrees with mode')
        check(rag.get('repositoryRetrieval') == (mode != 'disabled'), 'RAG repositoryRetrieval disagrees with mode')
        check(data.get('sourceDiscovery', {}).get('enabled') == (mode == 'repo-web'),
              'Web source discovery must match the selected RAG mode')
    required(sources.get('corpusRef'), 'source corpusRef')
    required(sources.get('snapshotDigest'), 'source snapshotDigest', digest)
    cutoff = sources.get('cutoffUtc')
    required(cutoff, 'source cutoffUtc')
    if cutoff is not None:
        try:
            check(isinstance(cutoff, str) and cutoff.endswith('Z'), 'cutoff must be an explicit UTC instant')
            datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
        except (AttributeError, ValueError, TypeError):
            errors.append('cutoff is not a valid timestamp')
    check(sources.get('generatedAndAuthoritativeCollectionsSeparate') is True,
          'generated and authoritative collections must be distinct')
    settings = data.get('catalog', {})
    check(settings.get('sfsCount') == 16 and settings.get('sfPerSfs') == 16,
          'cybersecurity catalog must retain the 16x16 contract')
    check(settings.get('questionCharacterLimit') == 512, 'question character limit must be 512')
    check(settings.get('languages') == ['en', 'el', 'fr'], 'language contract must be en/el/fr')
    for key in ('areasRef', 'concernInventoryRef', 'languageFixturesRef', 'finalSfSchemaRef', 'solutionSchemaRef', 'maskSchemaRef'):
        required(settings.get(key), key)
    budgets = data.get('budgets', {})
    time_limited = budgets.get('mode') == 'time-limited-token-metered'
    budget_keys = ('maximumAttempts',) if time_limited else (
        'X', 'Y', 'R', 'G', 'reservedCompletionX', 'maximumAttempts')
    for key in budget_keys:
        required(budgets.get(key), key, integer)
    if time_limited:
        for key in ('X', 'Y', 'Z', 'REVIEW_MINUTES', 'SOURCE_MINUTES', 'MASK_MINUTES'):
            required(data.get('timeoutsMinutes', {}).get(key), 'timeout ' + key,
                     lambda value: type(value) in (int, float) and 0 <= value <= 7200)
        check(all(budgets.get(key) is None for key in ('X', 'Y', 'R', 'G', 'reservedCompletionX')),
              'time-limited runs must distinguish minute limits from unconfigured token budgets')
    if integer(budgets.get('X')) and integer(budgets.get('reservedCompletionX')):
        check(budgets['reservedCompletionX'] < budgets['X'], 'completion reservation must leave allowance for the question pass')
    check(budgets.get('chargeFailedCalls') is True and budgets.get('countReasoningOnce') is True,
          'failed-call charging and single reasoning accounting are required')
    ollama = data.get('ollama', {})
    check(ollama.get('serverEnvironment', {}).get('OLLAMA_CONTEXT_LENGTH') == '262144',
          'request OLLAMA_CONTEXT_LENGTH=262144 on the serving process')
    check(ollama.get('cloudFallback') is False, 'cloud model fallback is disabled')
    model_records = data.get('models', {})
    for role, tag in MODELS.items():
        model = model_records.get(role, {})
        if role == 'primary' and data.get('refinementPlan'):
            tag = data['refinementPlan']['models']['a']
        check(model.get('tag') == tag, role + ' model tag differs from the requested deployment')
        check(model.get('requestedContext') == 262144, role + ' must request 262144 context')
        if role == 'primary':
            check(model.get('enabled') is True, 'primary model must be enabled')
        if model.get('enabled'):
            required(model.get('digest'), role + ' model digest', digest)
            if require_runtime:
                check(model.get('preflightStatus') == 'passed', role + ' runtime preflight has not passed')
                effective, native = model.get('observedContext'), model.get('nativeContext')
                check(integer(effective) and integer(native), role + ' requires observed and native context')
                if integer(effective) and integer(native):
                    check(effective <= native and effective <= 262144, role + ' effective context exceeds declared support or request')
                    if effective != 262144:
                        check(model.get('explicitContextFallback') is True and bool(model.get('fallbackReason')),
                              role + ' context reduction requires explicit fallback and reason')
                check(model.get('nativeToolCallingSupported') is True or model.get('textOnlyReview') is True,
                      role + ' requires tool support or a declared text-only review path')
    review = data.get('review', {})
    check(review.get('coreAcceptanceRequired') is True, 'core acceptance cannot be omitted')
    if review.get('crossLlmEnabled'):
        check(any(model_records.get(role, {}).get('enabled') for role in ('reasoning', 'implementation')),
              'cross-LLM review needs a selected optional model')
        required(review.get('reviewScopeRef'), 'optional review scope')
    mask_task = data.get('masks', {})
    if mask_task.get('requested'):
        required(mask_task.get('scopeRef'), 'requested mask scope')
        check(isinstance(mask_task.get('jurisdictions'), list) and bool(mask_task['jurisdictions']),
              'legal masks require declared jurisdictions independent of language')
    distill = data.get('distillation', {})
    check(distill.get('authoringTeacherWeightsReceiveStudentFeedback') is False,
          'student weights must not feed the authoring recurrence')
    check(distill.get('heldOutEvaluationRequired') is True, 'evaluated students require held-out references')
    if stage == 'distillation' and distill.get('enabled'):
        for key in ('trainerRef', 'baseStudentRef', 'datasetRef', 'trainingBudgetRef'):
            required(distill.get(key), 'distillation ' + key)
    next_run = data.get('nextRun', {})
    check(next_run.get('feedbackDependsOnStudentSuccess') is False,
          'accepted catalog feedback must be independent of student success')
    status = 'invalid' if errors else ('template-unconfigured' if unresolved else
             ('configuration-and-runtime-records-valid' if require_runtime else 'configuration-valid-runtime-unverified'))
    return {'status': status, 'errors': errors, 'unconfigured': unresolved,
            'limitations': ['Checks configuration and supplied record structure; does not contact MCP/Ollama, verify source truth, validate final catalog records, or establish a scientific result.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--allow-unconfigured', action='store_true')
    parser.add_argument('--require-runtime-preflight', action='store_true')
    parser.add_argument('--stage', choices=('core', 'distillation'), default='core')
    args = parser.parse_args()
    try:
        data = json.loads(args.path.read_text())
        result = validate(data, args.allow_unconfigured, args.require_runtime_preflight, args.stage)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        result = {'status': 'invalid', 'errors': [str(error)], 'unconfigured': []}
    print(json.dumps(result, indent=2))
    return 1 if result['status'] == 'invalid' else 0


if __name__ == '__main__':
    raise SystemExit(main())
