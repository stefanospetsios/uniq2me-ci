"""Zero budgets disable only their deadlines; no live model or network calls."""
import io
import math
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import QUESTION_CHECKS, read, validate_question
from ci.pipeline import Pipeline, timeout_minutes
from ci.runtime import OpenCode, native_tool_probe
from ci.smoke_test import create_fixture_pipeline
from ci.timeouts import budget_seconds, optional_seconds
from test_pipeline import SOURCES, question, review
from test_rag import MODEL


class TimeoutTests(unittest.TestCase):
    def test_zero_is_accepted_but_negative_nonfinite_and_invalid_minutes_are_rejected(self):
        for value in ('0', '2', '5.5'):
            with patch.dict(os.environ, {'X': value}):
                self.assertEqual(timeout_minutes('X', 2), float(value))
        for value in ('-1', 'nan', 'inf', '7201', 'invalid'):
            with self.subTest(value=value), patch.dict(os.environ, {'X': value}), self.assertRaises(ValueError):
                timeout_minutes('X', 2)
        self.assertTrue(math.isinf(budget_seconds(0)))
        self.assertIsNone(optional_seconds(budget_seconds(0)))

    def test_generation_and_review_caps_remain_independent(self):
        slot = {'sfsId': '0', 'sfId': '0', 'repoId': '0000'}
        for minutes, review_minutes, expected in [(0, 0, [None, None]),
                (0, 2, [None, 120]), (5, 0, [300, 200])]:
            with self.subTest(minutes=minutes, review=review_minutes), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {'RUNNER_TEMP': directory}), redirect_stdout(io.StringIO()) as output:
                clock, calls = [0], []
                pipeline = Pipeline()
                pipeline.state = {'maximumAttempts': 1, 'timeoutsMinutes': {'REVIEW_MINUTES': review_minutes},
                    'model': {'digest': 'a' * 64}}
                pipeline.frozen_sources = lambda: SOURCES
                class Server:
                    def ask(self, role, prompt, seconds, call_id, **kwargs):
                        calls.append(seconds)
                        if role == 'question-author':
                            clock[0] += 3600 if minutes == 0 else 100
                            return question(slot)
                        clock[0] += 30
                        return review(question(slot), QUESTION_CHECKS, question(slot)['fixtures'])
                with patch('ci.pipeline.time.monotonic', side_effect=lambda: clock[0]):
                    value = pipeline.generate(Server(), 'solutions', '0000', 'question-author', 'Test', minutes,
                        lambda v: validate_question(v, slot, SOURCES, {'0-concern'}), QUESTION_CHECKS,
                        question(slot)['fixtures'])
                self.assertEqual(value, question(slot))
                self.assertEqual(calls, expected)
                if minutes == 0:
                    self.assertIn('unlimited normal allowance', output.getvalue())

    def test_unlimited_opencode_call_keeps_polling_and_records_json_null(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as stdout:
            clock, calls = [0], []
            server = OpenCode(directory, ROOT, {}, directory,
                              {'digest': 'a' * 64, 'effectiveContext': 262144})
            def api(path, data=None, **kwargs):
                calls.append(path)
                if path == '/session':
                    return {'id': 'test-session'}
                if path == '/session/status':
                    clock[0] += 1000
                    return {'test-session': {'type': 'idle' if clock[0] >= 6000 else 'busy'}}
                if path.endswith('/message'):
                    return [{'info': {'role': 'assistant', 'providerID': 'ollama',
                        'modelID': 'qwen3:235b', 'time': {'completed': 1}, 'finish': 'stop'},
                        'parts': [{'type': 'text', 'text': '{"finished":true}'}]}]
            server.api, server.check_residency = api, lambda: None
            with patch('ci.runtime.time.monotonic', side_effect=lambda: clock[0]), \
                 patch('ci.runtime.time.sleep'):
                self.assertEqual(server.ask('question-author', 'Test', None, 'unlimited', grace_seconds=30),
                                 {'finished': True})
            self.assertNotIn('/session/test-session/abort', calls)
            path = Path(directory) / 'calls/unlimited/envelope.json'
            envelope = read(path)
            self.assertIsNone(envelope['timeoutSeconds'])
            self.assertFalse(envelope['timeLimitEnabled'])
            self.assertFalse(envelope['graceUsed'])
            self.assertNotIn('Infinity', path.read_text())
            self.assertIn('unlimited allowance', stdout.getvalue())

    def test_native_probe_supports_unlimited_model_loading(self):
        response = {'message': {'tool_calls': [{'function': {'name': 'preflight_probe',
                                                            'arguments': {'value': 'ready'}}}]}}
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
             patch('ci.runtime.http_json', return_value=response) as request:
            native_tool_probe('http://fixture', directory, 'qwen3:235b', 262144, True, None)
            self.assertIsNone(request.call_args.kwargs['timeout'])

    def test_unlimited_preflight_and_smoke_test_do_not_receive_zero_seconds(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
             patch.dict(os.environ, {'PREFLIGHT_MINUTES': '0', 'RAG_MODE': 'disabled', 'DRY_RUN': 'false'}), \
             patch('ci.preflight.runner_checks', return_value={'synthetic': True}):
            pipeline, _ = create_fixture_pipeline(directory, publish=False)
            pipeline.state.update(dryRun=False, ragMode='disabled', model=dict(MODEL))
            pipeline.state['timeoutsMinutes']['Z'] = 0
            pipeline.ensure_live_model = Mock()
            server = Mock()
            server.__enter__ = Mock(return_value=server)
            server.__exit__ = Mock(return_value=False)
            server.discover.return_value = {'allowedTools': [], 'openCodeVersion': 'fixture'}
            server.ask.return_value = {'status': 'ready'}
            pipeline.server = Mock(return_value=server)
            pipeline.execute('preflight')
            pipeline.ensure_live_model.assert_called_once_with(None)
            self.assertIsNone(server.ask.call_args.args[2])
            self.assertEqual(pipeline.state['runnerPreflight']['status'], 'passed')
