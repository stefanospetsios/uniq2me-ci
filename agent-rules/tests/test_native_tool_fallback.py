"""Native tool negotiation against synthetic provider responses only."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ci.contracts import read
from ci.rag import runtime_config
from ci.refinement import RefinementPipeline
from ci.runtime import NativeToolCallUnavailable, OpenCode, ollama_preflight

ROOT = Path(__file__).resolve().parents[2]
TAG = 'deepseek-r1:70b-llama-distill-q8_0'
PROSE = {'message': {'role': 'assistant', 'content': 'Synthetic tool description'},
         'done_reason': 'stop', 'eval_count': 585}
NATIVE = {'message': {'tool_calls': [{'function': {'name': 'preflight_probe',
                                                'arguments': {'value': 'ready'}}}]},
          'done_reason': 'stop'}


class NativeToolFallbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = Path(self.directory.name)
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.response, self.calls, self.ps_calls = PROSE, [], 0
        self.changed_context, self.tools_advertised = False, True
        api = patch('ci.runtime.http_json', side_effect=self.api)
        api.start()
        self.addCleanup(api.stop)

    def api(self, url, data=None, **kwargs):
        self.calls.append(url)
        if url.endswith('/api/version'):
            return {'version': 'synthetic-version'}
        if url.endswith('/api/tags'):
            return {'models': [{'name': TAG, 'digest': 'a' * 64}]}
        if url.endswith('/api/show'):
            return {'model_info': {'general.architecture': 'synthetic',
                                   'synthetic.context_length': 262144},
                    'capabilities': ['completion', 'tools'] if self.tools_advertised else ['completion']}
        if url.endswith('/api/chat'):
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        if url.endswith('/api/generate'):
            return {'response': 'Synthetic ready'}
        if url.endswith('/api/ps'):
            self.ps_calls += 1
            context = 4096 if self.changed_context and self.ps_calls == 2 else 262144
            return {'models': [{'digest': 'a' * 64, 'context_length': context}]}
        if url.endswith('/v1/chat/completions'):
            return {'choices': [{'message': {'role': 'assistant', 'content': 'Synthetic ready'}}]}
        raise AssertionError('Unexpected synthetic route: ' + url)

    def preflight(self, require_tools=False):
        return ollama_preflight('http://fixture', self.archive, tag=TAG,
                                native_context=True, require_tools=require_tools)

    def test_advertised_tools_with_prose_fall_back_and_preserve_probe_and_context_checks(self):
        record = self.preflight()
        self.assertEqual(record['preflightStatus'], 'passed')
        self.assertFalse(record['nativeToolCallingSupported'])
        self.assertTrue(record['textOnlyReview'])
        probe = record['nativeToolProbe']
        self.assertEqual(probe['status'], 'unavailable-text-only-fallback')
        self.assertIn(TAG, probe['failure'])
        self.assertNotIn('Qwen3', probe['failure'])
        self.assertEqual(read(self.archive / probe['archivePath'] / '1-response.json'), PROSE)
        self.assertEqual(read(self.archive / probe['archivePath'] / 'attempts.json'), probe['attempts'])
        self.assertTrue(record['openAiCompatibleAllocationVerified'])
        self.assertEqual(self.ps_calls, 2)
        self.assertNotIn('http://fixture/api/generate', self.calls)

    def test_required_web_tools_still_fail_with_actual_model_name(self):
        with self.assertRaisesRegex(NativeToolCallUnavailable, TAG):
            self.preflight(require_tools=True)
        record = read(self.archive / 'preflight.json')
        self.assertEqual(record['preflightStatus'], 'failed')
        self.assertNotIn('textOnlyReview', record)
        self.assertNotIn('http://fixture/v1/chat/completions', self.calls)

    def test_successful_native_probe_keeps_tools_enabled(self):
        self.response = NATIVE
        record = self.preflight()
        self.assertTrue(record['nativeToolCallingSupported'])
        self.assertFalse(record['textOnlyReview'])

    def test_no_advertised_tools_keeps_existing_text_only_warmup(self):
        self.tools_advertised = False
        record = self.preflight()
        self.assertTrue(record['textOnlyReview'])
        self.assertIn('http://fixture/api/generate', self.calls)
        self.assertNotIn('http://fixture/api/chat', self.calls)

    def test_transport_authentication_and_malformed_responses_remain_fatal(self):
        for response in (HTTPError('http://fixture/api/chat', 401, 'Synthetic unauthorized', {}, None),
                         HTTPError('http://fixture/api/chat', 500, 'Synthetic provider error', {}, None),
                         TimeoutError('Synthetic timeout'), {'error': 'Synthetic malformed response'}):
            with self.subTest(response=response):
                self.response = response
                with self.assertRaises((HTTPError, TimeoutError, ValueError)) as caught:
                    self.preflight()
                self.assertNotIsInstance(caught.exception, NativeToolCallUnavailable)
                self.assertEqual(read(self.archive / 'preflight.json')['preflightStatus'], 'failed')
                self.assertNotIn('http://fixture/v1/chat/completions', self.calls)

    def test_bounded_truncation_can_fall_back_without_claiming_tool_support(self):
        self.response = {'message': {'thinking': 'Synthetic unfinished reasoning'}, 'done_reason': 'length'}
        record = self.preflight()
        self.assertTrue(record['textOnlyReview'])
        self.assertEqual([a['numPredict'] for a in record['nativeToolProbe']['attempts']], [2048, 8192])

    def test_context_mismatch_after_fallback_still_blocks_preflight(self):
        self.changed_context = True
        with self.assertRaisesRegex(ValueError, 'different server default context'):
            self.preflight()
        self.assertEqual(read(self.archive / 'preflight.json')['preflightStatus'], 'failed')

    def refinement(self, mode='repo-only', previous=None):
        pipeline = RefinementPipeline.__new__(RefinementPipeline)
        pipeline.root = self.archive
        pipeline.state = {'activeModelLabel': 'c', 'refinementPlan': {'models': {'c': TAG}},
                          'modelRecords': {TAG: previous} if previous else {}, 'ragMode': mode}
        pipeline.save, pipeline.check_model_route = Mock(), Mock()
        return pipeline

    def test_refinement_still_probes_actual_json_route_after_cold_text_only_fallback(self):
        pipeline = self.refinement()
        with patch.dict(os.environ, {'OLLAMA_BASE_URL': 'http://fixture',
                                     'CONTEXT_FALLBACK': '', 'CONTEXT_FALLBACK_REASON': ''}):
            pipeline.ensure_live_model(900)
        self.assertTrue(pipeline.state['model']['textOnlyReview'])
        self.assertEqual(pipeline.state['modelRecords'][TAG], pipeline.state['model'])
        pipeline.check_model_route.assert_called_once()

    def test_warm_text_only_record_cannot_skip_tool_required_web_preparation(self):
        previous = self.preflight()
        pipeline = self.refinement(mode='repo-web', previous=previous)
        with patch.dict(os.environ, {'OLLAMA_BASE_URL': 'http://fixture',
                                     'CONTEXT_FALLBACK': '', 'CONTEXT_FALLBACK_REASON': ''}), \
             patch('ci.refinement.http_json', side_effect=self.api):
            with self.assertRaises(NativeToolCallUnavailable):
                pipeline.ensure_live_model(900)
        pipeline.check_model_route.assert_not_called()

    def test_warm_text_only_record_is_reused_for_tool_optional_authoring(self):
        previous = self.preflight()
        pipeline = self.refinement(mode='repo-web', previous=previous)
        pipeline.state['activeTask'] = 'rework_questions'
        self.calls.clear()
        with patch.dict(os.environ, {'OLLAMA_BASE_URL': 'http://fixture'}), \
             patch('ci.refinement.http_json', side_effect=self.api):
            pipeline.ensure_live_model(900)
        self.assertEqual(pipeline.state['model'], previous)
        self.assertEqual(self.calls, ['http://fixture/api/tags', 'http://fixture/api/ps'])
        pipeline.check_model_route.assert_called_once()

    def test_fallback_record_disables_every_opencode_tool_and_remote_server(self):
        record = self.preflight()
        config = runtime_config(read(ROOT / 'opencode.json'), 'repo-only')
        server = OpenCode(self.archive, ROOT, config, self.archive / 'opencode', record)
        server.api = Mock(return_value={'healthy': True, 'version': 'synthetic'})
        with patch.dict(os.environ, {'MCP_HEADERS_JSON': '', 'CONTROLLER_DEFINITIONS': ''}), \
             patch('ci.runtime.subprocess.Popen') as launch:
            launch.return_value.poll.return_value = None
            try:
                server.start()
                selected = json.loads(launch.call_args.kwargs['env']['OPENCODE_CONFIG_CONTENT'])
                self.assertEqual(selected['permission'], {'*': 'deny'})
                self.assertTrue(all(not m['enabled'] for m in selected['mcp'].values()))
                for role in selected['agent'].values():
                    self.assertEqual(role['permission'], {'*': 'deny'})
                    self.assertTrue(all(value is False for value in role['tools'].values()))
                self.assertEqual(selected['model'], 'ollama/' + TAG)
            finally:
                if server.log:
                    server.log.close()
