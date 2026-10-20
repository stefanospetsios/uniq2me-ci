"""Retrieval modes tested with synthetic agents and temporary local Git remotes."""
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import byte_digest, file_manifest, read, write
from ci.dry_run import candidate, synthetic_review
from ci.metrics import collect
from ci.pipeline import Pipeline, STAGES, command
from ci.rag import prepare_repository_sources, policy, runtime_config, validate_mode
from ci.runtime import OpenCode
from ci.smoke_test import create_fixture_pipeline

ENV = {'PUSH_TOKEN': 'synthetic-local-token', 'DRY_RUN': 'false',
       'OLLAMA_BASE_URL': 'http://fixture', 'GITHUB_OUTPUT': '',
       'X': '2', 'Y': '3', 'Z': '5', 'REVIEW_MINUTES': '2',
       'SOURCE_MINUTES': '15', 'MASK_MINUTES': '30', 'PREFLIGHT_MINUTES': '30',
       'CATALOG_GRACE_SECONDS': '30', 'MAX_ATTEMPTS': '2'}
MODEL = {'tag': 'qwen3:235b', 'digest': 'a' * 64, 'preflightStatus': 'passed',
    'effectiveContext': 262144, 'nativeContext': 262144, 'observedContext': 262144,
    'explicitContextFallback': False, 'fallbackReason': None, 'nativeToolCallingSupported': True,
    'ollamaVersion': None, 'ollamaVersionStatus': 'unknown-synthetic-test'}


class SyntheticDraftServer:
    """Exercise real controller branches without invoking a model or external MCP."""
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def bind(self, value):
        sources = self.pipeline.frozen_sources()
        source = next(iter(sources.values()), None)
        refs = [{'sourceId': source['id'], 'passageId': source['passages'][0]['id']}] if source else []
        def visit(v):
            if isinstance(v, list):
                return [visit(x) for x in v]
            if not isinstance(v, dict):
                return v
            v = {k: visit(x) for k, x in v.items()}
            if 'sourceRefs' in v:
                v['sourceRefs'] = refs
            if 'concernIds' in v:
                v['concernIds'] = [v['identity']['sfsId'] + '-concern-topic-boundaries']
            if 'wikiLinks' in v:
                v['wikiLinks'] = [{'key1': 'Synthetic repository reference', 'key2': source['url']}] if source else []
            if 'references' in v:
                v['references'] = [{'title': 'Synthetic repository reference', 'url': source['url']}] if source else []
            return v
        return visit(value)

    def ask(self, role, prompt, seconds, call_id, **kwargs):
        if call_id.startswith('roster-'):
            payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"roster":'):])
            return self.bind(synthetic_review(payload['roster'], payload['requiredChecks']))
        if call_id.endswith('-review'):
            payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"candidate":'):])
            return self.bind(synthetic_review(payload['candidate'], payload['requiredChecks'], payload['fixtures']))
        for stage in sorted([*STAGES, 'solutions'], key=len, reverse=True):
            if call_id.startswith(stage + '-'):
                key = re.fullmatch(r'(.+)-\d+-author', call_id[len(stage) + 1:])[1]
                if stage == 'catalog':
                    payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"area":'):])
                    scope = next(s for s in self.pipeline.state['slots'] if s['repoId'] == key)['sfsId']
                    expected = {c['id'] for c in read(self.pipeline.work / 'frozen/concerns.json')
                                if c['sfsId'] == scope}
                    assert set(payload['allowedConcernIds']) == expected
                    assert payload['area']['keyDescription']
                return self.bind(candidate(self.pipeline, stage, key))
        raise AssertionError('Unexpected synthetic call: ' + call_id)


class RagTests(unittest.TestCase):
    def test_invalid_mode_is_not_a_silent_fallback(self):
        with self.assertRaisesRegex(ValueError, 'RAG_MODE'):
            validate_mode('auto')

    def test_fresh_initialization_ignores_prior_bases_and_has_unique_attempt_namespace(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                **ENV, 'RAG_MODE': 'repo-only', 'FRESH_START': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                'METHODOLOGY_FILE': str(ROOT/'agent-rules/tests/fixtures/methodology.tex'),
                'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '2', 'RELEASE': '261013',
                'TARGET_BRANCH': '', 'BASE_CATALOG_MODE': 'existing', 'BASE_MASK_MODE': 'existing'}), \
             redirect_stdout(io.StringIO()):
            fixture, _ = create_fixture_pipeline(directory)
            input_repo = Path(directory) / 'uniq2me'
            shutil.copytree(fixture.work / 'inputs', input_repo)
            command(['git', 'init', '-q'], input_repo)
            command(['git', 'add', '.'], input_repo)
            command(['git', '-c', 'user.name=CI fixture', '-c', 'user.email=fixture@example.invalid',
                     'commit', '-qm', 'Synthetic pinned templates'], input_repo)
            command(['git', 'remote', 'add', 'origin', 'https://github.com/stefanospetsios/uniq2me.git'], input_repo)
            command(['git', 'remote', 'set-url', 'origin',
                     'https://github.com/stefanospetsios/uniq2me-ai.git'], fixture.output)
            write(fixture.output / 'next-base.json', {'invalid': 'must not inspect this prior base'})
            command(['git', 'add', 'next-base.json'], fixture.output)
            command(['git', '-c', 'user.name=CI fixture', '-c', 'user.email=fixture@example.invalid',
                     'commit', '-qm', 'Synthetic prior pointer'], fixture.output)
            pipeline = Pipeline()
            pipeline.workspace, pipeline.output = Path(directory), fixture.output
            pipeline.root = Path(directory) / 'fresh-attempt'
            pipeline.work, pipeline.state_path = pipeline.root / 'work', pipeline.root / 'controller-state.json'
            pipeline.state = {}
            with patch('ci.pipeline.remote_head', return_value=None):
                pipeline.initialize()
            self.assertEqual(pipeline.state['runId'], 'qwen3-42-attempt-2')
            self.assertTrue(pipeline.state['freshStart'])
            self.assertEqual(pipeline.state['baseModes'], {'catalog': 'empty', 'masks': 'empty'})
            self.assertFalse((pipeline.work / 'inputs/prior-release').exists())

    def test_failed_configuration_gate_does_not_mark_corpus_completed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                **ENV, 'RAG_MODE': 'repo-only'}), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory)
            pipeline.state.update(dryRun=False, ragMode='repo-only', model=dict(MODEL))
            data = prepare_repository_sources(pipeline)
            with patch('ci.pipeline.command', side_effect=RuntimeError('Synthetic invalid runtime config')):
                with self.assertRaisesRegex(RuntimeError, 'invalid runtime config'):
                    pipeline.freeze_source_corpus(*data)
            self.assertNotIn('frozenManifest', pipeline.state)
            pipeline.pinned()  # Partial state remains internally consistent for a retry.
            with patch('ci.pipeline.command', return_value=Mock(stdout='Synthetic valid config')):
                pipeline.freeze_source_corpus(*data)
            self.assertIn('frozenManifest', pipeline.state)

    def test_non_web_preparation_preserves_the_verified_text_only_model_path(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{**ENV,'RAG_MODE':'disabled'}),redirect_stdout(io.StringIO()):
            pipeline,_=create_fixture_pipeline(directory)
            pipeline.state.update(dryRun=False,ragMode='disabled',
                model={**MODEL,'nativeToolCallingSupported':False,'textOnlyReview':True})
            with patch('ci.pipeline.command',return_value=Mock(stdout='Synthetic valid config')):
                pipeline.freeze_source_corpus(*prepare_repository_sources(pipeline))
            primary=read(pipeline.work/'agent-rules/run-config.json')['models']['primary']
            self.assertFalse(primary['nativeToolCallingSupported'])
            self.assertTrue(primary['textOnlyReview'])

    def test_non_web_config_explicitly_disables_remote_servers_and_permissions(self):
        config = read(ROOT / 'opencode.json')
        for mode in ('disabled', 'repo-only'):
            selected = runtime_config(config, mode)
            self.assertTrue(all(not c['enabled'] for c in selected['mcp'].values()))
            self.assertTrue(all(c['enabled'] for c in config['mcp'].values()))
            with tempfile.TemporaryDirectory() as directory:
                server = OpenCode(directory, ROOT, selected, directory, MODEL)
                server.version = 'synthetic'
                server.api = lambda path, **kw: [] if path.startswith('/experimental/tool') else {
                    name: {'status': 'connected'} for name in config['mcp']} if path == '/mcp' else {}
                server.stop, server.start = Mock(), Mock()
                with patch('ci.mcp_schemas.discover_mcp_tools', side_effect=AssertionError('Remote discovery')):
                    self.assertEqual(server.discover()['allowedTools'], [])

    def test_non_web_preflight_does_not_call_search_or_fetch_documents(self):
        for mode in ('disabled', 'repo-only'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {**ENV, 'RAG_MODE': mode}), redirect_stdout(io.StringIO()):
                pipeline, _ = create_fixture_pipeline(directory)
                pipeline.state.update(dryRun=False, ragMode=mode, model=MODEL)
                pipeline.save()
                server = Mock()
                server.__enter__, server.__exit__ = Mock(return_value=server), Mock(return_value=False)
                server.discover.return_value = {'allowedTools': [], 'openCodeVersion': 'synthetic'}
                server.ask.return_value = {'status': 'ready'}
                pipeline.server = Mock(return_value=server)
                pipeline.ensure_live_model = Mock()
                with patch('ci.preflight.runner_checks', return_value={'synthetic': True}), \
                     patch('ci.pipeline.probe_search_tools', side_effect=AssertionError('External search')), \
                     patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('External source fetch')):
                    pipeline.execute('preflight')
                report = pipeline.state['runnerPreflight']
                self.assertEqual(report['status'], 'passed')
                self.assertFalse(report['realMcpTested'])
                self.assertEqual(report['checks']['sourceAdmission'], 'skipped-rag-mode')
                self.assertNotIn('required_tool_groups', server.ask.call_args.kwargs)

    def test_repository_mode_does_not_dereference_web_citations(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory)
            pipeline.state.update(dryRun=False, ragMode='repo-only')
            with patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('External fetch')):
                sources, concerns, queries, limitations = prepare_repository_sources(pipeline)
            self.assertTrue(sources)
            self.assertEqual(len(concerns), 16)
            self.assertEqual(queries, [])
            self.assertTrue(limitations)
            self.assertTrue(all(s['sourceStatus'] == 'pinned-repository-text-not-externally-verified'
                                for s in sources.values()))
            for source in sources.values():
                self.assertEqual(source['byteDigest'], byte_digest(pipeline.work / source['inputPath']))

    def test_disabled_empty_sources_cannot_be_mislabeled_as_grounded(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory)
            pipeline.state.update(dryRun=False, ragMode='disabled')
            sources, concerns, queries, limitations = prepare_repository_sources(pipeline)
            self.assertEqual(sources, {})
            self.assertTrue(all(c['sourceRefs'] == [] for c in concerns))
            self.assertEqual(policy('disabled')['outputPolicy'], 'unverified-draft-only')

    def test_each_non_web_mode_publishes_five_draft_commits_without_advancing_accepted_pointer(self):
        for mode in ('disabled', 'repo-only'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {**ENV, 'RAG_MODE': mode}), redirect_stdout(io.StringIO()):
                pipeline, bare = create_fixture_pipeline(directory)
                accepted_pointer = {'recordType': 'synthetic-existing-accepted-pointer'}
                write(pipeline.output / 'next-base.json', accepted_pointer)
                command(['git', 'add', 'next-base.json'], pipeline.output)
                command(['git', '-c', 'user.name=CI fixture', '-c', 'user.email=fixture@example.invalid',
                         'commit', '--amend', '-qm', 'Synthetic draft base'], pipeline.output)
                pipeline.state.update(dryRun=False, ragMode=mode, model=dict(MODEL),
                    openCodeVersion='synthetic-agent-scaffold',
                    outputBaseCommit=command(['git', 'rev-parse', 'HEAD'], pipeline.output).stdout.strip(),
                    runnerPreflight={'status': 'passed'})
                config = read(pipeline.work / 'agent-rules/run-config.json')
                config['rag'] = policy(mode)
                config['sourceDiscovery']['enabled'] = False
                write(pipeline.work / 'agent-rules/run-config.json', config)
                pipeline.state['rulesManifest'] = file_manifest(pipeline.work / 'agent-rules')
                pipeline.save()
                pipeline.ensure_live_model = Mock()
                pipeline.server = lambda stage: SyntheticDraftServer(pipeline)
                def local_command(args, cwd, **kwargs):
                    if args[0] == sys.executable:
                        return Mock(stdout='Synthetic configuration scaffold; no live claim')
                    return command(args, cwd, **kwargs)
                with patch('ci.pipeline.command', side_effect=local_command), \
                     patch('ci.pipeline.fetch_source_snapshot', side_effect=AssertionError('External fetch')), \
                     patch('ci.pipeline.probe_search_tools', side_effect=AssertionError('External MCP')):
                    pipeline.execute('prepare')
                    for stage in STAGES:
                        pipeline.execute(stage)
                        pipeline.publish(stage)
                self.assertEqual(len(pipeline.state['publications']), 5)
                self.assertEqual(command(['git', 'rev-list', '--count', pipeline.state['branch']], bare).stdout.strip(), '6')
                self.assertEqual(read(pipeline.output / 'next-base.json'), accepted_pointer)
                self.assertTrue((pipeline.output / 'draft-base.json').exists())
                manifest = read(pipeline.output / 'release-manifest.json')
                self.assertEqual(manifest['status'], 'unverified-draft')
                self.assertEqual(manifest['rag']['mode'], mode)
                marker = read(pipeline.output / 'pipeline-status.json')
                self.assertTrue(marker['draftStagesComplete'])
                self.assertFalse(marker['multilingualComplete'])
                self.assertTrue(marker['rootViewsAreUnverifiedDrafts'])
                self.assertEqual(collect(pipeline.root, pipeline.state)['progress']['authoritativeSources'], 0)
                if mode == 'disabled':
                    self.assertEqual(read(pipeline.output / 'catalog/questions/en/0000.json')['sourceRefs'], [])
                    self.assertEqual(read(pipeline.output / 'repo-sf/en/0000.json')['wikiLinks'], [])


if __name__ == '__main__':
    unittest.main()
