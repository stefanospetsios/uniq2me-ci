"""Runner preflight blocks bad prerequisites and preserves five publications."""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import read, write
from ci.pipeline import command, remote_head
from ci.preflight import PILOT_URL, runner_checks
from ci.runtime import OpenCode
from ci.smoke_test import create_fixture_pipeline, restore_on_fresh_runner
from ci.checkpoints import seal

ENV = {'PUSH_TOKEN': 'synthetic-local-token', 'DRY_RUN': 'true',
       'OLLAMA_BASE_URL': 'http://fixture', 'GITHUB_OUTPUT': ''}
MODEL = {'tag': 'qwen3:235b', 'digest': 'a' * 64, 'preflightStatus': 'passed', 'effectiveContext': 262144}


class PreflightTests(unittest.TestCase):
    def test_missing_opencode_is_caught_before_loading_model(self):
        with tempfile.TemporaryDirectory() as directory, patch('ci.preflight.shutil.which',
                side_effect=lambda name: None if name == 'opencode' else '/synthetic/' + name):
            with self.assertRaisesRegex(ValueError, 'Missing runner programs: opencode'):
                runner_checks(directory, True)

    def test_dry_preflight_has_zero_model_calls_and_no_commits(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), \
             patch('ci.preflight.runner_checks', return_value={'synthetic': True}), \
             patch('ci.pipeline.ollama_preflight', side_effect=AssertionError('Dry preflight loaded a model')), \
             patch('ci.runtime.OpenCode.start', side_effect=AssertionError('Dry preflight started OpenCode')), \
             redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory)
            pipeline.execute('preflight')
            report = read(pipeline.root / 'runner-preflight.json')
            self.assertEqual(report['status'], 'passed')
            self.assertFalse(report['realInferenceTested'])
            self.assertFalse(report['realMcpTested'])
            self.assertEqual(pipeline.state['publications'], [])
            self.assertIsNone(remote_head(pipeline.output, pipeline.state['branch']))
            seal(pipeline)
            restored = restore_on_fresh_runner(pipeline, Path(directory) / 'prepare-runner')
            restored.execute('prepare')
            restored.execute('catalog')
            restored.publish('catalog')
            self.assertEqual(len(restored.state['publications']), 1)

    def test_git_receive_probe_does_not_create_the_target_branch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory)
            pipeline.check_publication_access()
            self.assertIsNone(remote_head(pipeline.output, pipeline.state['branch']))
            self.assertEqual(command(['git', 'rev-list', '--count', 'HEAD'], pipeline.output).stdout.strip(), '1')

    def test_live_preflight_requires_agent_searches_and_independent_source_admission(self):
        for admits_source in (True, False):
            with self.subTest(admits=admits_source), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, ENV), patch('ci.preflight.runner_checks', return_value={'synthetic': True}), \
                 patch('ci.pipeline.ollama_preflight', return_value=dict(MODEL)), redirect_stdout(io.StringIO()):
                pipeline, _ = create_fixture_pipeline(directory)
                pipeline.state['dryRun'] = False
                pipeline.save()
                server = Mock()
                server.__enter__ = Mock(return_value=server)
                server.__exit__ = Mock(return_value=False)
                server.runtime_mcp = {'searxng': {'type': 'remote', 'url': 'http://fixture/searxng'},
                                      'websearch': {'type': 'remote', 'url': 'http://fixture/websearch'}}
                server.sensitive = []
                server.redact = lambda value: value
                server.discover.return_value = {'allowedTools': ['searxng_web_search', 'websearch_web_fetch'],
                                                 'openCodeVersion': 'synthetic-version'}
                server.ask.return_value = {'status': 'ready', 'source': {'url': 'https://docs.ollama.com/faq',
                    'passages': [{'id': 'p1', 'locator': 'test', 'text': 'Synthetic source admission test passage'}]}}
                pipeline.server = Mock(return_value=server)
                def snapshot(source, archive, deadline):
                    archive.mkdir(parents=True, exist_ok=True)
                    (archive/'pilot.txt').write_text('Synthetic source admission test passage' if admits_source else
                                                   'A different independently fetched passage', encoding='utf-8')
                    return {**source, 'byteDigest':'sha256:'+'b'*64, 'textDigest':'sha256:'+'c'*64,
                            'textPath':'pilot.txt'}
                fetch = Mock(side_effect=snapshot)
                def ask(*args, **kwargs):
                    value = server.ask.return_value
                    return kwargs['answer_validator'](value, [])
                server.ask.side_effect = ask
                with patch('ci.pipeline.fetch_source_snapshot', fetch), patch('ci.pipeline.probe_search_tools',
                        return_value={'searxng': {'status': 'passed'}, 'websearch': {'status': 'passed'}}):
                    if admits_source:
                        pipeline.execute('preflight')
                    else:
                        with self.assertRaisesRegex(ValueError, 'not found in independently fetched'):
                            pipeline.execute('preflight')
                groups = server.ask.call_args.kwargs['required_tool_groups']
                self.assertEqual(groups, {'searxng': ['searxng_web_search'], 'websearch': ['websearch_web_fetch']})
                fetch.assert_called_once()
                self.assertEqual(pipeline.state['runnerPreflight']['status'], 'passed' if admits_source else 'failed')
                self.assertEqual(pipeline.state['publications'], [])
                self.assertIsNone(remote_head(pipeline.output, pipeline.state['branch']))

    def test_verified_resident_model_is_reused_without_loading_it_again(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory)
            pipeline.state['model'] = dict(MODEL)
            pipeline.state['runnerPreflight'] = {'status': 'passed', 'ollamaBaseUrl': 'http://fixture'}
            tags = {'models': [{'name': 'qwen3:235b', 'digest': 'a' * 64}]}
            ps = {'models': [{'digest': 'a' * 64, 'context_length': 262144}]}
            with patch('ci.pipeline.http_json', side_effect=[tags, ps]), \
                 patch('ci.pipeline.ollama_preflight') as cold_load:
                pipeline.ensure_live_model(900)
                cold_load.assert_not_called()
            with patch('ci.pipeline.http_json', side_effect=[tags, {'models': []}]), \
                 patch('ci.pipeline.ollama_preflight', return_value=dict(MODEL)) as cold_load:
                pipeline.ensure_live_model(900)
                cold_load.assert_called_once()

    def test_direct_search_failure_blocks_the_agent_smoke_task(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), \
             patch('ci.preflight.runner_checks', return_value={'synthetic': True}), \
             patch('ci.pipeline.ollama_preflight', return_value=dict(MODEL)), \
             patch('ci.pipeline.probe_search_tools', return_value={
                 'searxng': {'status': 'passed'},
                 'websearch': {'status': 'failed', 'execution': {'isError': True}}}), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory)
            pipeline.state['dryRun'] = False
            pipeline.save()
            server = Mock()
            server.__enter__ = Mock(return_value=server)
            server.__exit__ = Mock(return_value=False)
            server.runtime_mcp = {'searxng': {}, 'websearch': {}}
            server.sensitive, server.redact = [], lambda value: value
            server.discover.return_value = {'allowedTools': ['searxng_web_search', 'websearch_web_search'],
                                             'openCodeVersion': 'synthetic'}
            pipeline.server = Mock(return_value=server)
            with self.assertRaisesRegex(ValueError, 'Direct MCP search execution failed'):
                pipeline.execute('preflight')
            server.ask.assert_not_called()
            self.assertEqual(pipeline.state['runnerPreflight']['checks']['mcpToolExecution'], 'failed')
            self.assertEqual(read(pipeline.root / 'preflight-mcp-tool-execution.json')['websearch']['status'], 'failed')

    def test_live_preparation_is_blocked_without_a_passed_preflight(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory)
            pipeline.state['dryRun'] = False
            pipeline.save()
            with patch('ci.pipeline.ollama_preflight') as load:
                with self.assertRaisesRegex(ValueError, 'requires a passed runner preflight'):
                    pipeline.execute('prepare')
                load.assert_not_called()


class SourcePilotTests(unittest.TestCase):
    """Actual preflight/session/quotation path using synthetic HTTP and model responses."""
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {**ENV, 'PREFLIGHT_MINUTES':'30'}))
        self.stack.enter_context(patch('ci.preflight.runner_checks', return_value={'synthetic':True}))
        self.stack.enter_context(patch('ci.pipeline.ollama_preflight', return_value=dict(MODEL)))
        self.stack.enter_context(patch('ci.pipeline.probe_search_tools', return_value={
            'searxng':{'status':'passed'}, 'websearch':{'status':'passed'}}))
        self.pipeline, _ = create_fixture_pipeline(directory, publish=False)
        self.pipeline.state.update(dryRun=False, maximumAttempts=2)
        self.pipeline.state['timeoutsMinutes']['Z'] = 1
        self.pipeline.save()
        self.source_text = ('By default, Ollama uses a context window size of 4096 tokens.\n\n'
                            'This can be overridden with the \nOLLAMA_CONTEXT_LENGTH\n environment variable.')
        self.quote = ('By default, Ollama uses a context window size of 4096 tokens. '
                      'This can be overridden with the OLLAMA_CONTEXT_LENGTH environment variable.')
        self.replies, self.posts, self.messages, self.sessions = [], [], [], []
        self.server = OpenCode(self.pipeline.work, ROOT, {}, self.pipeline.root/'runtime/preflight/1', MODEL)
        self.server.runtime_mcp = {'searxng':{}, 'websearch':{}}
        self.server.discover = lambda: {'allowedTools':['searxng_web_search','websearch_web_search'],
                                       'openCodeVersion':'synthetic'}
        self.server.check_residency = lambda: None
        def api(path, data=None, **kwargs):
            if path == '/session':
                self.sessions.append(data)
                return {'id':'synthetic-pilot'}
            if path == '/session/status':
                return {}
            if path.endswith('/prompt_async'):
                self.posts.append(data)
                parts = [{'type':'text','text':json.dumps(self.replies.pop(0))}]
                if len(self.posts) == 1:
                    parts += [{'type':'tool','tool':tool,'state':{'status':'completed',
                        'input':{'query':'Synthetic FAQ search'},'output':json.dumps({'url':PILOT_URL})}}
                        for tool in ('searxng_web_search','websearch_web_search')]
                self.messages.append({'info':{'role':'assistant','providerID':'ollama','modelID':MODEL['tag'],
                    'time':{'completed':1},'finish':'stop','tokens':{'total':10}}, 'parts':parts})
            if path.endswith('/message'):
                return self.messages
            return None
        self.server.api = api
        manager = Mock()
        manager.__enter__ = Mock(return_value=self.server)
        manager.__exit__ = Mock(return_value=False)
        self.pipeline.server = Mock(return_value=manager)
        self.fetch = self.stack.enter_context(patch('ci.pipeline.fetch_source_snapshot', side_effect=self.snapshot))

    def snapshot(self, source, archive, deadline):
        data = self.source_text.encode('utf-8')
        archive.mkdir(parents=True, exist_ok=True)
        (archive/'pilot.txt').write_bytes(data)
        (archive/'pilot.bin').write_bytes(data)
        checksum = 'sha256:' + hashlib.sha256(data).hexdigest()
        return {**source, 'byteDigest':checksum, 'textDigest':checksum, 'textPath':'pilot.txt',
                'snapshotPath':'pilot.bin', 'sourceStatus':'fetched-awaiting-passage-check'}

    def reply(self, quotation, url=PILOT_URL):
        return {'status':'ready','source':{'url':url,
            'passages':[{'id':'p1','locator':'Context window size','text':quotation}]}}

    def envelope(self):
        return read(self.server.archive/'calls/runner-smoke/envelope.json')

    def test_added_markdown_is_corrected_in_same_session_against_one_snapshot(self):
        invalid = self.reply(self.quote.replace('OLLAMA_CONTEXT_LENGTH','`OLLAMA_CONTEXT_LENGTH`'))
        self.replies = [invalid, self.reply(self.quote)]
        head = command(['git','rev-parse','HEAD'], self.pipeline.output).stdout
        self.pipeline.execute('preflight')
        self.fetch.assert_called_once()
        self.assertEqual(self.fetch.call_args.args[0]['url'],PILOT_URL)
        self.assertEqual(self.fetch.call_args.args[0]['language'],'en')
        self.assertEqual(len(self.sessions),1)
        self.assertEqual(len(self.posts),2)
        self.assertIn('localTextPath',self.posts[0]['parts'][0]['text'])
        self.assertIn('OLLAMA_CONTEXT_LENGTH\\n environment',self.posts[0]['parts'][0]['text'])
        self.assertIn('passageFailures',self.posts[1]['parts'][0]['text'])
        self.assertEqual(self.envelope()['answerRejections'],1)
        self.assertEqual(self.envelope()['reportedTokenUsage']['total'],20)
        rejection = read(self.server.archive/'calls/runner-smoke/answer-rejection-1.json')
        self.assertEqual(json.loads(rejection['answer']),invalid)
        self.assertEqual(rejection['details']['phase'],'preflight-evidence')
        self.assertTrue(rejection['details']['passageFailures'][0]['sourceExcerpt'])
        self.assertEqual(self.pipeline.state['runnerPreflight']['checks']['sourceAdmission'],'passed')
        self.assertEqual(self.pipeline.state['runnerPreflight']['status'],'passed')
        self.assertEqual(read(self.pipeline.root/'preflight-source-snapshot.json')['textPath'],'pilot.txt')
        self.assertEqual(command(['git','rev-parse','HEAD'], self.pipeline.output).stdout,head)
        self.assertEqual(self.pipeline.state['publications'],[])

    def test_changed_facts_exhaust_corrections_and_still_block_preflight(self):
        self.replies = [self.reply(self.quote.replace('4096','8192'))]*3
        with self.assertRaisesRegex(ValueError,'not found in independently fetched'):
            self.pipeline.execute('preflight')
        self.fetch.assert_called_once()
        self.assertEqual(len(self.posts),3)
        self.assertEqual(self.envelope()['status'],'failed')
        self.assertEqual(self.envelope()['answerRejections'],3)
        checks = self.pipeline.state['runnerPreflight']['checks']
        self.assertEqual(checks['agentJsonAndToolUse'],'passed')
        self.assertEqual(checks['sourceAdmission'],'failed')
        self.assertEqual(self.pipeline.state['runnerPreflight']['status'],'failed')
        self.assertEqual(self.pipeline.state['publications'],[])

    def test_source_fetch_and_corrections_share_original_task_deadline(self):
        clock = [0]
        def fetch(*args):
            clock[0] = 5
            return self.snapshot(*args)
        self.fetch.side_effect = fetch
        def finish_response():
            clock[0] = 61
        self.server.check_residency = finish_response
        self.replies = [self.reply(self.quote.replace('OLLAMA_CONTEXT_LENGTH','`OLLAMA_CONTEXT_LENGTH`'))]
        with patch('ci.pipeline.time.monotonic',side_effect=lambda:clock[0]), \
             self.assertRaisesRegex(ValueError,'not found in independently fetched'):
            self.pipeline.execute('preflight')
        self.assertEqual(len(self.posts),1)
        self.assertEqual(self.envelope()['timeoutSeconds'],55)
        self.assertEqual(self.fetch.call_args.args[2],60)

    def test_unlimited_preflight_preserves_null_timeout_and_bounded_corrections(self):
        self.pipeline.state['timeoutsMinutes']['Z'] = 0
        self.replies = [self.reply(self.quote.replace('4096','8192')),self.reply(self.quote)]
        with patch.dict(os.environ,{'PREFLIGHT_MINUTES':'0'}):
            self.pipeline.execute('preflight')
        self.assertIsNone(self.envelope()['timeoutSeconds'])
        self.assertFalse(self.envelope()['timeLimitEnabled'])
        self.assertEqual(self.envelope()['answerRepairLimit'],2)
        self.assertEqual(len(self.posts),2)

    def test_unavailable_snapshot_defers_source_pilot_and_still_checks_model_and_tools(self):
        self.fetch.side_effect = OSError('Synthetic document retrieval failure')
        self.replies = [{'status':'ready'}]
        self.pipeline.execute('preflight')
        self.assertEqual(len(self.posts),1)
        checks = self.pipeline.state['runnerPreflight']['checks']
        self.assertEqual(checks['sourceAdmission'],'pending-download')
        self.assertEqual(checks['agentJsonAndToolUse'],'passed')
        self.assertEqual(self.pipeline.state['runnerPreflight']['status'],'passed')
        self.assertNotIn('sourceByteDigest',self.pipeline.state['runnerPreflight'])

    def test_model_cannot_substitute_another_document(self):
        self.replies = [self.reply(self.quote,'https://docs.ollama.com/other')]*3
        with self.assertRaisesRegex(ValueError,'requested official document'):
            self.pipeline.execute('preflight')
        self.fetch.assert_called_once()
        self.assertEqual(self.pipeline.state['runnerPreflight']['status'],'failed')
        self.assertEqual(len(self.posts),3)


if __name__ == '__main__':
    unittest.main()
