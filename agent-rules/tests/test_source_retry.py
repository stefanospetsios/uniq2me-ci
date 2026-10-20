"""Deferred downloads use synthetic HTTP only; retries never admit evidence."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import byte_digest, read, write
from ci.rag_store import query_records
from ci.source_cache import managed_files
from ci.source_research import SourceDownloadPending
from ci.source_retry import record_failure, retry_downloads, retry_key
from ci.smoke_test import create_fixture_pipeline
import test_source_research as source_fixtures
from test_source_research import URLS, PASSAGES, GROUPS, metadata


class SourceRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = {'runId': 'synthetic-run', 'controllerCommit': 'a' * 40, 'phase': 'prepare'}
        self.candidate = {**metadata(URLS[0]), 'id': 'D-source-2'}

    def test_transport_failure_is_filterable_and_repeated_attempts_keep_origin(self):
        first = record_failure(self.root, self.candidate, 'HTTP 400', self.context)
        second = record_failure(self.root, self.candidate, 'HTTP 503', {**self.context, 'runId': 'next-run'})
        self.assertEqual(second['attempts'], 2)
        self.assertEqual(second['firstFailedAt'], first['firstFailedAt'])
        self.assertEqual(second['originRunId'], 'synthetic-run')
        self.assertEqual(second['firstFailureContext']['runId'], 'synthetic-run')
        self.assertFalse(second['evidenceAdmitted'])
        self.assertIsNone(second['originalText'])
        selected = list(query_records(self.root, kind='source-download-retry', scope='D', status='pending-download'))
        self.assertEqual(selected, [second])
        self.assertEqual(len(managed_files(self.root)), 1)

    def test_http_failure_keeps_scope_pending_and_other_scopes_continue(self):
        with patch.dict(os.environ, {'RAG_MODE': 'repo-web', 'SOURCE_CACHE_MODE': 'off'}), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(self.root / 'coordinator', publish=False)
            pipeline.state.update(dryRun=False, ragMode='repo-web', sourceScopes=['D', 'E', 'F'],
                                 runnerPreflight={'status': 'passed'})
            pipeline.ensure_live_model = Mock()
            server = Mock()
            server.__enter__ = Mock(return_value=server)
            server.__exit__ = Mock(return_value=False)
            server.redact.side_effect = lambda v: v
            server.discover.return_value = {'allowedTools': [GROUPS['searxng'][0], GROUPS['websearch'][0]], 'openCodeVersion': 'synthetic'}
            pipeline.server = Mock(return_value=server)
            visited = []
            def research(server, scope, *args, **kwargs):
                visited.append(scope)
                if scope == 'D':
                    raise SourceDownloadPending('HTTP 400 left too few documents', {'phase': 'retrieval',
                        'retrievalFailures': [{'candidate': self.candidate, 'reason': 'HTTP 400', 'retryableDownload': True}],
                        'blockingGaps': []})
                return {'sources': {}, 'concerns': [], 'queries': [], 'limitations': []}
            with patch('ci.source_research.research_scope', side_effect=research), \
                 patch.object(pipeline, 'freeze_source_corpus') as freeze:
                pipeline.sources()
            self.assertEqual(visited, ['D', 'D', 'E', 'F'])
            self.assertEqual(pipeline.state['pendingSourceScopes']['D']['status'], 'pending-source-downloads')
            self.assertTrue(pipeline.catalog_incomplete())
            self.assertFalse(pipeline.accepted_release())
            freeze.assert_called_once()
            self.assertIn('HTTP 400', freeze.call_args.args[3][0]['description'])

    def test_successful_retry_fetches_bytes_but_does_not_admit_scope_or_passages(self):
        rag = self.root / 'rag'
        queued = record_failure(rag, self.candidate, 'HTTP 400', self.context)
        response = source_fixtures.SourceResearchTests()
        response.contents = {URLS[0]: PASSAGES[0]}
        response.fetches, response.retrieval_errors, response.content_types = [], {}, {}
        with patch('ci.pipeline.urllib.request.build_opener') as opener, redirect_stdout(io.StringIO()):
            opener.return_value.open.side_effect = response.response
            report = retry_downloads(rag, self.root / 'retry', scope='D')
            # Local replay must not fetch a completed queue item twice.
            replay = retry_downloads(rag, self.root / 'retry', scope='D')
        outbox = self.root / 'retry/source-cache-outbox'
        record = read(outbox / 'download-retries' / (queued['id'] + '.json'))
        self.assertEqual(report['downloaded'], 1)
        self.assertEqual(replay['results'], [])
        self.assertEqual(response.fetches, [URLS[0]])
        self.assertEqual(record['status'], 'downloaded-awaiting-evidence')
        self.assertEqual(record['foundIn']['phase'], 'retry-downloads')
        self.assertFalse(record['evidenceAdmitted'])
        self.assertFalse((outbox / 'scopes').exists())
        self.assertFalse((outbox / 'passages').exists())
        document = next((outbox / 'documents').glob('*.json'))
        saved = read(document)['source']
        self.assertEqual(saved['sourceStatus'], 'fetched-awaiting-passage-check')
        self.assertEqual(byte_digest(outbox / 'snapshots' / saved['snapshotPath']), saved['byteDigest'])

    def test_challenge_download_defers_only_its_scope_and_continues_actual_source_preparation(self):
        self.source_continuation(download=True)

    def test_exhausted_bad_quotations_defer_only_their_scope_and_preserve_research_failure(self):
        self.source_continuation(download=False)

    def test_exhausted_malformed_model_json_defers_only_its_scope_and_continues(self):
        self.source_continuation(download=False,malformed=True)

    def test_missing_search_calls_defer_only_their_scope_and_continue(self):
        self.source_continuation(download=False,missing_search=True)

    def source_continuation(self, download, malformed=False, missing_search=False):
        from test_source_documents import CHALLENGE
        from test_rag import MODEL
        case = source_fixtures.SourceResearchTests()
        case.setUp(); self.addCleanup(case.doCleanups)
        if download:
            case.contents[URLS[0]] = CHALLENGE
        else:
            case.server.evidence['sources'][0]['passages'][0]['text'] = 'Synthetic fabricated quotation absent from every fetched document.'
        with patch.dict(os.environ, {'RAG_MODE': 'repo-web', 'SOURCE_CACHE_MODE': 'off'}), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(self.root / 'continuation', publish=False)
            pipeline.state.update(dryRun=False, ragMode='repo-web', sourceScopes=['0','1'],
                                  runnerPreflight={'status':'passed'}, model=MODEL.copy())
            pipeline.ensure_live_model = Mock()
            server = case.server
            server.discover = lambda: {'allowedTools':[GROUPS['searxng'][0],GROUPS['websearch'][0]],
                                      'openCodeVersion':'synthetic'}
            server.redact = lambda value: value
            original_ask = server.ask
            def ask(role, prompt, seconds, call_id, **kwargs):
                if missing_search and call_id.startswith('sources-0-'):
                    from ci.runtime import MissingToolCallsError
                    raise MissingToolCallsError(['websearch'])
                if malformed and call_id.startswith('sources-0-') and not call_id.endswith('-discovery'):
                    from ci.runtime import parse_json_answer
                    parse_json_answer('{"sources":[{"id":"unfinished')
                if call_id.startswith('sources-1-'):
                    case.contents[URLS[0]] = PASSAGES[0]
                    server.evidence['sources'][0]['passages'][0]['text'] = PASSAGES[0]
                    server.evidence['sources'][0]['id'] = '1-source-1'
                    server.evidence['sources'][1]['id'] = '1-source-2'
                    server.evidence['concerns'][0].update(id='1-concern-1',sfsId='1',
                        sourceRefs=[{'sourceId':'1-source-1','passageId':'p1'}])
                return original_ask(role,prompt,seconds,call_id,**kwargs)
            server.ask = ask
            pipeline.server = Mock(side_effect=lambda stage: nullcontext(server))
            # The fixture leaves run-config fields unconfigured; exercise source
            # preparation/freeze here, with configuration validation covered separately.
            with patch('ci.pipeline.urllib.request.build_opener') as opener, \
                 patch('ci.pipeline.command',return_value=Mock(stdout='Synthetic valid config')) as config_check:
                opener.return_value.open.side_effect = case.response
                pipeline.sources()
            config_check.assert_called_once()
            self.assertTrue(config_check.call_args.args[0][1].endswith('validate_run_config.py'))
            status = 'pending-source-downloads' if download else 'pending-source-research'
            self.assertEqual(pipeline.state['pendingSourceScopes']['0']['status'], status)
            self.assertNotIn('1',pipeline.state['pendingSourceScopes'])
            self.assertTrue(pipeline.state['frozenManifest'])
            admitted = read(pipeline.work / 'frozen/sources.json')['sources']
            self.assertEqual({s['id'] for s in admitted}, {'1-source-1','1-source-2'})
            if download:
                queued = next((pipeline.root / 'source-cache-outbox/download-retries').glob('*.json'))
                self.assertEqual(read(queued)['status'],'pending-download')
                self.assertIn('JavaScript/bot challenge',read(queued)['lastError'])
            else:
                self.assertFalse((pipeline.root / 'source-cache-outbox/download-retries').exists())
                failures = read(pipeline.root / 'source-rejections.json')
                self.assertEqual(len(failures),pipeline.state['maximumAttempts'])
                phase = 'discovery' if missing_search else 'evidence'
                self.assertTrue(all(f['scope']=='0' and f['details']['phase']==phase for f in failures))
                if missing_search:
                    self.assertTrue(all(f['details']['missingToolGroups']==['websearch'] for f in failures))
                self.assertFalse(pipeline.accepted_release())
                if malformed:
                    self.assertTrue(all(row['ruleId']=='invalid-model-json' for f in failures
                        for row in f['details']['evidenceFailures']))

    def test_model_or_infrastructure_failure_does_not_become_pending_research(self):
        with patch.dict(os.environ, {'RAG_MODE':'repo-web','SOURCE_CACHE_MODE':'off'}), redirect_stdout(io.StringIO()):
            pipeline,_=create_fixture_pipeline(self.root/'strict',publish=False)
            pipeline.state.update(dryRun=False,ragMode='repo-web',sourceScopes=['0'],runnerPreflight={'status':'passed'})
            pipeline.ensure_live_model=Mock()
            server=Mock();server.__enter__=Mock(return_value=server);server.__exit__=Mock(return_value=False)
            server.redact.side_effect=lambda value:value
            server.discover.return_value={'allowedTools':[GROUPS['searxng'][0],GROUPS['websearch'][0]],'openCodeVersion':'synthetic'}
            pipeline.server=Mock(return_value=server)
            with patch('ci.source_research.research_scope',side_effect=RuntimeError('Pinned model digest changed')), \
                 patch.object(pipeline,'freeze_source_corpus') as freeze, \
                 self.assertRaisesRegex(RuntimeError,'Pinned model digest changed'):
                pipeline.sources()
            freeze.assert_not_called()
            self.assertFalse(pipeline.state.get('pendingSourceScopes'))

    def test_challenge_stays_queued_until_real_text_downloads_without_admitting_evidence(self):
        from test_source_documents import CHALLENGE
        rag = self.root / 'rag'
        queued = record_failure(rag,self.candidate,'Earlier unavailable document',self.context)
        response = source_fixtures.SourceResearchTests()
        response.contents = {URLS[0]:CHALLENGE}
        response.fetches, response.retrieval_errors, response.content_types = [], {}, {}
        with patch('ci.pipeline.urllib.request.build_opener') as opener, redirect_stdout(io.StringIO()):
            opener.return_value.open.side_effect = response.response
            blocked = retry_downloads(rag,self.root / 'retry')
            self.assertEqual(blocked['pending'],1)
            self.assertEqual(blocked['downloaded'],0)
            response.contents[URLS[0]] = PASSAGES[0]
            recovered = retry_downloads(rag,self.root / 'retry')
        self.assertEqual(recovered['downloaded'],1)
        self.assertFalse(recovered['evidenceAdmitted'])
        outbox = self.root / 'retry/source-cache-outbox'
        record = read(outbox / 'download-retries' / (queued['id']+'.json'))
        self.assertEqual(record['status'],'downloaded-awaiting-evidence')
        self.assertFalse((outbox / 'scopes').exists())
        self.assertFalse((outbox / 'passages').exists())

    def test_failed_retry_stays_pending_and_does_not_stop_other_urls(self):
        rag = self.root / 'rag'
        candidates = [self.candidate, {**metadata(URLS[1]), 'id': 'E-source-1'}]
        for candidate in candidates:
            record_failure(rag, candidate, 'Earlier failure', self.context)
        response = source_fixtures.SourceResearchTests()
        response.contents = dict(zip(URLS, PASSAGES))
        response.fetches, response.content_types = [], {}
        error = HTTPError(URLS[0], 400, 'Bad Request', {}, None)
        self.addCleanup(error.close)
        response.retrieval_errors = {URLS[0]: error}
        with patch('ci.pipeline.urllib.request.build_opener') as opener, redirect_stdout(io.StringIO()):
            opener.return_value.open.side_effect = response.response
            report = retry_downloads(rag, self.root / 'retry')
        self.assertEqual(report['downloaded'], 1)
        self.assertEqual(report['pending'], 1)
        pending = read(self.root / 'retry/source-cache-outbox/download-retries' / (retry_key(self.candidate) + '.json'))
        self.assertEqual(pending['attempts'], 2)
        self.assertEqual(pending['status'], 'pending-download')
        self.assertIn('400', pending['lastError'])

    def test_retry_scope_filter_and_limit_do_not_fetch_other_queued_documents(self):
        rag = self.root / 'rag'
        record_failure(rag, self.candidate, 'HTTP 400', self.context)
        with patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('wrong scope')):
            report = retry_downloads(rag, self.root / 'retry', scope='E', limit=1)
        self.assertEqual(report['results'], [])

    def test_daily_batch_limit_rotates_away_from_a_persistent_failure(self):
        rag = self.root / 'rag'
        other = {**metadata(URLS[1]), 'id': 'E-source-1'}
        for candidate, timestamp in [(self.candidate, '2020-01-01T00:00:00Z'), (other, '2020-01-02T00:00:00Z')]:
            queued = record_failure(rag, candidate, 'Earlier failure', self.context)
            queued['lastAttemptAt'] = timestamp
            write(rag / 'download-retries' / (queued['id'] + '.json'), queued)
        response = source_fixtures.SourceResearchTests()
        response.contents = dict(zip(URLS, PASSAGES))
        response.fetches, response.content_types = [], {}
        error = HTTPError(URLS[0], 400, 'Bad Request', {}, None)
        self.addCleanup(error.close)
        response.retrieval_errors = {URLS[0]: error}
        with patch('ci.pipeline.urllib.request.build_opener') as opener, redirect_stdout(io.StringIO()):
            opener.return_value.open.side_effect = response.response
            first = retry_downloads(rag, self.root / 'retry', limit=1)
            second = retry_downloads(rag, self.root / 'retry', limit=1)
        self.assertEqual(first['pending'], 1)
        self.assertEqual(second['downloaded'], 1)
        self.assertEqual(response.fetches, URLS)

    def test_download_caused_coverage_gap_is_pending_even_when_document_quota_passes(self):
        case = source_fixtures.SourceResearchTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.unavailable_candidates()
        case.server.evidence['blockingGaps'] = ['Unavailable document leaves required device recovery coverage unsupported']
        with self.assertRaises(SourceDownloadPending) as failure:
            case.run_scope()
        self.assertEqual(failure.exception.details['phase'], 'coverage')
        self.assertEqual(len(failure.exception.details['snapshots']), 2)


if __name__ == '__main__':
    unittest.main()
