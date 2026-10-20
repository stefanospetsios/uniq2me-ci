"""Reference-cache reuse and publication with mocked HTTP and local Git only."""
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import HEX, byte_digest, digest, read, write
from ci.pipeline import check_source_passages
from ci.smoke_test import create_fixture_pipeline
from ci.source_cache import (CACHE_DIRECTORY, CACHE_REPOSITORY, SourceCache, configuration, git, hydrate,
                             managed_files, publish, publish_files)
from ci.rag_store import query_records
from ci.source_research import research_scope, url_identity
import test_source_research as source_fixtures
from test_source_research import GROUPS, PASSAGES, URLS, Server, metadata


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stdout = redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.environment = patch.dict(os.environ, {'SOURCE_CACHE_MODE': 'reuse', 'RAG_MODE': 'repo-web',
            'SOURCE_CACHE_MAX_AGE_DAYS': '7', 'SOURCE_CACHE_TOKEN': 'synthetic-local-token'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.pipeline, _ = create_fixture_pipeline(self.root / 'first')
        self.pipeline.state.update(dryRun=False, ragMode='repo-web', sourceCache=configuration(),
            model={'digest': 'sha256:' + '1' * 64, 'effectiveContext': 262144})
        self.cache = SourceCache(self.pipeline)
        self.area = {'keyTitle': 'Synthetic endpoints', 'keyWords': ['patching', 'encryption']}
        self.fingerprint = self.cache.scope_fingerprint('0', self.area)
        self.server = Server(self.root / 'runtime')
        self.responses = source_fixtures.SourceResearchTests()
        self.responses.contents = dict(zip(URLS, PASSAGES))
        self.responses.fetches = []
        self.responses.retrieval_errors = {}
        self.responses.content_types = {}

    def research(self):
        with patch('ci.pipeline.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = self.responses.response
            return research_scope(self.server, '0', self.area, GROUPS, ['cisa.gov', 'nist.gov'],
                'Synthetic rules', self.pipeline.work, time.monotonic() + 900, 'sources-0-1', {},
                self.cache.fetch_snapshot, check_source_passages)

    def admitted(self):
        corpus = self.research()
        self.cache.remember_scope('0', self.fingerprint, corpus)
        return corpus

    def next_run(self):
        pipeline, _ = create_fixture_pipeline(self.root / 'next')
        pipeline.state.update(dryRun=False, ragMode='repo-web', model=copy.deepcopy(self.pipeline.state['model']),
                              sourceCache=configuration(), runId='qwen3-next-synthetic-run')
        imported = pipeline.root / 'source-cache-import'
        shutil.copytree(self.cache.outbox, imported)
        pipeline.state['sourceCache'].update(importedCommit='2' * 40, importedManifest=managed_files(imported))
        return pipeline, SourceCache(pipeline)

    def test_next_run_reuses_verified_scope_without_search_or_download(self):
        original = self.admitted()
        pipeline, cache = self.next_run()
        fingerprint = cache.scope_fingerprint('0', self.area)
        self.assertEqual(fingerprint, self.fingerprint)
        with patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('unexpected network')):
            reused = cache.reuse_scope('0', fingerprint)
        self.assertEqual(reused['concerns'], original['concerns'])
        self.assertEqual(len(self.server.calls), 2)  # Only original discovery/extraction.
        for identity, source in reused['sources'].items():
            self.assertEqual(source['retrievedAt'], original['sources'][identity]['retrievedAt'])
            self.assertFalse(source['cacheReuse']['webRecheckedThisRun'])
            self.assertEqual(byte_digest(pipeline.work / 'frozen/snapshots' / source['snapshotPath']), source['byteDigest'])
        self.assertTrue(all(q['cacheReuse']['originRunId'] == self.pipeline.state['runId'] for q in reused['queries']))
        self.assertEqual(pipeline.state['sourceCache']['scopesReused'], 1)
        self.assertEqual(pipeline.state['sourceCache']['documentsReused'], 2)
        self.assertIn('No update search', reused['limitations'][-1]['description'])
        self.assertEqual(managed_files(cache.outbox), {})

    def test_repository_only_loads_saved_quotations_without_network_or_scope_admission(self):
        from ci.rag import prepare_repository_sources
        original = self.admitted()
        pipeline, cache = self.next_run()
        pipeline.state.update(ragMode='repo-only', sourceScopes=['0'])
        with patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('unexpected download')), \
             patch('ci.source_research.research_scope', side_effect=AssertionError('unexpected search')):
            sources, concerns, queries, limitations = prepare_repository_sources(pipeline)
        saved = [s for s in sources.values() if s.get('storedSourceId')]
        self.assertEqual(len(saved), 2)  # Scope records must not duplicate document records.
        for source in saved:
            prior = original['sources'][source['storedSourceId']]
            self.assertEqual(source['passages'], prior['passages'])
            self.assertEqual(source['retrievedAt'], prior['retrievedAt'])
            self.assertEqual(source['cacheReuse']['cacheCommit'], '2' * 40)
            self.assertFalse(source['cacheReuse']['webRecheckedThisRun'])
            self.assertEqual(byte_digest(pipeline.work / 'frozen/snapshots' / source['textPath']), source['textDigest'])
        self.assertEqual(queries, [])
        self.assertEqual(pipeline.state['allowedTools'], [])
        self.assertEqual(pipeline.state['sourceCache']['scopesReused'], 0)
        self.assertEqual(pipeline.state['sourceCache']['documentsReused'], 2)
        self.assertEqual(managed_files(cache.outbox), {})
        self.assertIn('unverified drafts', limitations[0]['description'])

    def test_repository_only_source_stage_freezes_saved_rag_without_starting_retrieval(self):
        from test_rag import MODEL
        self.admitted()
        pipeline, _ = self.next_run()
        pipeline.state.update(ragMode='repo-only', sourceScopes=['0'], model=copy.deepcopy(MODEL),
                              runnerPreflight={'status': 'passed'})
        pipeline.ensure_live_model = Mock()
        pipeline.server = Mock(side_effect=AssertionError('unexpected retrieval server'))
        with patch.dict(os.environ, {'RAG_MODE': 'repo-only'}), \
             patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('unexpected download')), \
             patch('ci.pipeline.command', return_value=Mock(stdout='Synthetic valid config')):
            pipeline.sources()
        frozen = read(pipeline.work / 'frozen/sources.json')
        self.assertIn('frozenManifest', pipeline.state)
        self.assertEqual(sum(bool(s.get('storedSourceId')) for s in frozen['sources']), 2)
        self.assertEqual(frozen['rag']['outputPolicy'], 'unverified-draft-only')
        self.assertFalse(frozen['rag']['webResearch'])
        pipeline.server.assert_not_called()

    def test_repository_only_recovers_quotations_from_scopes_when_document_index_has_no_passages(self):
        from ci.rag import saved_repository_research
        self.admitted()
        pipeline, cache = self.next_run()
        for path in (cache.imported / 'documents').glob('*.json'):
            record = read(path)
            record['source'].pop('passages', None)
            record['source']['sourceStatus'] = 'fetched-awaiting-passage-check'
            write(path, record)
        pipeline.state['sourceCache']['importedManifest'] = managed_files(cache.imported)
        saved, _ = saved_repository_research(pipeline)
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(s['cacheReuse']['cacheRecord'].startswith('scopes/') for s in saved.values()))

    def test_repository_only_filters_saved_research_by_framework_and_selected_scope(self):
        from ci.rag import saved_repository_research
        self.admitted()
        pipeline, cache = self.next_run()
        pipeline.state.update(catalogProfile={'frameworkId': 'dora'}, sourceScopes=['0'])
        self.assertEqual(saved_repository_research(pipeline)[0], {})
        for collection in ('documents', 'scopes'):
            for path in (cache.imported / collection).glob('*.json'):
                record = read(path)
                record['filters']['frameworkIds'] = ['dora']
                write(path, record)
        pipeline.state['sourceCache']['importedManifest'] = managed_files(cache.imported)
        self.assertEqual(len(saved_repository_research(pipeline)[0]), 2)
        pipeline.state['sourceScopes'] = ['1']
        self.assertEqual(saved_repository_research(pipeline)[0], {})
        pipeline.state.pop('catalogProfile')
        pipeline.state['sourceScopes'] = ['0']
        self.assertEqual(saved_repository_research(pipeline)[0], {})

    def test_repository_only_never_uses_unchecked_quotes_or_modified_snapshots(self):
        from ci.rag import saved_repository_research
        self.server.evidence['sources'][0]['passages'][0]['text'] = 'Fabricated passage absent from downloaded bytes.'
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.research()
        pipeline, cache = self.next_run()
        self.assertEqual(saved_repository_research(pipeline)[0], {})
        self.server.evidence['sources'][0]['passages'][0]['text'] = PASSAGES[0]
        self.admitted()
        shutil.rmtree(cache.imported)
        shutil.copytree(self.cache.outbox, cache.imported)
        pipeline.state['sourceCache']['importedManifest'] = managed_files(cache.imported)
        for path in (cache.imported / 'snapshots').glob('*.txt'):
            path.write_text('Changed snapshot, not original evidence.')
        self.assertEqual(saved_repository_research(pipeline)[0], {})

    def test_failed_passage_extraction_still_exports_reusable_raw_documents(self):
        self.server.evidence['sources'][0]['passages'][0]['text'] = 'Fabricated passage absent from downloaded bytes.'
        with self.assertRaisesRegex(ValueError, 'not found'):
            self.research()
        self.assertEqual(len(list((self.cache.outbox / 'documents').glob('*.json'))), 2)
        self.assertFalse((self.cache.outbox / 'scopes').exists())
        pipeline, cache = self.next_run()
        archive = pipeline.work / 'frozen/snapshots'
        with patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('unexpected network')):
            source = cache.fetch_snapshot(metadata(URLS[0]), archive, time.monotonic() + 900)
        self.assertEqual(source['sourceStatus'], 'fetched-awaiting-passage-check')
        self.assertNotIn('passages', source)
        self.assertFalse(source['cacheReuse']['webRecheckedThisRun'])

    def test_legacy_cached_challenge_is_refetched_instead_of_reused(self):
        from test_source_documents import CHALLENGE
        self.responses.contents[URLS[0]] = CHALLENGE
        with patch('ci.source_documents.ensure_source_available'):
            with patch('ci.pipeline.urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = self.responses.response
                self.cache.fetch_snapshot(metadata(URLS[0]), self.pipeline.work / 'old-snapshots', time.monotonic() + 60)
        pipeline, cache = self.next_run()
        self.responses.contents[URLS[0]] = PASSAGES[0]
        with patch('ci.pipeline.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = self.responses.response
            fetched = cache.fetch_snapshot(metadata(URLS[0]), pipeline.work / 'new-snapshots', time.monotonic() + 60)
        self.assertEqual(self.responses.fetches, [URLS[0], URLS[0]])
        self.assertNotIn('cacheReuse', fetched)
        self.assertEqual((pipeline.work / 'new-snapshots' / fetched['textPath']).read_text().strip(), PASSAGES[0])

    def test_new_documentation_host_survives_verified_scope_and_document_cache_reuse(self):
        url = 'https://docs.typo3.org/permalink/t3coreapi:privileges@main'
        self.responses.contents[url] = PASSAGES[0]
        self.server.discovery['sources'][0] = metadata(url)
        self.server.outputs[0] = json.dumps({'results': [{'url': u} for u in [url, URLS[1]]]})
        self.admitted()
        pipeline, cache = self.next_run()
        with patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('unexpected network')):
            reused = cache.reuse_scope('0', self.fingerprint)
            document = cache.fetch_snapshot(metadata(url), pipeline.work / 'cached-document', time.monotonic() + 60)
        self.assertEqual(reused['sources']['0-source-1']['url'], url)
        self.assertEqual(document['url'], url)
        self.assertEqual(document['cacheReuse']['originRunId'], self.pipeline.state['runId'])
        self.assertFalse(document['cacheReuse']['webRecheckedThisRun'])

    def test_scope_with_excluded_first_document_reuses_original_nonconsecutive_source_ids(self):
        outside = 'http://outside.example.invalid/synthetic-document'
        self.server.discovery['sources'].insert(0, metadata(outside))
        self.server.outputs[0] = json.dumps({'results': [{'url': u} for u in [outside, *URLS]]})
        for index, source in enumerate(self.server.evidence['sources'], 2):
            source['id'] = f'0-source-{index}'
        self.server.evidence['concerns'][0]['sourceRefs'][0]['sourceId'] = '0-source-2'
        original = self.admitted()
        pipeline, cache = self.next_run()
        with patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('unexpected network')):
            reused = cache.reuse_scope('0', self.fingerprint)
        self.assertIsNotNone(reused)
        self.assertEqual(set(reused['sources']), {'0-source-2', '0-source-3'})
        self.assertEqual(reused['concerns'], original['concerns'])
        self.assertEqual(self.responses.fetches, URLS)
        self.assertEqual(len(self.server.calls), 2)
        self.assertEqual(pipeline.state['sourceCache']['scopesReused'], 1)

    def test_cached_source_ids_cannot_change_scope(self):
        self.admitted()
        pipeline, cache = self.next_run()
        path = cache.imported / 'scopes' / (self.fingerprint + '.json')
        record = read(path)
        source = record['corpus']['sources'].pop('0-source-1')
        source['id'] = '1-source-1'
        record['corpus']['sources'][source['id']] = source
        write(path, record)
        pipeline.state['sourceCache']['importedManifest'] = managed_files(cache.imported)
        self.assertIsNone(cache.reuse_scope('0', self.fingerprint))

    def test_fresh_start_rederives_scope_but_reuses_document_bytes(self):
        self.admitted()
        pipeline, cache = self.next_run()
        pipeline.state['freshStart'] = True
        self.assertIsNone(cache.reuse_scope('0', self.fingerprint))
        with patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('unexpected network')):
            source = cache.fetch_snapshot(metadata(URLS[0]), pipeline.work / 'frozen/snapshots', time.monotonic() + 900)
        self.assertIn('cacheReuse', source)

    def test_refresh_bypasses_imports_but_same_run_retry_reuses_fetched_bytes(self):
        original = self.admitted()
        pipeline, cache = self.next_run()
        pipeline.state['sourceCache']['mode'] = 'refresh'
        self.assertIsNone(cache.reuse_scope('0', self.fingerprint))
        def fetch(candidate, archive, deadline):
            source = original['sources']['0-source-1']
            archive.mkdir(parents=True, exist_ok=True)
            for name in (source['snapshotPath'], source['textPath']):
                shutil.copyfile(self.pipeline.work / 'frozen/snapshots' / name, archive / name)
            return source
        with patch('ci.pipeline.fetch_source_snapshot', side_effect=fetch) as fetcher:
            cache.fetch_snapshot(metadata(URLS[0]), pipeline.work / 'frozen/snapshots', time.monotonic() + 900)
            repeated = cache.fetch_snapshot(metadata(URLS[0]), pipeline.work / 'frozen/snapshots', time.monotonic() + 900)
        self.assertEqual(fetcher.call_count, 1)
        self.assertIn('cacheReuse', repeated)

    def test_changed_topic_or_model_invalidates_scope(self):
        self.admitted()
        pipeline, cache = self.next_run()
        for area, model in (({**self.area, 'keyWords': ['different topic']}, self.pipeline.state['model']['digest']),
                            (self.area, 'sha256:' + '3' * 64)):
            pipeline.state['model']['digest'] = model
            fingerprint = cache.scope_fingerprint('0', area)
            self.assertNotEqual(fingerprint, self.fingerprint)
            self.assertIsNone(cache.reuse_scope('0', fingerprint))

    def test_corrupt_snapshot_is_rejected_and_falls_back_to_real_fetch(self):
        original = self.admitted()
        pipeline, cache = self.next_run()
        name = original['sources']['0-source-1']['textPath']
        (cache.imported / 'snapshots' / name).write_text('Tampered synthetic document')
        self.assertIsNone(cache.reuse_scope('0', self.fingerprint))
        with patch('ci.pipeline.fetch_source_snapshot', side_effect=RuntimeError('fresh fetch required')) as fetcher:
            with self.assertRaisesRegex(RuntimeError, 'fresh fetch required'):
                cache.fetch_snapshot(metadata(URLS[0]), pipeline.work / 'frozen/snapshots', time.monotonic() + 900)
        fetcher.assert_called_once()

    def test_expired_documents_and_scopes_are_not_reused(self):
        corpus = self.research()
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        for source in corpus['sources'].values():
            source['retrievedAt'] = old
        self.cache.remember_scope('0', self.fingerprint, corpus)
        pipeline, cache = self.next_run()
        self.assertIsNone(cache.reuse_scope('0', self.fingerprint))
        pipeline.state['sourceCache']['maxAgeDays'] = 0
        self.assertIsNotNone(cache.reuse_scope('0', self.fingerprint))

    def test_bad_quote_in_pinned_record_is_rechecked_and_rejected(self):
        self.admitted()
        pipeline, cache = self.next_run()
        path = cache.imported / 'scopes' / (self.fingerprint + '.json')
        record = read(path)
        record['corpus']['sources']['0-source-1']['passages'][0]['text'] = 'Invented normative text absent from the document.'
        write(path, record)
        pipeline.state['sourceCache']['importedManifest'] = managed_files(cache.imported)
        self.assertIsNone(cache.reuse_scope('0', self.fingerprint))

    def test_snapshot_path_traversal_is_rejected_before_reading(self):
        self.admitted()
        pipeline, cache = self.next_run()
        path = cache.imported / 'scopes' / (self.fingerprint + '.json')
        record = read(path)
        record['corpus']['sources']['0-source-1']['textPath'] = '../../outside.txt'
        write(path, record)
        pipeline.state['sourceCache']['importedManifest'] = managed_files(cache.imported)
        self.assertIsNone(cache.reuse_scope('0', self.fingerprint))

    def test_hydration_pins_main_data_without_changing_controller_revision(self):
        self.admitted()
        checkout = self.pipeline.workspace / 'uniq2me-rag'
        checkout.mkdir()
        git(['init', '-q', '-b', 'main'], checkout)
        git(['remote','add','origin',CACHE_REPOSITORY],checkout)
        shutil.copytree(self.cache.outbox, checkout / CACHE_DIRECTORY)
        git(['add', '.'], checkout)
        git(['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Reference cache'], checkout)
        original = self.pipeline.state['controllerCommit']
        hydrate(self.pipeline)
        pinned = self.pipeline.state['sourceCache']['importedCommit']
        self.assertEqual(self.pipeline.state['controllerCommit'], original)
        self.assertEqual(pinned, git(['rev-parse', 'HEAD'], checkout).stdout.strip())
        (checkout / CACHE_DIRECTORY / 'README.md').write_text('Unrelated cache documentation change')
        hydrate(self.pipeline)
        self.assertEqual(self.pipeline.state['sourceCache']['importedCommit'], pinned)

    def test_repository_only_hydrates_pinned_shared_research(self):
        self.pipeline.state['ragMode'] = 'repo-only'
        self.test_hydration_pins_main_data_without_changing_controller_revision()

    def test_publication_only_changes_cache_is_idempotent_and_keeps_output_history(self):
        self.admitted()
        controller = self.root / 'controller-main'
        remote = self.root / 'controller-remote.git'
        controller.mkdir()
        git(['init', '-q', '-b', 'main'], controller)
        git(['init', '--bare', '-q', str(remote)], self.root)
        (controller / 'controller.py').write_text('Synthetic controller code sentinel\n')
        git(['add', '.'], controller)
        git(['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Code base'], controller)
        git(['remote', 'add', 'origin', str(remote)], controller)
        git(['push', '-q', 'origin', 'main'], controller)
        output_head = git(['rev-parse', 'HEAD'], self.pipeline.output).stdout.strip()
        result = publish_files(self.cache.outbox, str(remote), self.root, '', 'synthetic-run')
        self.assertEqual(result['status'], 'pushed')
        repeated = publish_files(self.cache.outbox, str(remote), self.root, '', 'synthetic-run')
        self.assertEqual(repeated['status'], 'already-published')
        self.assertEqual(git(['rev-list', '--count', 'main'], remote).stdout.strip(), '2')
        self.assertEqual(git(['show', 'main:controller.py'], remote).stdout, 'Synthetic controller code sentinel\n')
        paths = git(['diff-tree', '--no-commit-id', '--name-only', '-r', 'main'], remote).stdout.splitlines()
        self.assertTrue(paths and all(name.startswith('data/') for name in paths))
        self.assertEqual(git(['rev-parse', 'HEAD'], self.pipeline.output).stdout.strip(), output_head)

    def test_nonweb_and_dry_never_publish_but_catalog_publish_false_still_saves_rag(self):
        with patch('ci.source_cache.publish_files') as publisher:
            for field, value in (('dryRun', True), ('ragMode', 'disabled'), ('ragMode', 'repo-only')):
                state = copy.deepcopy(self.pipeline.state)
                self.pipeline.state[field] = value
                publish(self.pipeline)
                self.pipeline.state = state
            self.pipeline.state['sourceCache']['mode'] = 'off'
            publish(self.pipeline)
        publisher.assert_not_called()
        self.pipeline.state.update(publish=False,sourceCache=configuration())
        with patch('ci.source_cache.publish_files',return_value={'status':'pushed','commit':'a'*40}) as publisher:
            publish(self.pipeline)
        self.assertEqual(publisher.call_args.args[1],CACHE_REPOSITORY)

    def test_configuration_validates_modes_and_expiry(self):
        for mode, days in (('unknown', '7'), ('reuse', '-1'), ('reuse', 'nan'), ('reuse', 'inf')):
            with self.subTest(mode=mode, days=days), patch.dict(os.environ, {
                    'SOURCE_CACHE_MODE': mode, 'SOURCE_CACHE_MAX_AGE_DAYS': days}), self.assertRaises(ValueError):
                configuration()
        with patch.dict(os.environ, {'SOURCE_CACHE_MAX_AGE_DAYS': '0'}):
            self.assertEqual(configuration()['maxAgeDays'], 0)
        with patch.dict(os.environ, {'SOURCE_CACHE_MAX_AGE_DAYS': ''}):
            self.assertEqual(configuration()['maxAgeDays'], 0)

    def test_concurrent_main_edit_is_kept_when_publisher_retries(self):
        self.admitted()
        controller, remote = self.root / 'racing-controller', self.root / 'racing-main.git'
        controller.mkdir()
        git(['init', '-q', '-b', 'main'], controller)
        git(['init', '--bare', '-q', str(remote)], self.root)
        (controller / 'controller.py').write_text('Original synthetic code\n')
        git(['add', '.'], controller)
        git(['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Base'], controller)
        git(['remote', 'add', 'origin', str(remote)], controller)
        git(['push', '-q', 'origin', 'main'], controller)
        raced = []
        def concurrent_git(args, cwd, token='', check=True):
            if args[0] == 'push' and 'HEAD:refs/heads/main' in args and not raced:
                raced.append(True)
                (controller / 'controller.py').write_text('User edit while RAG was running\n')
                git(['add', '.'], controller)
                git(['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Concurrent edit'], controller)
                git(['push', '-q', 'origin', 'main'], controller)
            return git(args, cwd, token, check)
        with patch('ci.source_cache.git', side_effect=concurrent_git):
            result = publish_files(self.cache.outbox, str(remote), self.root, '', 'synthetic-race')
        self.assertEqual(result['status'], 'pushed')
        self.assertEqual(git(['show', 'main:controller.py'], remote).stdout, 'User edit while RAG was running\n')
        self.assertEqual(git(['rev-list', '--count', 'main'], remote).stdout.strip(), '3')

    def test_cached_legal_scope_still_requires_core_eu_and_greek_instruments(self):
        self.admitted()
        pipeline, cache = self.next_run()
        fingerprint = cache.scope_fingerprint('legal', {'keyTitle': 'Synthetic legal scope'})
        record = read(cache.imported / 'scopes' / (self.fingerprint + '.json'))
        record.update(scopeId='legal', fingerprint=fingerprint)
        sources = {}
        for index, source in enumerate(record['corpus']['sources'].values(), 1):
            source.update(id=f'legal-source-{index}', type='law')
            sources[source['id']] = source
        record['corpus'].update(sources=sources, concerns=[])
        write(cache.imported / 'scopes' / (fingerprint + '.json'), record)
        pipeline.state['sourceCache']['importedManifest'] = managed_files(cache.imported)
        self.assertIsNone(cache.reuse_scope('legal', fingerprint))

    def test_changed_document_invalidates_previously_admitted_scope(self):
        corpus = self.admitted()
        pipeline, cache = self.next_run()
        key = digest(url_identity(URLS[0]))[7:]
        path = cache.imported / 'documents' / (key + '.json')
        record = read(path)
        # A separately fetched later version is still awaiting admission.
        updated = copy.deepcopy(corpus['sources']['0-source-2'])
        updated.update(url=URLS[0], id='0-source-1', sourceStatus='fetched-awaiting-passage-check')
        record.update(source=updated, files={
            'snapshots/' + name: byte_digest(cache.imported / 'snapshots' / name)
            for name in (updated['snapshotPath'], updated['textPath'])})
        write(path, record)
        pipeline.state['sourceCache']['importedManifest'] = managed_files(cache.imported)
        self.assertIsNone(cache.reuse_scope('0', self.fingerprint))

    def test_prepare_coordinator_reuses_main_scope_without_agent_generation(self):
        self.area = read(self.pipeline.work / 'inputs/json/en/0.json')
        self.fingerprint = self.cache.scope_fingerprint('0', self.area)
        self.admitted()
        pipeline, cache = self.next_run()
        pipeline.state['runnerPreflight'] = {'status': 'passed'}
        pipeline.ensure_live_model = Mock()
        pipeline.freeze_source_corpus = Mock()
        for scope in [*HEX[1:], 'legal']:
            write(pipeline.work / 'discovery' / (scope + '.json'), {
                'sources': {}, 'concerns': [], 'queries': [], 'limitations': []})
        server = Mock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        server.discover.return_value = {'allowedTools': ['searxng_search', 'websearch_search'],
                                        'openCodeVersion': 'synthetic'}
        server.redact.side_effect = lambda value: value
        pipeline.server = Mock(return_value=server)
        with patch('ci.source_research.research_scope') as researcher, \
             patch('ci.pipeline.fetch_source_snapshot') as fetcher:
            pipeline.execute('prepare')
        researcher.assert_not_called()
        fetcher.assert_not_called()
        server.ask.assert_not_called()
        pipeline.freeze_source_corpus.assert_called_once()
        sources = pipeline.freeze_source_corpus.call_args.args[0]
        self.assertEqual(set(sources), {'0-source-1', '0-source-2'})
        self.assertEqual(pipeline.state['sourceCache']['scopesReused'], 1)

    def test_multilingual_passages_retain_original_text_and_source_retrieval_dates(self):
        originals=['Texte français synthétique pour vérifier les sauvegardes et la restauration.',
                   'Αρχικό συνθετικό ελληνικό κείμενο για την επαλήθευση αντιγράφων ασφαλείας.']
        for index,lang in enumerate(('fr','el')):
            self.server.discovery['sources'][index]['language']=lang
            self.responses.contents[URLS[index]]=originals[index]
            self.server.evidence['sources'][index]['passages'][0]['text']=originals[index]
        corpus=self.admitted()
        for index,lang in enumerate(('fr','el')):
            values=list(query_records(self.cache.outbox,language_filter=lang,kind='source-passage',scope='0'))
            self.assertEqual(len(values),1)
            self.assertEqual(values[0]['originalText'],originals[index])
            self.assertEqual(values[0]['quotation'],originals[index])
            self.assertEqual(values[0]['retrievedAt'],corpus['sources'][f'0-source-{index+1}']['retrievedAt'])
            self.assertEqual(values[0]['filters']['status'],'passage-checked')

    def test_preflight_archive_reuses_fetched_document_without_assuming_directory_name(self):
        corpus=self.admitted()
        pipeline,cache=self.next_run()
        archive=pipeline.root/'preflight-snapshots'
        with patch('ci.pipeline.fetch_source_snapshot',side_effect=AssertionError('unexpected network')):
            result=cache.fetch_snapshot(metadata(URLS[0]),archive,time.monotonic()+900)
        self.assertEqual(byte_digest(archive/result['textPath']),corpus['sources']['0-source-1']['textDigest'])
        self.assertNotIn('passages',result)

if __name__ == '__main__':
    unittest.main()
