"""Synthetic controller tests; no model, external pushes or research observations."""
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import (HEX, QUESTION_CHECKS, SOLUTION_CHECKS, FIELD_CHECKS, TRANSLATION_CHECKS, MASK_CHECKS,
    byte_digest, catalog_digest, digest,
    expand_mask, file_manifest, read, validate_masks, validate_question,
    validate_review, validate_sf, validate_solution, write)
from ci.pipeline import Pipeline, STAGES, fetch_source, valid_release
from ci.runtime import (MissingToolCallsError, ModelAnswerError, OpenCode, native_tool_probe, ollama_preflight, parse_json_answer,
                        http_json, public_endpoint, reported_usage, safe_tool)

FIXTURES = Path(__file__).parent / 'fixtures'
SOURCE = {'id': 'synthetic-source', 'url': 'https://www.cisa.gov/synthetic-test',
          'passages': [{'id': 'p1', 'text': 'Synthetic source passage for tests only.', 'locator': 'test'}]}
SOURCES = {SOURCE['id']: SOURCE}
REFS = [{'sourceId': SOURCE['id'], 'passageId': 'p1'}]


def question(slot):
    return {'recordType': 'question-candidate', 'identity': {**slot, 'language': 'en'},
        'shortBinaryQuestion': 'Has synthetic condition ' + slot['repoId'] + ' been satisfied?',
        'contract': {'actor': 'Synthetic test owner', 'scope': 'Synthetic test scope',
            'evidencePeriod': 'Synthetic period', 'positive': 'Test evidence satisfies the criterion',
            'negative': 'Test evidence contradicts the criterion', 'missingEvidence': 'Pending test evidence',
            'requiredEvidence': ['Synthetic test record'], 'alternatives': ['Equivalent test evidence']},
        'sourceRefs': REFS, 'concernIds': [slot['sfsId'] + '-concern'],
        'fixtures': [{'id': kind, 'kind': kind, 'synthetic': True,
                      'evidence': 'Synthetic ' + kind + ' evidence', 'expected': outcome}
                     for kind, outcome in [('satisfied', 'satisfied'), ('failed', 'failed'), ('missing', 'pending')]],
        'unresolved': [], 'parentQuestionDigest': None, 'semanticRelation': 'initial',
        'changeReason': 'Synthetic initial generation for controller tests only'}


def solution(slot, q, language='en'):
    return {'recordType': 'canonical-solution', 'identity': {**slot, 'language': language},
        'questionDigest': digest(q), 'desiredEffect': 'Synthetic desired effect',
        'environment': 'Synthetic environment', 'resourceBasis': 'Synthetic assumptions, no measurements',
        'rollback': 'Synthetic rollback', 'prerequisites': ['Synthetic fixture'],
        'steps': ['Check example-tool --version with version 1.0'],
        'verification': ['Verify synthetic test evidence'], 'alternatives': ['Equivalent test procedure'],
        'versions': ['example-tool 1.0'], 'sourceRefs': REFS,
        'protectedLiterals': ['example-tool --version', '1.0'],
        'executionValidation': 'not-executed', 'unresolved': []}


def sf(slot, q, language='en'):
    value = read(FIXTURES / 'sf-template.json')
    value['identity'] = {**slot, 'language': language}
    value['isPlaceholder'] = False
    for key in value['question']:
        value['question'][key] = 'Synthetic authoring guidance ' + language + ' é Ελληνικά'
    value['question']['shortBinaryQuestion'] = q['shortBinaryQuestion'] if language == 'en' else (
        'Synthetic translated question ' + slot['repoId'] + ' ' + language)
    value['meta']['title'] = 'Synthetic question ' + slot['repoId']
    value['clarificationQuestion'] = [{'key1': 'Synthetic clarification', 'key2': 'Synthetic evidence explanation'}]
    value['wikiLinks'] = [{'key1': 'Synthetic source', 'key2': SOURCE['url']}]
    value['openSourceApproach'] = 'Synthetic canonical approach; not executed; ' + (
        f'solutions/{language}/{slot["repoId"]}.json')
    for key in ('editLink', 'jsonLink'):
        value[key] = 'https://github.com/stefanospetsios/uniq2me-ai/blob/qwen3%2F261012/' + (
            f'repo-sf/{language}/{slot["repoId"]}.json')
    return value


def masks(binding, language='en'):
    result = {'masks': [], 'coverage': []}
    for jurisdiction in ('EU', 'GR'):
        mask_id = 'nis2-' + jurisdiction.lower()
        result['masks'].append({'schemaVersion': 1, 'id': mask_id, 'language': language,
            'title': 'Synthetic mask', 'description': 'Synthetic draft for controller tests',
            'purpose': 'Synthetic requirement selection', 'scope': 'Synthetic entity scope',
            'evidencePeriod': 'Synthetic period', 'status': 'draft',
            'catalog': {'language': language, 'edition': None, 'digest': binding},
            'selection': {'type': 'sf', 'positions': [{'sfsId': '0', 'sfId': '0'}]},
            'references': [{'title': 'Synthetic source', 'url': SOURCE['url']}]})
        result['coverage'].append({'maskId': mask_id, 'instrument': 'Synthetic NIS2 test reference',
            'jurisdiction': jurisdiction, 'entityScope': 'Synthetic entity scope',
            'applicabilityAssumptions': 'Synthetic assumptions', 'uncoveredObligations': ['Synthetic gap'],
            'unresolved': [], 'mappings': [{'article': 'Synthetic test article',
                'rationale': 'Synthetic mapping rationale', 'sourceRefs': REFS,
                'positions': [{'sfsId': '0', 'sfId': '0'}]}]})
    return result


def review(artifact, checks, fixtures=None):
    return {'artifactDigest': digest(artifact), 'decision': 'accepted',
            'checks': [{'ruleId': check, 'passed': True,
                        'rationale': 'Synthetic controller test rationale', 'sourceRefs': REFS}
                       for check in checks], 'unresolved': [],
            'fixtureOutcomes': [{'id': f['id'], 'outcome': f['expected']} for f in fixtures or []]}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.slot = {'sfsId': '0', 'sfId': '0', 'repoId': '0000'}
        self.q = question(self.slot)
        self.s = solution(self.slot, self.q)
        self.schema = read(FIXTURES / 'mask.schema.json')

    def test_question_reports_all_independent_structural_errors_for_one_repair(self):
        self.q['contract']['alternatives'] = []
        self.q['contract']['scope'] = ''
        self.q['fixtures'][-1]['evidence'] = ''
        self.q['semanticRelation'] = 'changed'
        with self.assertRaises(ValueError) as failure:
            validate_question(self.q, self.slot, SOURCES, {'0-concern'},
                lineage={'parentQuestionDigest': None, 'semanticRelations': ('initial',)})
        for expected in ('alternatives must be filled', 'contract.scope must be filled',
                         'fixtures[2]: fixture.evidence must be filled', 'Question needs explicit semantic lineage'):
            self.assertIn(expected, str(failure.exception))

    def test_question_type_repair_reports_the_required_literal_and_actual_value(self):
        for actual in (None,'question', 'security-factor'):
            value=copy.deepcopy(self.q)
            if actual is None:
                value.pop('recordType')
            else:
                value['recordType']=actual
            with self.subTest(actual=actual),self.assertRaises(ValueError) as caught:
                validate_question(value,self.slot,SOURCES,{'0-concern'})
            self.assertIn('recordType must equal "question-candidate"',str(caught.exception))
            self.assertIn('received '+('missing' if actual is None else json.dumps(actual)),str(caught.exception))
            self.assertEqual(value.get('recordType'),actual)

    def test_malformed_question_fields_produce_validation_feedback_without_type_errors(self):
        self.q.update(contract=None, concernIds=[{}], fixtures=[None, {'kind': []}])
        with self.assertRaises(ValueError) as failure:
            validate_question(self.q, self.slot, SOURCES, {'0-concern'})
        for expected in ('contract must be an object', 'Invalid concern IDs', 'fixtures[0] must be an object'):
            self.assertIn(expected, str(failure.exception))

    def test_truthful_completed_fields_keep_null_assessment_values(self):
        validate_question(self.q, self.slot, SOURCES, {'0-concern'})
        validate_solution(self.s, self.slot, SOURCES, self.q)
        candidate = sf(self.slot, self.q)
        validate_sf(candidate, read(FIXTURES / 'sf-template.json'), self.slot,
                    SOURCES, self.q, self.s)
        for key in ('resultScore', 'glacerQRcode'):
            mutated = copy.deepcopy(candidate)
            mutated[key] = 'Invented value'
            with self.assertRaises(ValueError):
                validate_sf(mutated, read(FIXTURES / 'sf-template.json'), self.slot,
                            SOURCES, self.q, self.s)

    def test_unknown_sources_and_changed_accepted_question_are_blockers(self):
        self.q['sourceRefs'] = [{'sourceId': 'invented', 'passageId': 'p1'}]
        with self.assertRaisesRegex(ValueError, 'Unknown frozen source'):
            validate_question(self.q, self.slot, SOURCES, {'0-concern'})
        self.q = question(self.slot)
        candidate = sf(self.slot, self.q)
        candidate['question']['shortBinaryQuestion'] += ' And something else?'
        with self.assertRaisesRegex(ValueError, 'changed the accepted question'):
            validate_sf(candidate, read(FIXTURES / 'sf-template.json'), self.slot,
                        SOURCES, self.q, self.s)

    def test_unknown_concern_diagnostic_supplies_exact_allowed_ids_for_repair(self):
        self.q['concernIds'] = ['0-concern-access-controls']
        with self.assertRaisesRegex(ValueError, '0-concern-access-controls.*allowed concern IDs.*0-concern-topic-boundaries'):
            validate_question(self.q, self.slot, SOURCES, {'0-concern-topic-boundaries'})

    def test_legacy_keys_and_types_cannot_be_removed(self):
        candidate = sf(self.slot, self.q)
        del candidate['glacerQRcode']
        with self.assertRaisesRegex(ValueError, 'template keys'):
            validate_sf(candidate, read(FIXTURES / 'sf-template.json'), self.slot,
                        SOURCES, self.q, self.s)

    def test_review_is_bound_to_exact_candidate_and_all_fixture_outcomes(self):
        value = review(self.q, QUESTION_CHECKS, self.q['fixtures'])
        validate_review(value, self.q, QUESTION_CHECKS, SOURCES, self.q['fixtures'])
        value['fixtureOutcomes'][-1]['outcome'] = 'failed'
        with self.assertRaisesRegex(ValueError, 'different judgments'):
            validate_review(value, self.q, QUESTION_CHECKS, SOURCES, self.q['fixtures'])
        value = review(self.q, QUESTION_CHECKS)
        value['artifactDigest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'different bytes'):
            validate_review(value, self.q, QUESTION_CHECKS, SOURCES)

    def test_candidate_review_checks_can_use_the_artifact_without_external_citations(self):
        value=review(self.q,QUESTION_CHECKS,self.q['fixtures'])
        local={'single-condition','nontechnical-wording','fixture-decisions'}
        for row in value['checks']:
            if row['ruleId'] in local:
                row['sourceRefs']=[]
        validate_review(value,self.q,QUESTION_CHECKS,SOURCES,self.q['fixtures'])
        for rule in ('source-support','scope-and-evidence','concern-coverage'):
            broken=copy.deepcopy(value)
            next(row for row in broken['checks'] if row['ruleId']==rule)['sourceRefs']=[]
            with self.subTest(rule=rule),self.assertRaisesRegex(ValueError,'Review check '+rule+' sourceRefs'):
                validate_review(broken,self.q,QUESTION_CHECKS,SOURCES,self.q['fixtures'])
        broken=copy.deepcopy(value)
        row=next(row for row in broken['checks'] if row['ruleId']=='nontechnical-wording')
        row['sourceRefs']=[{'sourceId':'invented','passageId':'p1'}]
        with self.assertRaisesRegex(ValueError,'Unknown frozen source: invented'):
            validate_review(broken,self.q,QUESTION_CHECKS,SOURCES,self.q['fixtures'])
        broken=copy.deepcopy(value)
        broken['fixtureOutcomes'][0]['outcome']='pending'
        with self.assertRaisesRegex(ValueError,'different judgments'):
            validate_review(broken,self.q,QUESTION_CHECKS,SOURCES,self.q['fixtures'])

    def test_artifact_checks_across_stages_do_not_require_unrelated_web_passages(self):
        groups=[
            (SOLUTION_CHECKS,
             {'source-support','implementable-route','alternatives','rollback-and-verification'}),
            (FIELD_CHECKS,
             {'source-support'}),
            (TRANSLATION_CHECKS,set()),
            (MASK_CHECKS,
             {'legal-source-support','entity-and-jurisdiction','article-mapping','uncovered-obligations'}),
            (['source-support','fixed-area-boundaries','no-duplicate-criteria','no-contradictions','concern-coverage'],
             {'source-support','concern-coverage'})]
        for checks,evidence_checks in groups:
            with self.subTest(checks=checks):
                value=review(self.q,checks,self.q['fixtures'])
                for row in value['checks']:
                    if row['ruleId'] not in evidence_checks:
                        row['sourceRefs']=[]
                validate_review(value,self.q,checks,SOURCES,self.q['fixtures'])
                for rule in evidence_checks:
                    broken=copy.deepcopy(value)
                    next(row for row in broken['checks'] if row['ruleId']==rule)['sourceRefs']=[]
                    with self.assertRaisesRegex(ValueError,'Review check '+rule+' sourceRefs'):
                        validate_review(broken,self.q,checks,SOURCES,self.q['fixtures'])

    def test_mask_bit_order_leading_zeros_and_language_binding(self):
        fields = expand_mask({'type': 'sf', 'positions': [
            {'sfsId': '0', 'sfId': '0'}, {'sfsId': '0', 'sfId': 'F'}, {'sfsId': '1', 'sfId': 'E'}]})
        self.assertEqual(fields['0'], '8001')
        self.assertEqual(fields['1'], '0002')
        self.assertEqual(fields['F'], '0000')
        binding = 'sha256:' + 'a' * 64
        value = masks(binding, 'fr')
        validate_masks(value, self.schema, 'fr', binding, SOURCES)
        value['masks'][0]['catalog']['digest'] = 'sha256:' + 'b' * 64
        with self.assertRaisesRegex(ValueError, 'Stale mask'):
            validate_masks(value, self.schema, 'fr', binding, SOURCES)

    def test_unmapped_selection_and_manufactured_review_are_blockers(self):
        binding = 'sha256:' + 'a' * 64
        value = masks(binding)
        value['masks'][0]['selection']['positions'].append({'sfsId': '0', 'sfId': '1'})
        with self.assertRaisesRegex(ValueError, 'Every selected bit'):
            validate_masks(value, self.schema, 'en', binding, SOURCES)
        value = masks(binding)
        value['masks'][0]['provenance'] = {'review': {'status': 'accepted',
            'reviewers': ['Invented legal reviewer'], 'reviewedAt': '2026-10-12T00:00:00Z',
            'notes': 'Synthetic invented review; should be rejected'}}
        with self.assertRaisesRegex(ValueError, 'manufacture'):
            validate_masks(value, self.schema, 'en', binding, SOURCES)

    def test_output_json_does_not_hide_duplicate_keys_or_prose(self):
        self.assertEqual(parse_json_answer('```json\n{"a": 1}\n```'), {'a': 1})
        with self.assertRaises(ValueError):
            parse_json_answer('{"a": 1, "a": 2}')
        with self.assertRaises(ValueError):
            parse_json_answer('The answer is {"a": 1}')

    def test_only_assistant_json_failures_have_the_model_answer_error_type(self):
        for answer in ('{"text":"unfinished', '{"a":1,"a":2}', 'prose {"a":1}',
                       '{"a":NaN}', '```json\n{"a":1}'):
            with self.subTest(answer=answer),self.assertRaises(ModelAnswerError):
                parse_json_answer(answer)
        self.assertEqual(parse_json_answer('{"text":"complete"}'),{'text':'complete'})

    def test_read_only_mcp_tool_permissions(self):
        for name in ('searxng_web_search', 'websearch_fetch_url', 'github_get_file_contents',
                     'git_git_show', 'filesystem_read_file', 'playwright_browser_snapshot'):
            self.assertTrue(safe_tool(name), name)
        for name in ('github_push_files', 'git_git_commit', 'filesystem_write_file',
                     'playwright_browser_evaluate', 'playwright_browser_click', 'bash', 'task'):
            self.assertFalse(safe_tool(name), name)

    def test_unsafe_tag_suffixes_are_rejected(self):
        self.assertEqual(valid_release('261012'), '261012')
        for value in ('../main', 'a/b', 'a..b', 'a.lock', 'a.', '', 'x;echo danger'):
            with self.assertRaises(ValueError):
                valid_release(value)


class NativeToolProbeTests(unittest.TestCase):
    def test_truncated_reasoning_retries_with_warm_model_and_shared_deadline(self):
        now, requests = [100.0], []
        def api(url, request, *, timeout):
            requests.append((copy.deepcopy(request), timeout))
            now[0] += 80  # Synthetic slow loading, included in the shared deadline.
            if len(requests) == 1:
                return {'message': {'content': 'Synthetic reasoning still in progress'},
                        'done_reason': 'length', 'eval_count': 128, 'load_duration': 77_000_000_000}
            return {'message': {'tool_calls': [{'function': {'name': 'preflight_probe',
                                                             'arguments': {'value': 'ready'}}}]},
                    'done_reason': 'stop', 'eval_count': 300, 'load_duration': 1_000_000}
        with tempfile.TemporaryDirectory() as directory, \
             patch('ci.runtime.http_json', side_effect=api), \
             patch('ci.runtime.time.monotonic', side_effect=lambda: now[0]), redirect_stdout(io.StringIO()):
            record = native_tool_probe('http://fixture', directory, 'qwen3:235b', 262144, True, 900)
            self.assertEqual([request['options']['num_predict'] for request, _ in requests], [2048, 8192])
            self.assertEqual([timeout for _, timeout in requests], [900, 820])
            self.assertTrue(all(request['model'] == 'qwen3:235b' and request['keep_alive'] == '24h'
                                and request['options']['num_ctx'] == 262144 and request['think'] is True
                                for request, _ in requests))
            self.assertEqual([a['validNativeToolCall'] for a in record['attempts']], [False, True])
            self.assertEqual(record['attempts'][0]['loadDuration'], 77_000_000_000)
            self.assertEqual(read(Path(directory) / record['archivePath'] / 'attempts.json'), record['attempts'])
            for attempt in record['attempts']:
                self.assertTrue((Path(directory) / attempt['responsePath']).exists())

    def test_prose_describing_a_tool_call_does_not_pass(self):
        response = {'message': {'content': '<tool_call>{"name":"preflight_probe",'
                    '"arguments":{"value":"ready"}}</tool_call>'}, 'done_reason': 'stop'}
        with tempfile.TemporaryDirectory() as directory, \
             patch('ci.runtime.http_json', return_value=response) as api, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'plain text describing a tool call'):
                native_tool_probe('http://fixture', directory, 'qwen3:235b', 262144, True, 900)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(read(Path(directory) / 'tool-probe-response.json'), response)

    def test_repeated_truncation_fails_with_both_attempts_preserved(self):
        response = {'message': {'thinking': 'Synthetic unfinished reasoning'}, 'done_reason': 'length'}
        with tempfile.TemporaryDirectory() as directory, \
             patch('ci.runtime.http_json', return_value=response) as api, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'num_predict=8192'):
                native_tool_probe('http://fixture', directory, 'qwen3:235b', 262144, True, 900)
            self.assertEqual(api.call_count, 2)
            journals = list(Path(directory).glob('tool-probe-*/attempts.json'))
            self.assertEqual(len(journals), 1)
            self.assertEqual(len(read(journals[0])), 2)

    def test_a_later_probe_does_not_erase_failed_attempts(self):
        failed = {'message': {'content': 'Synthetic prose'}, 'done_reason': 'stop'}
        passed = {'message': {'tool_calls': [{'function': {'name': 'preflight_probe',
                                                        'arguments': {'value': 'ready'}}}]}}
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with patch('ci.runtime.http_json', return_value=failed):
                with self.assertRaises(ValueError):
                    native_tool_probe('http://fixture', directory, 'qwen3:235b', 262144, True, 900)
            with patch('ci.runtime.http_json', return_value=passed):
                native_tool_probe('http://fixture', directory, 'qwen3:235b', 262144, True, 900)
            responses = list(Path(directory).glob('tool-probe-*/1-response.json'))
            self.assertEqual(len(responses), 2)
            self.assertIn(failed, [read(path) for path in responses])


class RuntimeTests(unittest.TestCase):
    def test_missing_search_tool_gets_one_same_session_continuation(self):
        for completes_missing_tool in (True, False):
            with self.subTest(completes=completes_missing_tool), tempfile.TemporaryDirectory() as directory, \
                 redirect_stdout(io.StringIO()):
                server = OpenCode(directory, ROOT, {}, directory, {'digest': 'a' * 64, 'effectiveContext': 32768})
                messages, posts, sessions, aborts = [], [], [], []
                def api(path, data=None, **_):
                    if path == '/session':
                        sessions.append(path)
                        return {'id': 'same-source-session'}
                    if path.endswith('/prompt_async'):
                        posts.append(path)
                        parts = [{'type': 'text', 'text': '{"sources": [], "synthetic": true}'}]
                        if len(posts) == 1 or completes_missing_tool:
                            parts.append({'type': 'tool', 'tool': 'searxng_web_search' if len(posts) == 1 else
                                          'websearch_web_search', 'state': {'status': 'completed'}})
                        messages.append({'info': {'id': 'reply-' + str(len(posts)), 'role': 'assistant',
                            'providerID': 'ollama', 'modelID': 'qwen3:235b', 'time': {'completed': 1},
                            'finish': 'stop', 'tokens': {'total': 100}}, 'parts': parts})
                    if path.endswith('/message'):
                        return messages
                    if path.endswith('/abort'):
                        aborts.append(path)
                    if path == '/session/status':
                        return {}
                    return None
                server.api, server.check_residency = api, lambda: None
                groups = {'searxng': ['searxng_web_search'], 'websearch': ['websearch_web_search']}
                if completes_missing_tool:
                    self.assertTrue(server.ask('source-researcher', 'Synthetic discovery', 30,
                                              'source-test', required_tool_groups=groups)['synthetic'])
                else:
                    with self.assertRaisesRegex(MissingToolCallsError, 'still lacks completed calls for: websearch') as caught:
                        server.ask('source-researcher', 'Synthetic discovery', 30,
                                   'source-test', required_tool_groups=groups)
                    self.assertEqual(caught.exception.missing_groups, ['websearch'])
                self.assertEqual(len(sessions), 1)
                self.assertEqual(posts, ['/session/same-source-session/prompt_async'] * 2)
                envelope = read(Path(directory) / 'calls/source-test/envelope.json')
                self.assertEqual(envelope['toolContinuations'], 1)
                self.assertEqual(len(envelope['providerUsage']), 2)
                self.assertEqual(envelope['status'], 'completed' if completes_missing_tool else 'failed')

    def test_preflight_version_metadata_can_be_intercepted_without_skipping_native_checks(self):
        gateway = {'Version': '3.7.13', 'Codename': 'langres', 'startDate': 'synthetic-gateway-start'}
        cases = [({'version': 'synthetic-ollama-version'}, 'observed'),
                 (gateway, 'unexpected-response'), (None, 'unexpected-response'),
                 ({'version': 123}, 'unexpected-response'),
                 (HTTPError('http://fixture/api/version', 404, 'Not found', {}, None), 'http-error'),
                 (json.JSONDecodeError('Not JSON', '<html>', 0), 'non-json-response')]
        for response, expected_status in cases:
            with self.subTest(status=expected_status, response=response), tempfile.TemporaryDirectory() as directory:
                calls = []
                def api(url, data=None, **_):
                    calls.append(url)
                    if url.endswith('/api/version'):
                        if isinstance(response, Exception):
                            raise response
                        return response
                    if url.endswith('/api/tags'):
                        return {'models': [{'name': 'qwen3:235b', 'digest': 'a' * 64}]}
                    if url.endswith('/api/show'):
                        return {'model_info': {'general.architecture': 'qwen3', 'qwen3.context_length': 262144},
                                'capabilities': ['completion', 'tools', 'thinking']}
                    if url.endswith('/api/chat'):
                        self.assertEqual(data['options']['num_ctx'], 262144)
                        self.assertTrue(data['think'])
                        return {'message': {'tool_calls': [{'function': {'name': 'preflight_probe',
                                                                       'arguments': {'value': 'ready'}}}]}}
                    if url.endswith('/api/ps'):
                        return {'models': [{'digest': 'a' * 64, 'context_length': 262144}]}
                    if url.endswith('/v1/chat/completions'):
                        return {'choices': [{'message': {'role': 'assistant', 'content': 'Synthetic ready'}}]}
                    raise AssertionError(url)
                with patch('ci.runtime.http_json', side_effect=api), redirect_stdout(io.StringIO()):
                    record = ollama_preflight('http://fixture', directory)
                self.assertEqual(record['preflightStatus'], 'passed')
                self.assertEqual(record['ollamaVersionStatus'], expected_status)
                self.assertEqual(record['ollamaVersion'],
                                 'synthetic-ollama-version' if expected_status == 'observed' else None)
                self.assertTrue(record['nativeToolCallingSupported'])
                self.assertTrue(record['openAiCompatibleAllocationVerified'])
                self.assertEqual(calls.count('http://fixture/api/ps'), 2)
                self.assertIn('http://fixture/api/chat', calls)
                self.assertIn('http://fixture/v1/chat/completions', calls)
                self.assertTrue((Path(directory) / 'version.json').exists())
                if response is gateway:
                    self.assertEqual(read(Path(directory) / 'version.json'), gateway)
                    self.assertTrue(any('version is unknown' in item for item in record['limitations']))

    def test_preflight_unknown_version_does_not_accept_a_non_ollama_backend(self):
        gateway = {'Version': '3.7.13', 'Codename': 'langres'}
        with tempfile.TemporaryDirectory() as directory, \
             patch('ci.runtime.http_json', side_effect=[gateway, gateway]) as api, \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, '/api/tags did not expose a model list'):
                ollama_preflight('http://fixture', directory)
            record = read(Path(directory) / 'preflight.json')
            self.assertEqual(record['preflightStatus'], 'failed')
            self.assertIsNone(record['ollamaVersion'])
            self.assertEqual(api.call_count, 2)  # No model inference at the wrong backend.

    def test_preflight_requires_explicit_supported_context_fallback(self):
        model_digest = 'a' * 64
        def api(url, data=None, **_):
            if url.endswith('/api/version'):
                return {'version': 'synthetic-test-version'}
            if url.endswith('/api/tags'):
                return {'models': [{'name': 'qwen3:235b', 'digest': model_digest}]}
            if url.endswith('/api/show'):
                return {'model_info': {'general.architecture': 'qwen3', 'qwen3.context_length': 32768},
                        'template': 'Synthetic test template'}
            if url.endswith('/api/chat'):
                return {'message': {'tool_calls': [{'function': {'name': 'preflight_probe',
                                                                 'arguments': {'value': 'ready'}}}]}}
            if url.endswith('/v1/chat/completions'):
                return {'choices': [{'message': {'role': 'assistant', 'content': 'Synthetic ready'}}]}
            if url.endswith('/api/ps'):
                return {'models': [{'digest': model_digest, 'context_length': 32768}]}
            raise AssertionError(url)
        with tempfile.TemporaryDirectory() as directory, patch('ci.runtime.http_json', side_effect=api):
            with self.assertRaisesRegex(ValueError, 'unsupported'):
                ollama_preflight('http://localhost:11434', Path(directory) / 'failed')
            failed = read(Path(directory) / 'failed/preflight.json')
            self.assertEqual(failed['preflightStatus'], 'failed')
            value = ollama_preflight('http://localhost:11434', Path(directory) / 'passed',
                                     '32768', 'Synthetic native-limit fallback')
            self.assertEqual(value['observedContext'], 32768)
            self.assertTrue(value['explicitContextFallback'])
            self.assertEqual(value['longContextSmoke'], 'not-run')
            self.assertTrue(value['openAiCompatibleAllocationVerified'])

    def test_successful_opencode_answer_records_the_actual_model_and_prompt_bytes(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(output), patch.dict(os.environ, {
                'OLLAMA_BASE_URL': 'https://user:private-test-value@fixture.example:443'}):
            server = OpenCode(directory, ROOT, {}, directory, {'digest': 'a' * 64, 'effectiveContext': 32768})
            messages = [{'info': {'role': 'assistant', 'providerID': 'ollama', 'modelID': 'qwen3:235b',
                         'time': {'completed': 1}, 'finish': 'stop', 'tokens': {'input': 20, 'output': 5}},
                         'parts': [{'type': 'text', 'text': '{"synthetic": true}'}]}]
            def api(path, data=None, **_):
                if path == '/session':
                    return {'id': 'synthetic-session'}
                if path == '/session/status':
                    return {}
                if path.endswith('/message'):
                    return messages
                return None
            server.api = api
            server.check_residency = lambda: None
            answer = server.ask('question-author', 'Synthetic JSON response test', 5, 'success')
            self.assertEqual(answer, {'synthetic': True})
            envelope = read(Path(directory) / 'calls/success/envelope.json')
            self.assertEqual(envelope['status'], 'completed')
            self.assertEqual(envelope['observedProviderModels'], ['ollama/qwen3:235b'])
            self.assertEqual(envelope['configuredProviderEndpoint'], 'https://fixture.example:443/v1')
            self.assertEqual(public_endpoint('https://user:password@fixture/v1?secret=private-test-query'),
                             'https://fixture/v1')
            self.assertIsNone(envelope['reportedTokenUsage']['total'])
            self.assertIn('submit ollama/qwen3:235b', output.getvalue())
            self.assertIn('observed ollama/qwen3:235b', output.getvalue())
            self.assertIn('reported tokens=unknown', output.getvalue())
            self.assertNotIn('private-test-value', output.getvalue())
            self.assertNotIn('private-test-query', output.getvalue())
            self.assertEqual(envelope['promptDigest'], byte_digest(Path(directory) / 'calls/success/prompt.txt'))
            messages[0]['info']['modelID'] = 'unexpected-model'
            with self.assertRaisesRegex(ValueError, 'differs from the pinned'):
                server.ask('question-author', 'Synthetic rejected model test', 5, 'wrong-model')

    def test_reported_usage_deduplicates_messages_without_inventing_aborted_tokens(self):
        completed = {'info': {'id': 'completed', 'role': 'assistant', 'tokens': {
            'total': 239, 'input': 111, 'output': 18, 'cache': {'read': 110}}}}
        aborted = {'info': {'id': 'aborted', 'role': 'assistant', 'tokens': {'input': 0, 'output': 0}}}
        usage, summary = reported_usage([completed, completed, aborted])
        self.assertEqual(len(usage), 2)
        self.assertIsNone(summary['total'])
        self.assertEqual(summary['knownSubtotal'], 239)
        self.assertEqual(summary['responsesWithTotal'], 1)
        self.assertEqual(summary['modelResponses'], 2)

    def test_source_passages_are_checked_against_independently_fetched_bytes(self):
        class Response:
            headers = __import__('email.message', fromlist=['Message']).Message()
            headers['Content-Type'] = 'text/html; charset=utf-8'
            url = 'https://www.cisa.gov/synthetic-test'
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, _):
                return b'<html><p>A synthetic authoritative passage for this test.</p></html>'
        candidate = copy.deepcopy(SOURCE)
        candidate['passages'][0]['text'] = 'A synthetic authoritative passage for this test.'
        import time
        with tempfile.TemporaryDirectory() as directory, patch('ci.pipeline.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response()
            value = fetch_source(candidate, directory, time.monotonic() + 60)
            self.assertEqual(value['sourceStatus'], 'fetched-and-passage-checked')
            # The preflight JSON contract identifies the fixed document by URL,
            # without a source ID. Even a passing require() evaluates its message.
            pilot = copy.deepcopy(candidate)
            pilot.pop('id')
            self.assertEqual(fetch_source(pilot, directory, time.monotonic() + 60)['sourceStatus'],
                             'fetched-and-passage-checked')
            pilot['passages'][0]['text'] = 'Synthetic absent quotation from the preflight pilot.'
            with self.assertRaisesRegex(ValueError, 'not found.*https://www.cisa.gov/synthetic-test/p1'):
                fetch_source(pilot, directory, time.monotonic() + 60)
            candidate['passages'][0]['text'] = 'This passage was fabricated and is absent from the fetched bytes.'
            with self.assertRaisesRegex(ValueError, 'not found'):
                fetch_source(candidate, directory, time.monotonic() + 60)

    def test_generation_repairs_share_the_original_five_minute_deadline(self):
        clock = [0]
        calls = []
        slot = {'sfsId': '0', 'sfId': '0', 'repoId': '0000'}
        class Server:
            def ask(self, role, prompt, seconds, call_id, **kwargs):
                calls.append(seconds)
                if len(calls) == 1:
                    clock[0] += 200
                    return {'invalid': True}
                if role == 'question-author':
                    clock[0] += 70
                    return question(slot)
                clock[0] += 10
                return review(question(slot), QUESTION_CHECKS, question(slot)['fixtures'])
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RUNNER_TEMP': directory}):
            pipeline = Pipeline()
            pipeline.state = {'maximumAttempts': 2, 'timeoutsMinutes': {'REVIEW_MINUTES': 2},
                              'model': {'digest': 'a' * 64}}
            pipeline.frozen_sources = lambda: SOURCES
            with patch('ci.pipeline.time.monotonic', side_effect=lambda: clock[0]), redirect_stdout(io.StringIO()):
                value = pipeline.generate(Server(), 'catalog', '0000', 'question-author',
                    'Synthetic question test', 5,
                    lambda v: validate_question(v, slot, SOURCES, {'0-concern'}), QUESTION_CHECKS)
            self.assertEqual(value['identity']['repoId'], '0000')
            self.assertEqual(calls, [300, 100, 30])
            self.assertTrue((pipeline.root / 'failures/catalog-0000-1.json').exists())
            self.assertEqual(read(pipeline.root / 'failures/catalog-0000-1.json')['function'],
                             'generate.validate')

    def test_timeout_aborts_actual_opencode_session_and_preserves_call(self):
        with tempfile.TemporaryDirectory() as directory:
            server = OpenCode(directory, ROOT, {}, directory, {'digest': 'a' * 64, 'effectiveContext': 32768})
            called = []
            def api(path, data=None, **_):
                called.append(path)
                if path == '/session':
                    return {'id': 'synthetic-session'}
                if path.endswith('/abort'):
                    return True
                if path == '/session/status':
                    return {'synthetic-session': {'type': 'idle' if any(
                        p.endswith('/abort') for p in called) else 'busy'}}
                if path.endswith('/message'):
                    return []
                return None
            server.api = api
            with self.assertRaises(TimeoutError):
                server.ask('question-author', 'Synthetic timeout test', 0.01, 'test-timeout')
            self.assertIn('/session/synthetic-session/abort', called)
            envelope = read(Path(directory) / 'calls/test-timeout/envelope.json')
            self.assertEqual(envelope['status'], 'failed')
            self.assertTrue((Path(directory) / 'calls/test-timeout/messages.json').exists())


class LifecycleTest(unittest.TestCase):
    def test_http_failure_names_method_and_route_without_url_credentials(self):
        endpoint = 'https://test-user:test-password@fixture.invalid/api/tags?token=test-query-secret'
        failure = HTTPError(endpoint, 404, 'Not found', {}, None)
        output = io.StringIO()
        with patch('ci.runtime.urllib.request.urlopen', side_effect=failure), redirect_stdout(output):
            with self.assertRaises(HTTPError) as observed:
                http_json(endpoint)
        self.assertIs(observed.exception, failure)
        self.assertIn('http_json: GET https://fixture.invalid/api/tags returned HTTP 404', output.getvalue())
        for secret in ('test-user', 'test-password', 'test-query-secret'):
            self.assertNotIn(secret, output.getvalue())

    def test_workflow_keeps_write_credentials_out_of_every_agent_stage(self):
        workflow = yaml.load((ROOT / '.github/workflows/catalog.yml').read_text(), Loader=yaml.BaseLoader)
        reusable = yaml.load((ROOT / '.github/actions/stage/action.yml').read_text(), Loader=yaml.BaseLoader)
        steps = reusable['runs']['steps']
        observed = False
        for step in steps:
            run = step.get('run', '')
            if run.startswith('python controller/ci/pipeline.py '):
                operation = run.split()[-1]
                if ' execute ' in run:
                    observed = True
                    self.assertNotIn('PUSH_TOKEN', step.get('env', {}))
                    self.assertNotIn('GITHUB_TOKEN', step.get('env', {}))
        self.assertTrue(observed)
        caller = yaml.load((ROOT/'examples/air.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(caller['on']['push']['tags'], ['deploy/qwen3/*'])
        jobs = workflow['jobs']
        self.assertEqual([job['with']['stage'] for job in jobs.values()],
                         ['preflight', 'prepare', 'refine', 'refine', 'refine', 'refine', 'refine'])
        for name, previous in [('prepare', 'preflight'), ('refine', 'prepare')]:
            self.assertEqual(jobs[name]['needs'], previous)
        self.assertTrue(any(' publish ' in s.get('run', '') for s in steps))
        bootstrap = yaml.load((ROOT/'.github/workflows/stage.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertIn("'5'", bootstrap['jobs']['stage']['env']['Z'])

    def test_full_256_position_lifecycle_makes_five_verified_local_remote_pushes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {'RUNNER_TEMP': str(root / 'temp'), 'GITHUB_WORKSPACE': str(root),
                   'PUSH_TOKEN': 'synthetic-local-git-test-token'}
            with patch.dict(os.environ, env), redirect_stdout(io.StringIO()):
                pipeline = Pipeline()
                pipeline.work.mkdir(parents=True)
                bare = root / 'synthetic-bare.git'
                subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True)
                pipeline.output.mkdir()
                subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=pipeline.output, check=True)
                (pipeline.output / 'README.md').write_text('Synthetic local controller test; no external publication.\n')
                self.git(pipeline.output, 'add', 'README.md')
                self.git(pipeline.output, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
                         'commit', '-qm', 'Synthetic base')
                self.git(pipeline.output, 'remote', 'add', 'origin', str(bare))
                base = self.git(pipeline.output, 'rev-parse', 'HEAD').strip()
                slots = []
                for sfs_id in HEX:
                    area = read(FIXTURES / 'sfs-template.json')
                    area['keyTitle'] = 'Synthetic fixed area ' + sfs_id
                    for entry in area['set']:
                        entry['repoId'] = '00' + sfs_id + entry['id']
                        slot = {'sfsId': sfs_id, 'sfId': entry['id'], 'repoId': entry['repoId']}
                        slots.append(slot)
                        template = read(FIXTURES / 'sf-template.json')
                        template['identity'] = {**slot, 'language': 'en'}
                        write(pipeline.work / 'inputs/repo-sf/en' / (slot['repoId'] + '.json'), template)
                    write(pipeline.work / 'inputs/json/en' / (sfs_id + '.json'), area)
                write(pipeline.work / 'inputs/schemas/mask.schema.json', read(FIXTURES / 'mask.schema.json'))
                (pipeline.work / 'inputs/main.tex').write_text('% Synthetic manuscript fixture only\n')
                write(pipeline.work / 'frozen/sources.json', {'sources': [SOURCE], 'cutoffUtc': '2026-01-01T00:00:00Z'})
                write(pipeline.work / 'frozen/concerns.json', [{'id': sfs + '-concern', 'sfsId': sfs,
                    'description': 'Synthetic concern', 'sourceRefs': REFS} for sfs in HEX])
                write(pipeline.work / 'agent-rules/run-config.json', {'synthetic': True})
                pipeline.state = {'runId': 'synthetic-run', 'branch': 'qwen3/261012', 'publish': True,
                    'startedAt': '2026-01-01T00:00:00Z', 'inputCommit': '1' * 40,
                    'outputBaseCommit': base, 'expectedRemote': None, 'controllerCommit': '2' * 40,
                    'inputManifest': file_manifest(pipeline.work / 'inputs'),
                    'frozenManifest': file_manifest(pipeline.work / 'frozen'),
                    'rulesManifest': file_manifest(pipeline.work / 'agent-rules'),
                    'timeoutsMinutes': {key: 5 for key in ('X', 'Y', 'Z', 'REVIEW_MINUTES',
                                                       'SOURCE_MINUTES', 'MASK_MINUTES')},
                    'maximumAttempts': 2, 'slots': slots, 'stages': {}, 'publications': [],
                    'sourceCutoffUtc': '2026-01-01T00:00:00Z', 'openCodeVersion': 'synthetic',
                    'model': {'digest': 'a' * 64, 'effectiveContext': 32768},
                    'baseModes': {'catalog': 'empty', 'masks': 'empty'}, 'priorRelease': None}
                pipeline.save()
                class Server:
                    def __enter__(self):
                        return self
                    def __exit__(self, *_):
                        pass
                    def ask(self, role, prompt, seconds, call_id, **kwargs):
                        # Roster reviews; exact candidate and checks are the final JSON payload.
                        payload = json.loads(prompt[prompt.index('{"roster":'):])
                        return review(payload['roster'], payload['requiredChecks'])
                pipeline.server = lambda _: Server()
                def generate(server, stage, key, role, prompt, minutes, validate, checks, fixtures=None):
                    if stage == 'catalog':
                        value = question(next(s for s in slots if s['repoId'] == key))
                    elif stage == 'solutions':
                        slot = next(s for s in slots if s['repoId'] == key)
                        value = solution(slot, pipeline.question(slot))
                    elif stage == 'extended':
                        slot = next(s for s in slots if s['repoId'] == key)
                        value = sf(slot, pipeline.question(slot))
                    elif stage == 'masks' or key.startswith('masks-'):
                        language = 'en' if stage == 'masks' else stage.split('-')[1]
                        value = masks(catalog_digest(pipeline.work, language), language)
                    elif key.startswith('area-'):
                        area = read(pipeline.work / 'json/en' / (key[-1] + '.json'))
                        value = {k: area[k] + ' synthetic translated' for k in
                                 ('keyTitle', 'keyShortInfo', 'keyDescription', 'keyWords', 'promptAi')}
                        value['meta'] = {k: area['meta'][k] + ' synthetic translated' for k in ('title', 'menuTitle')}
                    else:
                        language = stage.split('-')[1]
                        slot = next(s for s in slots if s['repoId'] == key)
                        value = {'sf': sf(slot, pipeline.question(slot), language),
                            'solution': solution(slot, pipeline.question(slot), language),
                            'contract': pipeline.question(slot)['contract'],
                            'glossary': [{'en': 'Synthetic term', language: 'Synthetic translation'}]}
                    validate(value)
                    write(pipeline.work / 'reviews' / stage / (key + '.json'),
                          {'syntheticTest': True, 'artifactDigest': digest(value)})
                    return value
                pipeline.generate = generate
                pipeline.catalog()
                pipeline.publish('catalog')
                pipeline.extended()
                pipeline.publish('extended')
                pipeline.masks()
                pipeline.publish('masks')
                pipeline.translate('fr')
                pipeline.publish('translate-fr')
                pipeline.translate('el')
                pipeline.publish('translate-el')
                self.assertEqual([p['stage'] for p in pipeline.state['publications']], STAGES)
                self.assertEqual(len({p['commit'] for p in pipeline.state['publications']}), 5)
                self.assertEqual(self.git(bare, 'rev-list', '--count', 'qwen3/261012').strip(), '6')
                for language in ('en', 'fr', 'el'):
                    self.assertEqual(len(list((pipeline.output / 'repo-sf' / language).glob('*.json'))), 256)
                    self.assertEqual(len(list((pipeline.output / 'solutions' / language).glob('*.json'))), 256)
                    for record in (pipeline.output / 'repo-sfs-masks' / language).glob('*.json'):
                        self.assertEqual(read(record)['catalog']['digest'], catalog_digest(pipeline.output, language))
                    if shutil.which('node'):
                        # Execute the existing site's JS canonicalization, including Unicode,
                        # independently of Python rather than reusing its digest implementation.
                        program = '''const fs=require('fs'), p=require('path'), c=require('crypto');
                        const canonical=v=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'
                        ?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
                        const root=process.argv[1], lang=process.argv[2];
                        const read=n=>JSON.parse(fs.readFileSync(p.join(root,n),'utf8'));
                        const sets=[...'0123456789ABCDEF'].map(sfsId=>{
                        const set=read('json/'+lang+'/'+sfsId+'.json');
                        return {sfsId,set,records:set.set.map(x=>read('repo-sf/'+lang+'/'+x.repoId+'.json'))};});
                        process.stdout.write('sha256:'+c.createHash('sha256').update(JSON.stringify(canonical(sets))).digest('hex'));'''
                        js = subprocess.run(['node', '-e', program, str(pipeline.output), language],
                                            check=True, capture_output=True, text=True)
                        self.assertEqual(js.stdout, catalog_digest(pipeline.output, language))
                pointer = read(pipeline.output / 'next-base.json')
                release = pipeline.output / pointer['releasePath']
                self.assertEqual(byte_digest(release / 'release-manifest.json'), pointer['releaseManifestDigest'])
                self.assertFalse(read(pipeline.output / 'DISTILLATION-TODO.json')['trained'])
                self.assertTrue(read(pipeline.output / 'pipeline-status.json')['multilingualComplete'])
                self.assertNotIn('snapshots', [p.name for p in release.iterdir()])
                # A later run must not overwrite immutable paths or accept changed parent bytes.
                changed = pipeline.work / 'repo-sf/en/0000.json'
                changed.write_text('{}\n')
                with self.assertRaisesRegex(ValueError, 'earlier validated milestone changed'):
                    pipeline.check_previous('translate-el')

    def git(self, root, *args):
        result = subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True)
        return result.stdout


if __name__ == '__main__':
    unittest.main()
