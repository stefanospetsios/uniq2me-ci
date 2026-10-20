"""Fast synthetic fixtures exercise every gate and commit without inference."""
import copy
import json

from ci.progress import log
from ci.contracts import HEX, byte_digest, catalog_digest, digest, file_manifest, read, write
from ci.runtime import OpenCode, utc
from ci.rag import mode_of, policy as rag_policy

SOURCE_ID = 'dry-run-synthetic-source'
SOURCE_URL = 'https://example.invalid/uniq2me-dry-run-not-a-real-source'
REFS = [{'sourceId': SOURCE_ID, 'passageId': 'synthetic-passage'}]


def synthetic_review(value, checks, fixtures=None):
    return {'artifactDigest': digest(value), 'decision': 'accepted',
        'checks': [{'ruleId': check, 'passed': True,
                   'rationale': 'SYNTHETIC DRY RUN: deterministic contract fixture; no expert/model judgment.',
                   'sourceRefs': REFS} for check in checks], 'unresolved': [],
        'fixtureOutcomes': [{'id': f['id'], 'outcome': f['expected']} for f in fixtures or []]}


class FixtureServer:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def ask(self, role, prompt, seconds, call_id, **kwargs):
        # The ordinary task generator uses candidate() below. Only the shared
        # roster gate calls this interface; no OpenCode/Ollama process is started.
        payload = json.loads(prompt[prompt.index('{"roster":'):])
        value = synthetic_review(payload['roster'], payload['requiredChecks'])
        write(self.pipeline.root / 'synthetic-calls' / (call_id + '.json'), {
            'synthetic': True, 'realModelInvoked': False, 'role': role,
            'promptDigest': digest(prompt), 'response': value})
        return value


def maybe_fail(pipeline, stage, key):
    publication_stage = 'extended' if stage == 'solutions' else stage
    injected = pipeline.state.setdefault('injectedFailures', [])
    point = key == pipeline.state['slots'][8]['repoId'] or stage == 'masks'
    if pipeline.state.get('failOnceAt') == publication_stage and publication_stage not in injected and point:
        injected.append(publication_stage)
        pipeline.save()
        raise RuntimeError('Intentional one-time dry-run failure in ' + publication_stage +
                           '; re-run failed jobs to test checkpoint recovery')


def freeze_sources(pipeline):
    created = utc()
    pipeline.state['model'] = {'recordType': 'synthetic-fixture-engine', 'tag': 'qwen3:235b',
        'digest': None, 'effectiveContext': 32768, 'preflightStatus': 'not-run-dry-run',
        'inferenceCalls': 0, 'modelsLoadedByThisRun': 0, 'synthetic': True}
    pipeline.state['openCodeVersion'] = 'not-invoked-dry-run'
    capabilities = {'synthetic': True, 'realConnectionsProbed': False,
                    'inferenceCalls': 0, 'servers': list(read(pipeline.trusted / 'opencode.json')['mcp'])}
    if (pipeline.root / 'preflight-mcp-capabilities.json').exists():
        capabilities = read(pipeline.root / 'preflight-mcp-capabilities.json')
        capabilities.update(realConnectionsProbed=True, inferenceCalls=0)
        pipeline.state['openCodeVersion'] = capabilities['openCodeVersion']
    elif pipeline.state.get('probeMcp') and mode_of(pipeline.state) == 'repo-web':
        with OpenCode(pipeline.work, pipeline.trusted, read(pipeline.trusted / 'opencode.json'),
                      pipeline.root / 'runtime/mcp-probe', pipeline.state['model']) as server:
            capabilities = server.redact(server.discover())
            capabilities.update(realConnectionsProbed=True, inferenceCalls=0)
            pipeline.state['openCodeVersion'] = server.version
    write(pipeline.work / 'frozen/mcp-capabilities.json', capabilities)
    source = {'id': SOURCE_ID, 'type': 'synthetic-test-fixture',
        'title': 'SYNTHETIC DRY RUN: no authoritative source was fetched',
        'publisher': 'UNIQ2Me CI fixture generator', 'url': SOURCE_URL,
        'language': 'en', 'jurisdiction': 'not-applicable-synthetic',
        'publicationDate': None, 'effectiveDate': None, 'createdAt': created,
        'sourceStatus': 'synthetic-not-fetched', 'synthetic': True,
        'passages': [{'id': 'synthetic-passage', 'locator': 'synthetic fixture only',
                      'text': 'SYNTHETIC DRY RUN passage. It establishes no technical or legal requirement.'}]}
    write(pipeline.work / 'frozen/sources.json', {'synthetic': True,
        'rag': rag_policy(mode_of(pipeline.state)),
        'cutoffUtc': created, 'sources': [source], 'authoritativeSourcesFetched': 0})
    write(pipeline.work / 'frozen/concerns.json', [{'id': sfs + '-dry-concern',
        'sfsId': sfs, 'description': 'SYNTHETIC DRY RUN concern for area ' + sfs,
        'sourceRefs': REFS, 'synthetic': True} for sfs in HEX])
    write(pipeline.work / 'frozen/queries.json', {'synthetic': True,
                                               'actualSearchesExecuted': 0, 'queries': []})
    config = read(pipeline.work / 'agent-rules/run-config.json')
    config['configurationStatus'] = 'dry-run-synthetic-no-inference'
    config['executionMode'] = 'dry-run'
    config['inputs']['sources'].update(corpusRef='frozen/sources.json', cutoffUtc=created,
                                       snapshotDigest=digest(source))
    write(pipeline.work / 'agent-rules/run-config.json', config)
    pipeline.state['rulesManifest'] = file_manifest(pipeline.work / 'agent-rules')
    pipeline.state['frozenManifest'] = file_manifest(pipeline.work / 'frozen')
    pipeline.state['sourceCutoffUtc'] = created
    pipeline.save()
    log('dry_run.freeze_sources', 'Dry-run preparation completed: zero model calls; source/model responses are synthetic')


def question(slot):
    return {'recordType': 'question-candidate', 'identity': {**slot, 'language': 'en'},
        'shortBinaryQuestion': 'DRY RUN: Has synthetic test condition ' + slot['repoId'] + ' been satisfied?',
        'contract': {'actor': 'SYNTHETIC test owner', 'scope': 'SYNTHETIC test scope',
            'evidencePeriod': 'SYNTHETIC test period', 'positive': 'Synthetic fixture satisfies the test criterion',
            'negative': 'Synthetic fixture fails the test criterion', 'missingEvidence': 'Pending synthetic evidence',
            'requiredEvidence': ['Synthetic test record'], 'alternatives': ['Equivalent synthetic fixture']},
        'sourceRefs': REFS, 'concernIds': [slot['sfsId'] + '-dry-concern'],
        'fixtures': [{'id': kind, 'kind': kind, 'synthetic': True,
                      'evidence': 'SYNTHETIC ' + kind + ' evidence', 'expected': outcome}
                     for kind, outcome in [('satisfied', 'satisfied'), ('failed', 'failed'), ('missing', 'pending')]],
        'unresolved': [], 'parentQuestionDigest': None, 'semanticRelation': 'initial',
        'changeReason': 'SYNTHETIC DRY RUN fixture; no authoring/model judgment occurred', 'synthetic': True}


def solution(slot, q, language='en'):
    return {'recordType': 'canonical-solution', 'identity': {**slot, 'language': language},
        'questionDigest': digest(q), 'desiredEffect': 'SYNTHETIC test effect',
        'environment': 'SYNTHETIC environment', 'resourceBasis': 'SYNTHETIC assumptions; no measurements',
        'rollback': 'SYNTHETIC rollback; no commands executed', 'prerequisites': ['Synthetic fixture'],
        'steps': ['SYNTHETIC example-tool --version; test version 1.0'],
        'verification': ['Compare synthetic fixture with the test criterion'],
        'alternatives': ['Equivalent synthetic test route'], 'versions': ['Synthetic example-tool 1.0'],
        'sourceRefs': REFS, 'protectedLiterals': ['example-tool --version', '1.0'],
        'executionValidation': 'not-executed', 'unresolved': [], 'synthetic': True}


def sf(pipeline, slot, q, language='en'):
    value = copy.deepcopy(pipeline.template(slot))
    value['identity'] = {**slot, 'language': language}
    value['isPlaceholder'] = False
    prefixes = {'en': 'SYNTHETIC DRY RUN', 'fr': 'TEST SYNTHÉTIQUE', 'el': 'ΣΥΝΘΕΤΙΚΗ ΔΟΚΙΜΗ'}
    for key in value['question']:
        value['question'][key] = prefixes[language] + ' guidance for fixture ' + slot['repoId']
    value['question']['shortBinaryQuestion'] = q['shortBinaryQuestion'] if language == 'en' else (
        prefixes[language] + ': condition ' + slot['repoId'] + '?')
    value['meta']['title'] = prefixes[language] + ' ' + slot['repoId']
    value['clarificationQuestion'] = [{'key1': prefixes[language] + ' clarification',
                                      'key2': 'Synthetic evidence explanation; no actual assessment'}]
    value['wikiLinks'] = [{'key1': 'SYNTHETIC DRY RUN reference; not an authoritative source', 'key2': SOURCE_URL}]
    value['openSourceApproach'] = prefixes[language] + ': no real implementation; ' + (
        f'solutions/{language}/{slot["repoId"]}.json')
    value['editLink'] = pipeline.link('edit', f'repo-sf/{language}/{slot["repoId"]}.json')
    value['jsonLink'] = pipeline.link('blob', f'repo-sf/{language}/{slot["repoId"]}.json')
    value['authorGithubId'] = 'stefanospetsios'
    for key in ('glacierWiki', 'glacierLink', 'glacerQRcode', 'linkLatest', 'resultScore'):
        value[key] = None
    value['review'] = {'status': 'draft', 'approvals': []}
    return value


def masks(binding, language='en'):
    value = {'masks': [], 'coverage': []}
    for jurisdiction in ('EU', 'GR'):
        mask_id = 'synthetic-nis2-' + jurisdiction.lower()
        value['masks'].append({'schemaVersion': 1, 'id': mask_id, 'language': language,
            'title': 'SYNTHETIC DRY RUN mask ' + language,
            'description': 'Test fixture only; no legal mapping was performed',
            'purpose': 'SYNTHETIC requirement-selection test', 'scope': 'SYNTHETIC entity scope',
            'evidencePeriod': 'SYNTHETIC period', 'status': 'draft',
            'catalog': {'language': language, 'edition': None, 'digest': binding},
            'selection': {'type': 'sf', 'positions': [{'sfsId': '0', 'sfId': '0'}]},
            'references': [{'title': 'SYNTHETIC source', 'url': SOURCE_URL}]})
        value['coverage'].append({'maskId': mask_id, 'instrument': 'SYNTHETIC NIS2 test identifier',
            'jurisdiction': jurisdiction, 'entityScope': 'SYNTHETIC entity scope',
            'applicabilityAssumptions': 'SYNTHETIC test assumptions',
            'uncoveredObligations': ['SYNTHETIC gap; this test establishes no coverage'],
            'unresolved': [], 'mappings': [{'article': 'synthetic-article',
                'rationale': 'SYNTHETIC test mapping; not a legal interpretation', 'sourceRefs': REFS,
                'positions': [{'sfsId': '0', 'sfId': '0'}]}]})
    return value


def candidate(pipeline, stage, key):
    if stage == 'masks' or key.startswith('masks-'):
        language = 'en' if stage == 'masks' else stage.split('-')[1]
        return masks(catalog_digest(pipeline.work, language), language)
    if key.startswith('area-'):
        area = read(pipeline.work / 'json/en' / (key[-1] + '.json'))
        value = {k: 'SYNTHETIC translation: ' + area[k] for k in
                 ('keyTitle', 'keyShortInfo', 'keyDescription', 'keyWords', 'promptAi')}
        value['meta'] = {k: 'SYNTHETIC translation: ' + area['meta'][k] for k in ('title', 'menuTitle')}
        return value
    slot = next(s for s in pipeline.state['slots'] if s['repoId'] == key)
    if stage == 'catalog':
        return question(slot)
    q = pipeline.question(slot)
    if stage == 'solutions':
        return solution(slot, q)
    if stage == 'extended':
        return sf(pipeline, slot, q)
    language = stage.split('-')[1]
    return {'sf': sf(pipeline, slot, q, language), 'solution': solution(slot, q, language),
            'contract': q['contract'], 'glossary': [{'en': 'Synthetic fixture', language: 'Synthetic fixture'}]}
