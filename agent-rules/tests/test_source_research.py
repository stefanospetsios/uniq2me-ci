"""Live admission control with synthetic HTTP/MCP evidence; no model or pushes."""
import copy
import io
import json
import os
import sys
import tempfile
import time
from urllib.parse import quote
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import read, write
from ci.pipeline import check_source_passages, fetch_source_snapshot, official_url
from ci.smoke_test import create_fixture_pipeline
from ci.runtime import ModelAnswerError, OpenCode, parse_json_answer
from ci.source_research import SourceAdmissionError, research_scope, scope_queries, url_identity

URLS = ['https://www.cisa.gov/synthetic-endpoints',
        'https://www.nist.gov/synthetic-workstations']
PASSAGES = ['Synthetic endpoint patching evidence for admission control tests.',
            'Synthetic workstation encryption evidence for admission control tests.']
GROUPS = {'searxng': ['searxng_search'], 'websearch': ['websearch_search']}


def metadata(url):
    return {'id': 'wrong-model-invented-scope-id', 'type': 'technical-doc',
            'title': 'Synthetic test document', 'publisher': 'Synthetic official publisher',
            'url': url, 'language': 'en', 'jurisdiction': 'Synthetic test scope',
            'applicability': 'Synthetic endpoints', 'publicationDate': None,
            'effectiveDate': None, 'redistribution': 'metadata-and-short-passages'}


class Server:
    def __init__(self, archive):
        self.archive, self.calls = archive, []
        self.discovery = {'sources': [metadata(u) for u in URLS],
                          'limitations': ['Brave: too many requests; other results were available.']}
        self.evidence = {'sources': [{'id': f'0-source-{i}',
            'passages': [{'id': 'p1', 'locator': 'Synthetic heading', 'text': p}]}
            for i, p in enumerate(PASSAGES, 1)], 'concerns': [{'id': '0-concern-1', 'sfsId': '0',
            'description': 'Synthetic endpoint patching evidence',
            'sourceRefs': [{'sourceId': '0-source-1', 'passageId': 'p1'}]}],
            'limitations': ['Publication effective dates not stated; no effective-date claim is made.'],
            'blockingGaps': []}
        self.outputs = [json.dumps({'results': [{'url': u} for u in URLS]}), 'Synthetic search completed']
        self.extra_parts = []
        self.evidence_responses = []

    def ask(self, role, prompt, seconds, call_id, **kwargs):
        self.calls.append({'prompt': prompt, 'seconds': seconds, 'callId': call_id, **kwargs})
        if call_id.endswith('-discovery'):
            parts = [{'type': 'tool', 'tool': group[0], 'state': {'status': 'completed',
                'input': {'query': 'Synthetic search'}, 'output': output}}
                for group, output in zip(GROUPS.values(), self.outputs)] + self.extra_parts
            write(self.archive / 'calls' / call_id / 'messages.json', [{'parts': parts}])
            candidate = copy.deepcopy(self.discovery)
            if kwargs.get('answer_validator'):
                candidate = kwargs['answer_validator'](candidate, [{'parts': parts}])
            return candidate
        return copy.deepcopy(self.evidence_responses.pop(0) if self.evidence_responses else self.evidence)


class SourceResearchTests(unittest.TestCase):
    def test_scope_retry_includes_actual_candidates_and_preserves_previous_failures(self):
        self.coordinator_retry('0')

    def test_legal_scope_retry_requests_core_laws_instead_of_topic_documents(self):
        self.coordinator_retry('legal')

    def test_coverage_retry_preserves_evidence_findings_and_requests_supplemental_sources(self):
        self.coordinator_retry('0', coverage=True)

    def coordinator_retry(self, rejected_scope, coverage=False):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
             patch.dict(os.environ, {'RAG_MODE': 'repo-web'}):
            pipeline, _ = create_fixture_pipeline(directory, publish=False)
            pipeline.state.update(dryRun=False, ragMode='repo-web', runnerPreflight={'status': 'passed'})
            pipeline.ensure_live_model = Mock()
            server = Mock()
            server.__enter__ = Mock(return_value=server)
            server.__exit__ = Mock(return_value=False)
            server.redact.side_effect = lambda value: value
            server.discover.return_value = {'allowedTools': [GROUPS['searxng'][0], GROUPS['websearch'][0]],
                'openCodeVersion': 'synthetic'}
            pipeline.server = Mock(return_value=server)
            write(pipeline.root / 'source-rejections.json', [{'reason': 'Earlier synthetic failed attempt'}])
            feedback = {'returnedCandidates': [metadata(URLS[0])],
                'observedResultUrls': URLS, 'suggestedQueries': ['Synthetic query']}
            if coverage:
                feedback.update(phase='coverage', blockingGaps=['Synthetic missing device encryption support'],
                                evidenceCandidate={'blockingGaps': ['Synthetic missing device encryption support']})
                pipeline.state['maximumAttempts'] = 3
            repairs = []
            failures = []
            def research(server, scope, *args, repair='', **kwargs):
                if scope != rejected_scope:
                    return {'sources': {}, 'concerns': [], 'queries': [], 'limitations': []}
                self.assertEqual(kwargs['evidence_attempts'], pipeline.state['maximumAttempts'])
                self.assertEqual(kwargs['coverage_gaps'], feedback['blockingGaps'] if coverage and repairs else [])
                repairs.append(repair)
                details = feedback if not coverage or len(repairs) == 1 else {
                    'phase': 'evidence', 'evidenceCandidate': {}, 'observedResultUrls': URLS}
                failures.append(details)
                raise SourceAdmissionError('Synthetic missing required support', details)
            with patch('ci.source_research.research_scope', side_effect=research), \
                 patch.object(pipeline,'freeze_source_corpus') as freeze:
                pipeline.execute('prepare')
            freeze.assert_called_once()
            self.assertEqual(pipeline.state['pendingSourceScopes'][rejected_scope]['status'], 'pending-source-research')
            self.assertFalse(pipeline.accepted_release())
            self.assertEqual(len(repairs), pipeline.state['maximumAttempts'])
            self.assertIn(URLS[0], repairs[1])
            self.assertIn('returnedCandidates', repairs[1])
            self.assertIn('Synthetic query', repairs[1])
            if rejected_scope == 'legal':
                self.assertIn('observed full EU NIS2 directive and Greek implementing law', repairs[1])
                self.assertNotIn('Return two observed topic documents', repairs[1])
            else:
                self.assertIn('actual implementation documentation', repairs[1])
            self.assertIn('retain relevant earlier documents', repairs[1])
            self.assertNotIn('omit the rejected URLs', repairs[1])
            if coverage:
                self.assertIn('evidenceCandidate', repairs[1])
                self.assertIn('Synthetic missing device encryption support official implementation', repairs[1])
                self.assertIn('Synthetic missing device encryption support official implementation', repairs[2])
            history = read(pipeline.root / 'source-rejections.json')
            self.assertEqual(len(history), pipeline.state['maximumAttempts'] + 1)
            self.assertEqual(history[0]['reason'], 'Earlier synthetic failed attempt')
            self.assertEqual(history[-1]['details'], failures[-1])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / 'work'
        self.server = Server(Path(self.temp.name) / 'runtime')
        self.contents = {u: p for u, p in zip(URLS, PASSAGES)}
        self.retrieval_errors = {}
        def close_errors():
            for error in self.retrieval_errors.values():
                error.close()
        self.addCleanup(close_errors)
        self.content_types = {}
        self.fetches = []

    def response(self, request, **kwargs):
        self.fetches.append(request.full_url)
        if request.full_url in self.retrieval_errors:
            raise self.retrieval_errors[request.full_url]
        headers = Message()
        headers['Content-Type'] = self.content_types.get(request.full_url, 'text/html; charset=utf-8')
        body = self.contents[request.full_url]
        body = body.encode() if isinstance(body, str) else body
        if headers['Content-Type'].startswith('text/html'):
            body = b'<html><p>' + body + b'</p></html>'
        class Response:
            def __enter__(r):
                r.headers, r.url = headers, request.full_url
                return r
            def __exit__(r, *_):
                pass
            def read(r, _):
                return body
        return Response()

    def run_scope(self, scope='0', deadline=None, evidence_attempts=1, repair='', coverage_gaps=None):
        with patch('ci.pipeline.urllib.request.build_opener') as opener, redirect_stdout(io.StringIO()):
            opener.return_value.open.side_effect = self.response
            return research_scope(self.server, scope,
                {'keyTitle': 'Operating System and Workstation Environment',
                 'keyDescription': 'Synthetic topic boundaries', 'set': ['not-needed-assessments']},
                GROUPS, ['cisa.gov', 'nist.gov'], 'Synthetic controller rules', self.work,
                time.monotonic() + 900 if deadline is None else deadline, f'sources-{scope}-1', {}, fetch_source_snapshot,
                check_source_passages, evidence_attempts=evidence_attempts, repair=repair, coverage_gaps=coverage_gaps)

    def install_opencode_protocol(self, replies, advance=None, evidence_replies=None):
        """Exercise the actual session/JSON/selection correction path, with no service calls."""
        fixture = self.server
        server = OpenCode(self.work, ROOT, {}, fixture.archive,
                          {'digest':'a'*64, 'effectiveContext':32768})
        sessions, submissions, messages = [], [], {}
        def api(path, data=None, **kwargs):
            if path == '/session':
                session = 'synthetic-' + str(len(sessions))
                sessions.append((session, data['title']))
                messages[session] = []
                return {'id':session}
            if path == '/session/status':
                return {}
            session = path.split('/')[2]
            if path.endswith('/prompt_async'):
                submissions.append(data)
                discovery = sessions[int(session.split('-')[-1])][1].endswith('-discovery')
                value = replies.pop(0) if discovery else (evidence_replies.pop(0) if evidence_replies else fixture.evidence)
                parts = [{'type':'text','text':value if isinstance(value,str) else json.dumps(value)}]
                if discovery and not messages[session]:
                    parts += [{'type':'tool','tool':group[0],'state':{'status':'completed',
                        'input':{'query':'Synthetic endpoints'},'output':output}}
                        for group,output in zip(GROUPS.values(),fixture.outputs)]
                messages[session].append({'info':{'role':'assistant','providerID':'ollama',
                    'modelID':'qwen3:235b','time':{'completed':1},'finish':'stop',
                    'tokens':{'total':10}},'parts':parts})
            if path.endswith('/message'):
                return messages[session]
            return None
        server.api, server.check_residency = api, advance or (lambda: None)
        self.server = server
        return sessions, submissions

    def test_malformed_evidence_json_repairs_against_the_same_snapshots(self):
        discovery=copy.deepcopy(self.server.discovery)
        sessions,posts=self.install_opencode_protocol([discovery],evidence_replies=['{"sources":[{"id":"unfinished'])
        admitted=self.run_scope(evidence_attempts=2)
        self.assertEqual(self.fetches,URLS)
        self.assertEqual(len(sessions),3)
        self.assertEqual(len(posts),3)
        self.assertEqual(len(admitted['sources']),2)
        failure=read(self.work/'discovery-candidates/sources-0-1-evidence-rejection.json')
        self.assertIsNone(failure['evidenceCandidate'])
        self.assertEqual(failure['evidenceFailures'][0]['ruleId'],'invalid-model-json')
        envelope=read(self.server.archive/'calls/sources-0-1-evidence/envelope.json')
        self.assertEqual(envelope['status'],'failed')
        transcript=read(self.server.archive/'calls/sources-0-1-evidence/messages.json')
        self.assertIn('unfinished',transcript[-1]['parts'][0]['text'])

    def test_exhausted_malformed_evidence_json_is_a_bounded_source_admission_failure(self):
        discovery=copy.deepcopy(self.server.discovery)
        sessions,posts=self.install_opencode_protocol([discovery],evidence_replies=['{"broken":','{"broken":'])
        with self.assertRaisesRegex(SourceAdmissionError,'Expecting value') as caught:
            self.run_scope(evidence_attempts=2)
        self.assertEqual(len(posts),3)
        self.assertEqual(self.fetches,URLS)
        failures=caught.exception.details['evidenceFailures']
        self.assertEqual(len(failures),2)
        self.assertTrue(all(f['ruleId']=='invalid-model-json' and f['candidate'] is None for f in failures))
        self.assertIn('same fetched snapshots',posts[-1]['parts'][0]['text'])

    def test_bad_quotation_then_malformed_final_repair_preserves_both_failures(self):
        discovery=copy.deepcopy(self.server.discovery)
        invalid=copy.deepcopy(self.server.evidence)
        invalid['sources'][0]['passages'][0]['text']='Synthetic fabricated quotation absent from the fetched document.'
        self.install_opencode_protocol([discovery],evidence_replies=[invalid,'{"text":"unfinished'])
        with self.assertRaisesRegex(SourceAdmissionError,'Unterminated string') as caught:
            self.run_scope(evidence_attempts=2)
        failures=caught.exception.details['evidenceFailures']
        self.assertEqual(len(failures),2)
        self.assertEqual(failures[0]['passageFailures'][0]['ruleId'],'quotation-not-found')
        self.assertEqual(failures[1]['ruleId'],'invalid-model-json')

    def test_exhausted_malformed_discovery_json_preserves_observations_and_failure_type(self):
        _,posts=self.install_opencode_protocol(['{"broken":']*3)
        with self.assertRaisesRegex(SourceAdmissionError,'Invalid discovery response JSON') as caught:
            self.run_scope(evidence_attempts=2)
        self.assertEqual(len(posts),3)
        self.assertEqual(self.fetches,[])
        self.assertEqual(caught.exception.details['ruleId'],'invalid-model-json')
        from ci.source_observations import history
        observed,_=history(self.work,'0',GROUPS)
        self.assertEqual(observed,set(URLS))

    def test_missing_search_calls_after_one_followup_remain_unadmitted_source_research(self):
        discovery=copy.deepcopy(self.server.discovery)
        sessions,posts=self.install_opencode_protocol([discovery,discovery])
        original=self.server.api
        def api(path,*args,**kwargs):
            result=original(path,*args,**kwargs)
            if path.endswith('/message'):
                for message in result:
                    message['parts']=[p for p in message['parts'] if p.get('tool') not in GROUPS['websearch']]
            return result
        self.server.api=api
        with self.assertRaisesRegex(SourceAdmissionError,'lacks completed calls for: websearch') as caught:
            self.run_scope(evidence_attempts=2)
        self.assertEqual(len(sessions),1)
        self.assertEqual(len(posts),2)
        self.assertEqual(self.fetches,[])
        self.assertEqual(caught.exception.details['ruleId'],'missing-required-search-calls')
        self.assertEqual(caught.exception.details['missingToolGroups'],['websearch'])
        envelope=read(self.server.archive/'calls/sources-0-1-discovery/envelope.json')
        self.assertEqual(envelope['toolContinuations'],1)
        self.assertEqual(envelope['status'],'failed')

    def test_transport_json_and_model_identity_errors_are_not_evidence_repairs(self):
        for error in (json.JSONDecodeError('Malformed API response','{',1),
                      ValueError('OpenCode response model differs from the pinned authoring model')):
            with self.subTest(error=type(error).__name__):
                original=self.server.ask
                def ask(*args,**kwargs):
                    if not args[3].endswith('-discovery'):
                        raise error
                    return original(*args,**kwargs)
                self.server.ask=ask
                try:
                    with self.assertRaises(type(error)) as caught:
                        self.run_scope(evidence_attempts=2)
                    self.assertIs(caught.exception,error)
                finally:
                    self.server.ask=original

    def test_duplicate_json_then_unobserved_url_are_corrected_in_one_discovery_session(self):
        guessed = 'https://www.nist.gov/invented'
        sessions, posts = self.install_opencode_protocol([
            '{"publisher":"one","publisher":"two"}',
            {'sources':[metadata(URLS[0]),metadata(guessed)],'limitations':['Synthetic search warning']},
            {'sources':[metadata(URLS[1])],'limitations':[]}])
        admitted = self.run_scope(evidence_attempts=2)
        self.assertEqual(self.fetches, URLS)
        self.assertEqual(len(admitted['sources']),2)
        self.assertEqual(len(sessions),2)  # One discovery session and one evidence session.
        self.assertEqual(len(posts),4)
        self.assertIn('Duplicate output key: publisher',posts[1]['parts'][0]['text'])
        self.assertIn('eligibleCandidates',posts[2]['parts'][0]['text'])
        self.assertIn(URLS[1],posts[2]['parts'][0]['text'])
        self.assertTrue(any('not observed' in w['description'] for w in admitted['limitations']))
        self.assertTrue(any('Synthetic search warning' in w['description'] for w in admitted['limitations']))
        archive = self.server.archive/'calls/sources-0-1-discovery'
        envelope = read(archive/'envelope.json')
        self.assertEqual(envelope['answerRejections'],2)
        self.assertEqual(envelope['reportedTokenUsage']['total'],30)
        self.assertFalse((archive/'answer-rejection-3.json').exists())
        self.assertIn('publisher',read(archive/'answer-rejection-1.json')['answer'])
        self.assertEqual(len(read(archive/'answer-rejection-2.json')['details']['eligibleCandidates']),1)

    def test_discovery_corrections_remain_bounded_and_do_not_fetch_unobserved_urls(self):
        invalid = {'sources':[metadata(URLS[0]),metadata('https://www.nist.gov/invented')],
                   'limitations':[]}
        sessions, posts = self.install_opencode_protocol([invalid]*3)
        with self.assertRaisesRegex(SourceAdmissionError,'not observed') as failure:
            self.run_scope(evidence_attempts=2)
        self.assertEqual(len(posts),3)
        self.assertEqual(len(sessions),1)
        self.assertEqual(self.fetches,[])
        self.assertEqual(len(failure.exception.details['eligibleCandidates']),1)
        envelope = read(self.server.archive/'calls/sources-0-1-discovery/envelope.json')
        self.assertEqual(envelope['status'],'failed')
        self.assertEqual(envelope['answerRejections'],3)

    def test_discovery_correction_cannot_extend_original_deadline(self):
        clock = [0]
        def advance():
            clock[0] += 40
        _,posts = self.install_opencode_protocol(['{"publisher":1,"publisher":2}'],advance)
        with patch('ci.runtime.time.monotonic',side_effect=lambda:clock[0]), \
             self.assertRaisesRegex(ValueError,'Duplicate output key'):
            self.run_scope(deadline=30,evidence_attempts=2)
        self.assertEqual(len(posts),1)
        self.assertEqual(self.fetches,[])

    def test_json_correction_is_opt_in_for_other_calls(self):
        _,posts = self.install_opencode_protocol(['{"publisher":1,"publisher":2}'])
        with redirect_stdout(io.StringIO()),self.assertRaisesRegex(ValueError,'Duplicate output key'):
            self.server.ask('source-researcher','Synthetic strict JSON',30,'strict-discovery')
        self.assertEqual(len(posts),1)
        envelope = read(self.server.archive/'calls/strict-discovery/envelope.json')
        self.assertEqual(envelope['answerRepairLimit'],0)
        self.assertEqual(envelope['status'],'failed')

    def test_model_mismatch_cannot_be_repaired_as_a_source_selection_error(self):
        _,posts = self.install_opencode_protocol([{'sources':[],'limitations':[]}])
        self.server.model_tag = 'different-pinned-model'
        with self.assertRaisesRegex(ValueError,'differs from the pinned authoring model'):
            self.run_scope(evidence_attempts=2)
        self.assertEqual(len(posts),1)
        self.assertEqual(self.fetches,[])
        envelope = read(self.server.archive/'calls/sources-0-1-discovery/envelope.json')
        self.assertEqual(envelope['answerRejections'],0)

    def test_unlimited_source_budget_covers_both_phases_without_json_infinity(self):
        admitted = self.run_scope(deadline=float('inf'))
        self.assertEqual(len(admitted['sources']), 2)
        self.assertEqual([call['seconds'] for call in self.server.calls], [None, None])

    def test_optional_invented_extra_url_does_not_discard_two_verified_documents(self):
        guessed = 'https://www.cisa.gov/synthetic-invented-extra'
        self.server.discovery['sources'].append(metadata(guessed))
        admitted = self.run_scope()
        self.assertEqual(len(admitted['sources']), 2)
        self.assertEqual(self.fetches, URLS)
        self.assertTrue(any(guessed in row['description'] for row in admitted['limitations']))
        selected = read(self.work / 'discovery-candidates/sources-0-1-selection.json')
        self.assertEqual(selected['rejectedCandidates'][0]['url'], guessed)
        original = read(self.work / 'discovery-candidates/sources-0-1-discovery.json')
        self.assertEqual(len(original['discovery']['sources']), 3)

    def unavailable_candidates(self):
        unavailable = 'https://www.cisa.gov/synthetic-template.docx'
        outside = 'http://outside.example.invalid/synthetic-manual'
        self.server.discovery['sources'] = [metadata(unavailable), *self.server.discovery['sources'], metadata(outside)]
        self.server.outputs[0] = json.dumps({'results': [{'url': u} for u in [unavailable, *URLS, outside]]})
        self.retrieval_errors[unavailable] = HTTPError(unavailable, 406, 'Not Acceptable', {}, None)
        for index, source in enumerate(self.server.evidence['sources'], 2):
            source['id'] = f'0-source-{index}'
        self.server.evidence['concerns'][0]['sourceRefs'][0]['sourceId'] = '0-source-2'
        return unavailable, outside

    def test_optional_406_and_invalid_source_url_leave_usable_subset_for_evidence_review(self):
        unavailable, outside = self.unavailable_candidates()
        admitted = self.run_scope()
        self.assertEqual(set(admitted['sources']), {'0-source-2', '0-source-3'})
        self.assertEqual(self.fetches, [unavailable, *URLS])  # Invalid URLs never reach HTTP.
        self.assertTrue(all(source['sourceStatus'] == 'fetched-and-passage-checked'
                            for source in admitted['sources'].values()))
        for url in (unavailable, outside):
            self.assertTrue(any(url in item['description'] for item in admitted['limitations']))
        record = read(self.work / 'discovery-candidates/sources-0-1-retrieval.json')
        self.assertEqual({row['sourceId'] for row in record['retrievalFailures']}, {'0-source-1', '0-source-4'})
        self.assertEqual(len(record['snapshots']), 2)
        self.assertEqual(record['discovery'], self.server.discovery)
        prompt = self.server.calls[1]['prompt']
        example, _ = json.JSONDecoder().raw_decode(prompt.split(
            'must appear either in sources or in excludedSources, with its ID unchanged: ', 1)[1])
        self.assertEqual({row['id'] for row in example['sources']}, set(admitted['sources']))
        self.assertEqual(example['concerns'][0]['sourceRefs'][0]['sourceId'], '0-source-2')

    def test_observed_documentation_from_any_https_host_can_pass_all_admission_checks(self):
        urls = [
            'https://docs.typo3.org/permalink/t3coreapi:privileges@main',
            'https://gerrit.googlesource.com/gerrit/+/HEAD/Documentation/config-accounts.txt',
            'https://docs.drone.io/server/user/admin/',
            'https://pgbackrest.org/user-guide.html',
            'https://huggingface.co/other-maintainer/project/blob/main/README.md',
        ]
        for url in urls:
            with self.subTest(url=url):
                self.contents[url] = PASSAGES[0]
                self.server.discovery['sources'][0] = metadata(url)
                self.server.outputs[0] = json.dumps({'results': [{'url': u} for u in [url, URLS[1]]]})
                admitted = self.run_scope()
                self.assertEqual(admitted['sources']['0-source-1']['url'], url)
                self.assertEqual(admitted['sources']['0-source-1']['sourceStatus'],
                                 'fetched-and-passage-checked')
                self.assertIn(url, self.fetches)
        self.assertIn('examples, not an allowlist', self.server.calls[0]['prompt'])

    def test_unrestricted_hosts_do_not_allow_non_https_missing_hosts_or_credentials(self):
        for url in ['http://docs.typo3.org/manual', 'file:///tmp/manual',
                    'https:///manual', 'https://user:password@docs.typo3.org/manual']:
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'public HTTPS'):
                official_url(url)

    def test_source_redirects_accept_new_documentation_hosts_but_recheck_url_transport(self):
        from urllib.request import Request
        with patch('ci.pipeline.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = self.response
            fetch_source_snapshot(metadata(URLS[0]), self.work / 'redirect-snapshots', time.monotonic() + 60)
        handler = opener.call_args.args[0]
        request = Request(URLS[0])
        redirected = handler.redirect_request(request, None, 302, 'Found', {},
                                              'https://docs.drone.io/server/user/admin/')
        self.assertEqual(redirected.full_url, 'https://docs.drone.io/server/user/admin/')
        eli = handler.redirect_request(request, None, 302, 'Found', {},
                                       'http://data.europa.eu/eli/reg/2022/2554/oj')
        self.assertEqual(eli.full_url, 'https://data.europa.eu/eli/reg/2022/2554/oj')
        for target in ['http://docs.drone.io/manual', 'https://user:password@docs.drone.io/manual']:
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'public HTTPS'):
                handler.redirect_request(request, None, 302, 'Found', {}, target)

    def test_optional_binary_office_document_is_excluded_without_admitting_garbled_text(self):
        url = 'https://www.cisa.gov/synthetic-template.docx'
        self.server.discovery['sources'].append(metadata(url))
        self.server.outputs[0] = json.dumps({'results': [{'url': u} for u in [*URLS, url]]})
        self.contents[url] = b'PK\x03\x04Synthetic zipped Office document, not source text'
        self.content_types[url] = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        admitted = self.run_scope()
        self.assertEqual(set(admitted['sources']), {'0-source-1', '0-source-2'})
        self.assertTrue(any('Unsupported source document format' in item['description'] for item in admitted['limitations']))
        record = read(self.work / 'discovery-candidates/sources-0-1-retrieval.json')
        self.assertEqual(record['retrievalFailures'][0]['url'], url)

    def test_unavailable_document_still_blocks_when_only_one_snapshot_survives(self):
        self.retrieval_errors[URLS[0]] = HTTPError(URLS[0], 406, 'Not Acceptable', {}, None)
        with self.assertRaisesRegex(SourceAdmissionError, 'two independently fetched documents') as failure:
            self.run_scope()
        self.assertEqual(len(self.server.calls), 1)
        details = failure.exception.details
        self.assertEqual(details['phase'], 'retrieval')
        self.assertEqual(details['retrievalFailures'][0]['url'], URLS[0])
        self.assertEqual([row['id'] for row in details['snapshots']], ['0-source-2'])

    def test_surviving_guidance_cannot_replace_failed_technical_implementation_document(self):
        unavailable, _ = self.unavailable_candidates()
        for source in self.server.discovery['sources'][1:3]:
            source['type'] = 'guidance'
        with self.assertRaisesRegex(SourceAdmissionError, 'technical implementation documentation') as failure:
            self.run_scope()
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(failure.exception.details['retrievalFailures'][0]['url'], unavailable)

    def test_unavailable_core_law_blocks_even_with_two_surviving_documents(self):
        directive = 'https://eur-lex.europa.eu/eli/dir/2022/2555/oj/eng'
        source = metadata(directive)
        source['type'] = 'law'
        self.server.discovery['sources'].append(source)
        self.server.outputs[0] = json.dumps({'results': [{'url': u} for u in [*URLS, directive]]})
        self.retrieval_errors[directive] = HTTPError(directive, 404, 'Not Found', {}, None)
        with self.assertRaisesRegex(SourceAdmissionError, 'Missing fetched core legal instrument: EU Directive') as failure:
            self.run_scope('legal')
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(len(failure.exception.details['snapshots']), 2)
        self.assertEqual(failure.exception.details['retrievalFailures'][0]['url'], directive)

    def test_excluded_retrieval_candidate_still_blocks_when_coverage_is_missing(self):
        self.unavailable_candidates()
        self.server.evidence['blockingGaps'] = ['Required recovery test evidence is absent from the surviving documents.']
        with self.assertRaisesRegex(SourceAdmissionError, 'Required recovery test evidence') as failure:
            self.run_scope()
        self.assertEqual(failure.exception.details['phase'], 'coverage')
        self.assertEqual(len(failure.exception.details['retrievalFailures']), 2)

    def test_concerns_cannot_cite_excluded_retrieval_candidates(self):
        self.unavailable_candidates()
        self.server.evidence['concerns'][0]['sourceRefs'][0]['sourceId'] = '0-source-1'
        with self.assertRaisesRegex(SourceAdmissionError, 'Unknown frozen source: 0-source-1'):
            self.run_scope()

    def test_topic_laws_are_excluded_with_concrete_candidate_and_query_feedback(self):
        for source in self.server.discovery['sources']:
            source['type'] = 'law'
        with self.assertRaises(SourceAdmissionError) as failure:
            self.run_scope()
        self.assertEqual(self.fetches, [])
        self.assertEqual(len(failure.exception.details['returnedCandidates']), 2)
        self.assertEqual(len(failure.exception.details['rejectedCandidates']), 2)
        self.assertTrue(any('Operating System' in q for q in failure.exception.details['suggestedQueries']))
        self.assertTrue((self.work / 'discovery-candidates/sources-0-1-rejections.json').exists())

    def test_statute_url_cannot_be_mislabeled_technical_document(self):
        self.server.discovery['sources'][0]['url'] = 'https://eur-lex.europa.eu/eli/dir/2022/2555/oj/eng'
        with self.assertRaisesRegex(SourceAdmissionError, 'Statutory document URL cannot be relabeled'):
            self.run_scope()
        self.assertEqual(self.fetches, [])

    def test_topic_and_legal_prompts_have_distinct_search_instructions(self):
        self.run_scope()
        prompt = self.server.calls[0]['prompt']
        self.assertIn('TECHNICAL TOPIC SCOPE', prompt)
        self.assertIn('legal seeds are ONLY for the separate legal scope', prompt)
        self.assertIn('technical-doc|guidance', prompt)
        queries = scope_queries('0', {'keyTitle': 'Endpoints', 'keyWords': 'patching, disk encryption'})
        self.assertTrue(any('patching' in q for q in queries))
        self.assertTrue(any('disk encryption' in q for q in queries))
        self.assertFalse(any('NIS2' in q for q in queries))
        self.assertTrue(any('5160' in q for q in scope_queries('legal', {})))

    def test_scope_query_suggestions_include_keywords_beyond_the_first_six(self):
        keywords = ['operating systems', 'workstations', 'endpoint inventory', 'provisioning',
                    'patching', 'diskless clients', 'remote desktops', 'shared storage',
                    'device encryption', 'endpoint logging']
        queries = scope_queries('0', {'keyTitle': 'Endpoints', 'keyWords': ', '.join(keywords)})
        for keyword in keywords:
            self.assertIn(keyword + ' official maintainer implementation configuration documentation', queries)

    def test_quotation_repair_uses_same_snapshots_and_preserves_failed_candidate(self):
        invalid = copy.deepcopy(self.server.evidence)
        invalid['sources'][0]['passages'].append({'id': 'p2', 'locator': 'Synthetic heading',
            'text': 'Fabricated quotation absent from the supplied snapshot.'})
        self.server.evidence_responses = [invalid]
        gap = 'Synthetic prior encryption coverage finding'
        admitted = self.run_scope(evidence_attempts=2, repair='Previous DISCOVERY failure: search again',
                                  coverage_gaps=[gap])
        self.assertEqual(self.fetches, URLS)
        self.assertEqual(len(self.server.calls), 3)
        self.assertEqual(self.server.calls[2]['callId'], 'sources-0-1-evidence-repair-2')
        prompt = self.server.calls[2]['prompt']
        self.assertIn('0-source-1/p2', prompt)
        self.assertIn(json.dumps(invalid, ensure_ascii=False), prompt)
        self.assertIn(PASSAGES[0], prompt)
        self.assertIn('Do not perform discovery', prompt)
        self.assertNotIn('Previous DISCOVERY failure', prompt)
        self.assertTrue(all(gap in call['prompt'] for call in self.server.calls))
        self.assertEqual(admitted['sources']['0-source-1']['passages'], self.server.evidence['sources'][0]['passages'])
        failed = read(self.work / 'discovery-candidates/sources-0-1-evidence-rejection.json')
        self.assertEqual(failed['evidenceCandidate'], invalid)
        self.assertEqual(failed['observedResultUrls'], sorted(URLS))
        self.assertEqual(len(failed['snapshots']), 2)

    def test_short_heading_repair_requests_substantive_text_and_reuses_fetched_snapshots(self):
        self.contents[URLS[0]] = 'Article 2: Scope\n' + PASSAGES[0]
        invalid = copy.deepcopy(self.server.evidence)
        invalid['sources'][0]['passages'][0]['text'] = 'Article 2: Scope'
        self.server.evidence_responses = [invalid]
        admitted = self.run_scope(evidence_attempts=2)
        self.assertEqual(self.fetches, URLS)
        self.assertEqual(len(self.server.calls), 3)
        self.assertEqual(admitted['sources']['0-source-1']['passages'][0]['text'], PASSAGES[0])
        failed = read(self.work / 'discovery-candidates/sources-0-1-evidence-rejection.json')
        error = failed['evidenceFailures'][0]['passageFailures'][0]
        self.assertEqual(error['ruleId'], 'quotation-too-short')
        self.assertEqual(error['normalizedCharacters'], 16)
        self.assertEqual(error['minimumCharacters'], 20)
        for call in self.server.calls[1:]:
            self.assertIn('at least 20 characters after Unicode and whitespace normalization', call['prompt'])
            self.assertIn('Adding spaces or blank lines does not count', call['prompt'])
            self.assertIn('not just an article title', call['prompt'])
        self.assertIn('quotation-too-short', self.server.calls[-1]['prompt'])
        self.assertIn('Do not perform discovery', self.server.calls[-1]['prompt'])

    def test_repeated_short_heading_with_blank_lines_still_exhausts_evidence_repairs(self):
        self.contents[URLS[0]] = 'Article 2: Scope\n' + PASSAGES[0]
        invalid = copy.deepcopy(self.server.evidence)
        invalid['sources'][0]['passages'][0]['text'] = 'Article 2: Scope'
        padded = copy.deepcopy(invalid)
        padded['sources'][0]['passages'][0]['text'] += '\n\n'
        self.server.evidence_responses = [invalid, padded]
        with self.assertRaisesRegex(SourceAdmissionError, 'too short.*16 characters; minimum 20') as failure:
            self.run_scope(evidence_attempts=2)
        self.assertEqual(self.fetches, URLS)
        failures = failure.exception.details['evidenceFailures']
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(f['passageFailures'][0]['ruleId'] == 'quotation-too-short' for f in failures))
        self.assertTrue(all(f['passageFailures'][0]['normalizedCharacters'] == 16 for f in failures))

    def test_one_repair_reports_mismatches_from_every_source_with_reading_hints(self):
        invalid = copy.deepcopy(self.server.evidence)
        for index, selected in enumerate(invalid['sources']):
            selected['passages'][0]['text'] = PASSAGES[index].replace('Synthetic', 'Fabricated')
        self.server.evidence_responses = [invalid]
        admitted = self.run_scope(evidence_attempts=2)
        self.assertEqual(len(self.server.calls), 3)
        self.assertEqual(self.fetches, URLS)
        self.assertEqual(len(admitted['sources']), 2)
        failed = read(self.work / 'discovery-candidates/sources-0-1-evidence-rejection.json')
        errors = failed['evidenceFailures'][0]['passageFailures']
        self.assertEqual({row['sourceId'] for row in errors}, {'0-source-1', '0-source-2'})
        self.assertTrue(all(Path(row['localTextPath']).is_file() for row in errors))
        prompt = self.server.calls[-1]['prompt']
        self.assertIn('0-source-1/p1', prompt)
        self.assertIn('0-source-2/p1', prompt)
        self.assertIn('Repair ALL listed passageFailures', prompt)
        self.assertTrue(all(row['sourceExcerpt'] in self.contents[URLS[index]]
                            for index, row in enumerate(errors)))

    def test_misattributed_quotation_reports_the_actual_fetched_source_without_accepting_it(self):
        invalid=copy.deepcopy(self.server.evidence)
        invalid['sources'][0]['passages'][0]['text']=PASSAGES[1]
        self.server.evidence_responses=[invalid]
        admitted=self.run_scope(evidence_attempts=2)
        failure=read(self.work/'discovery-candidates/sources-0-1-evidence-rejection.json')
        matches=failure['evidenceFailures'][0]['passageFailures'][0]['otherSourceMatches']
        self.assertEqual([m['sourceId'] for m in matches],['0-source-2'])
        self.assertEqual(admitted['sources']['0-source-1']['passages'][0]['text'],PASSAGES[0])
        self.assertIn('attribute it to the correct source',self.server.calls[-1]['prompt'])

    def test_outer_discovery_retry_retains_verified_observations_for_the_same_scope(self):
        self.server.evidence['sources'][0]['passages'][0]['text']='Synthetic fabricated quotation absent from either document.'
        with self.assertRaises(SourceAdmissionError):
            self.run_scope()
        self.server.evidence['sources'][0]['passages'][0]['text']=PASSAGES[0]
        self.server.outputs=['No new results','No new results']
        admitted=self.run_scope()
        self.assertEqual(set(admitted['observedResultUrls']),set(URLS))
        self.assertTrue(any(q.get('reusedScopeObservation') for q in admitted['queries']))
        self.assertIn('CI-retained URLs observed earlier',self.server.calls[-2]['prompt'])

    def test_optional_source_exclusion_requires_surviving_quota_and_valid_concern_refs(self):
        optional='https://example.invalid/optional-index'
        self.server.discovery['sources'].append(metadata(optional))
        self.server.outputs[0]=json.dumps({'results':[{'url':u} for u in [*URLS,optional]]})
        self.contents[optional]='Synthetic portal index with no normative requirement text.'
        self.server.evidence['excludedSources']=[{'sourceId':'0-source-3','reason':'Redundant index; both substantive documents cover the required concerns.'}]
        admitted=self.run_scope()
        self.assertEqual(set(admitted['sources']),{'0-source-1','0-source-2'})
        self.assertTrue(any('Excluded optional fetched source 0-source-3' in r['description'] for r in admitted['limitations']))
        self.server.evidence['concerns'][0]['sourceRefs']=[{'sourceId':'0-source-3','passageId':'p1'}]
        with self.assertRaisesRegex(SourceAdmissionError,'Unknown frozen source: 0-source-3'):
            self.run_scope()

    def test_excluding_a_required_document_cannot_bypass_the_two_document_gate(self):
        self.server.evidence['sources']=self.server.evidence['sources'][:1]
        self.server.evidence['excludedSources']=[{'sourceId':'0-source-2','reason':'No usable supporting passage.'}]
        with self.assertRaisesRegex(SourceAdmissionError,'two independently fetched documents'):
            self.run_scope()

    def test_typographic_equivalence_does_not_spend_an_evidence_retry(self):
        self.contents[URLS[0]] = 'The user’s privileged account requires independent approval.'
        self.server.evidence['sources'][0]['passages'][0]['text'] = (
            "The user's privileged account requires independent approval.")
        admitted = self.run_scope(evidence_attempts=2)
        self.assertEqual(len(self.server.calls), 2)
        self.assertEqual(admitted['sources']['0-source-1']['typographyNormalizedPassages'], ['p1'])

    def test_exhausted_quotation_repairs_still_reject_with_complete_evidence_feedback(self):
        self.server.evidence['sources'][0]['passages'][0]['text'] = 'Fabricated quotation missing from the source.'
        with self.assertRaisesRegex(SourceAdmissionError, 'not found.*0-source-1/p1') as failure:
            self.run_scope(evidence_attempts=2)
        self.assertEqual(self.fetches, URLS)
        self.assertEqual(len(self.server.calls), 3)
        details = failure.exception.details
        self.assertEqual(details['phase'], 'evidence')
        self.assertEqual(details['evidenceCandidate'], self.server.evidence)
        self.assertEqual(len(details['evidenceFailures']), 2)
        self.assertEqual(len(details['snapshots']), 2)
        self.assertEqual(details['observedResultUrls'], sorted(URLS))

    def test_missing_coverage_returns_to_discovery_without_repeating_extraction(self):
        self.server.evidence['blockingGaps'] = ['Synthetic missing encryption evidence']
        with self.assertRaises(SourceAdmissionError) as failure:
            self.run_scope(evidence_attempts=2)
        self.assertEqual(len(self.server.calls), 2)
        details = failure.exception.details
        self.assertEqual(details['phase'], 'coverage')
        self.assertEqual(details['blockingGaps'], self.server.evidence['blockingGaps'])
        self.assertEqual(details['evidenceCandidate'], self.server.evidence)
        self.assertEqual(details['discovery'], self.server.discovery)

    def test_evidence_repairs_consume_the_original_source_deadline(self):
        clock = [0]
        original = self.server.ask
        invalid = copy.deepcopy(self.server.evidence)
        invalid['sources'][0]['passages'][0]['text'] = 'Fabricated quotation missing from the source.'
        self.server.evidence_responses = [invalid]
        def ask(*args, **kwargs):
            clock[0] += 40
            return original(*args, **kwargs)
        self.server.ask = ask
        with patch('ci.source_research.time.monotonic', side_effect=lambda: clock[0]):
            self.run_scope(deadline=180, evidence_attempts=2)
        self.assertEqual([call['seconds'] for call in self.server.calls], [180, 140, 100])

    def test_evidence_repair_does_not_start_after_source_deadline(self):
        clock = [0]
        original = self.server.ask
        self.server.evidence['sources'][0]['passages'][0]['text'] = 'Fabricated quotation missing from the source.'
        def ask(*args, **kwargs):
            clock[0] += 40 if not self.server.calls else 150
            return original(*args, **kwargs)
        self.server.ask = ask
        with patch('ci.source_research.time.monotonic', side_effect=lambda: clock[0]), \
             self.assertRaisesRegex(TimeoutError, 'Source evidence timeout exhausted'):
            self.run_scope(deadline=180, evidence_attempts=2)
        self.assertEqual(len(self.server.calls), 2)

    def test_percent_encoded_unicode_url_matches_observed_iri(self):
        iri = 'https://www.cisa.gov/συνθετικό-test'
        uri = quote(iri, safe=':/')
        self.server.discovery['sources'][0]['url'] = uri
        self.server.outputs[0] = json.dumps({'results': [{'url': iri}, {'url': URLS[1]}]})
        self.contents[uri] = PASSAGES[0]
        self.assertEqual(len(self.run_scope()['sources']), 2)
        self.assertEqual(self.fetches[0], uri)
        self.assertEqual(url_identity(iri), url_identity(uri.lower()))
        self.assertNotEqual(url_identity('https://www.cisa.gov/a%2Fb'), url_identity('https://www.cisa.gov/a/b'))

    def test_url_prefix_and_query_echo_are_not_document_provenance(self):
        for output in (json.dumps({'results': [{'url': URLS[0] + '-different'}, {'url': URLS[1]}]}),
                       json.dumps({'query': URLS[0], 'results': [{'url': URLS[1]}]})):
            with self.subTest(output=output):
                self.server.outputs[0] = output
                with self.assertRaisesRegex(SourceAdmissionError, 'URL was not observed'):
                    self.run_scope()
        self.assertEqual(self.fetches, [])

    def test_validated_subset_is_not_admitted_without_topic_implementation_document(self):
        for source in self.server.discovery['sources']:
            source['type'] = 'guidance'
        with self.assertRaisesRegex(SourceAdmissionError, 'technical implementation documentation'):
            self.run_scope()
        self.assertEqual(self.fetches, [])

    def test_warnings_preserved_with_verified_bytes_and_controller_scope_ids(self):
        admitted = self.run_scope()
        self.assertEqual(set(admitted['sources']), {'0-source-1', '0-source-2'})
        self.assertEqual(len(admitted['limitations']), 2)
        self.assertTrue(all(w['scope'] == '0' for w in admitted['limitations']))
        self.assertEqual(self.fetches, URLS)  # Each document fetched once, before extraction.
        self.assertEqual(len(admitted['queries']), 2)
        self.assertEqual(self.server.calls[0]['required_tool_groups'], GROUPS)
        self.assertLess(self.server.calls[1]['seconds'], self.server.calls[0]['seconds'])
        self.assertIn('"scopeId": "0"', self.server.calls[0]['prompt'])
        self.assertNotIn('not-needed-assessments', self.server.calls[0]['prompt'])
        self.assertIn(PASSAGES[0], self.server.calls[1]['prompt'])
        self.assertIn('localTextPath', self.server.calls[1]['prompt'])
        for s in admitted['sources'].values():
            self.assertEqual(s['sourceStatus'], 'fetched-and-passage-checked')
            self.assertTrue((self.work / 'frozen/snapshots' / s['snapshotPath']).exists())

    def test_fabricated_quotation_rejected_despite_empty_blocking_gaps(self):
        self.server.evidence['sources'][1]['passages'][0]['text'] = (
            'Synthetic fabricated requirement, absent from the document.')
        with self.assertRaisesRegex(ValueError, 'not found.*0-source-2/p1'):
            self.run_scope()
        self.assertEqual(self.fetches, URLS)
        # Fetched documents and discovery limitations remain reviewable on failure.
        archive = read(self.work / 'discovery-candidates/sources-0-1.json')
        self.assertEqual(len(archive['snapshots']), 2)
        self.assertTrue(archive['discoveryLimitations'])

    def test_claimed_evidence_gap_is_a_specific_blocker(self):
        self.server.evidence['blockingGaps'] = ['Required encryption support was not available.']
        with self.assertRaisesRegex(ValueError, 'Blocking source evidence gaps: Required encryption'):
            self.run_scope()

    def test_ids_changed_to_area_title_are_rejected(self):
        self.server.evidence['concerns'][0]['sfsId'] = 'Operating System and Workstation Environment'
        with self.assertRaisesRegex(ValueError, 'fixed scope 0'):
            self.run_scope()
        self.server.evidence['concerns'][0]['sfsId'] = '0'
        self.server.evidence['sources'][0]['id'] = 'os-source-1'
        with self.assertRaisesRegex(ValueError, 'controller-assigned source IDs'):
            self.run_scope()

    def test_undiscovered_url_is_rejected_before_fetch(self):
        self.server.discovery['sources'][0]['url'] = 'https://www.cisa.gov/invented-document'
        with self.assertRaisesRegex(ValueError, 'URL was not observed'):
            self.run_scope()
        self.assertEqual(self.fetches, [])

    def test_reads_of_truncated_search_output_are_observed_provenance(self):
        self.server.outputs = ['Truncated search; full output at /synthetic/tool-output/file',
                               'Synthetic search completed']
        self.server.extra_parts = [{'type': 'tool', 'tool': 'read', 'state': {
            'status': 'completed', 'input': {'filePath': '/synthetic/tool-output/file'},
            'output': json.dumps({'results': [{'url': u} for u in URLS]})}}]
        self.assertEqual(len(self.run_scope()['sources']), 2)

    def test_mirrors_are_not_two_substantive_documents(self):
        self.contents[URLS[1]] = PASSAGES[0]
        with self.assertRaisesRegex(ValueError, 'identical document bytes'):
            self.run_scope()
        self.assertEqual(len(self.server.calls), 1)

    def test_legal_scope_cannot_ignore_missing_core_instrument(self):
        with self.assertRaisesRegex(ValueError, 'Missing fetched core legal instrument: EU Directive'):
            self.run_scope('legal')


if __name__ == '__main__':
    unittest.main()
