"""Observed usage accounting and publication metrics recovery."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import read, write
from ci.metrics import collect, markdown
from ci.pipeline import command
from ci.smoke_test import create_fixture_pipeline


class MetricsTests(unittest.TestCase):
    def test_usage_deduplicates_messages_and_does_not_add_reasoning_or_cache_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {'startedAt': '2026-10-13T00:00:00Z', 'executionAttempts': [
                {'stage': 'prepare', 'status': 'failed', 'elapsedSeconds': 100},
                {'stage': 'prepare', 'status': 'completed', 'elapsedSeconds': 200}]}
            message = {'info': {'id': 'assistant-1', 'role': 'assistant',
                'tokens': {'total': 120, 'input': 50, 'output': 30, 'reasoning': 10,
                           'cache': {'read': 40, 'write': 0}}}, 'parts': [
                {'id': 'tool-1', 'type': 'tool', 'tool': 'searxng_web_search', 'state': {'status': 'completed'}}]}
            for name in ('original', 'duplicate'):
                path = root / 'runtime/sources/1/calls' / name
                write(path / 'envelope.json', {'sessionId': 'same-session', 'status': 'failed', 'elapsedSeconds': 10})
                write(path / 'messages.json', [message])
            native = {'prompt_eval_count': 5, 'eval_count': 7, 'load_duration': 77_000_000_000,
                      'eval_duration': 1_000_000_000, 'prompt_eval_cached_count': 4}
            write(root / 'ollama/tool-probe-one/1-response.json', native)
            write(root / 'ollama/tool-probe-response.json', native)  # Alias must not be counted again.
            metrics = collect(root, state, '2026-10-13T01:00:00Z')
            self.assertEqual(metrics['tokens']['reportedTotal'], 132)
            self.assertEqual(metrics['tokens']['modelResponses'], 2)
            self.assertEqual(metrics['tokens']['reasoningTokens'], 10)
            self.assertEqual(metrics['tokens']['cacheReadTokens'], 44)
            self.assertEqual(metrics['time']['activeExecutionSeconds'], 300)
            self.assertEqual(metrics['time']['elapsedSinceStartSeconds'], 3600)
            self.assertEqual(metrics['time']['nativeModelLoadSeconds'], 77)
            self.assertEqual(metrics['time']['nativeGenerationTokensPerSecond'], 7)
            self.assertEqual(metrics['calls']['tools']['searxng_web_search']['completed'], 1)

    def test_missing_usage_is_unknown_with_a_visible_known_subtotal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / 'runtime/extended/1/calls/failure/envelope.json', {
                'status': 'failed', 'providerUsage': [None, {'total': 25, 'input': 10, 'output': 15}]})
            metrics = collect(root, {})
            self.assertIsNone(metrics['tokens']['reportedTotal'])
            self.assertEqual(metrics['tokens']['knownTotal'], 25)
            self.assertEqual(metrics['tokens']['responsesWithoutTotal'], 1)
            self.assertIn('| Reported token total | unknown |', markdown(metrics))
            self.assertEqual(metrics['calls']['failedAgent'], 1)

    def test_empty_real_usage_is_unknown_and_empty_dry_usage_is_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(collect(directory, {})['tokens']['reportedTotal'])
            self.assertEqual(collect(directory, {'dryRun': True})['tokens']['reportedTotal'], 0)

    def test_unanswered_native_request_keeps_the_total_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / 'ollama/tool-probe-one/1-request.json', {'synthetic': True})
            write(root / 'ollama/tool-probe-one/1-response.json', {'prompt_eval_count': 10, 'eval_count': 20})
            write(root / 'ollama/tool-probe-two/1-request.json', {'synthetic': True})
            metrics = collect(root, {})
            self.assertEqual(metrics['calls']['nativeProbe'], 2)
            self.assertEqual(metrics['tokens']['knownTotal'], 30)
            self.assertIsNone(metrics['tokens']['reportedTotal'])
            self.assertEqual(metrics['tokens']['responsesWithoutTotal'], 1)

    def test_commit_metrics_are_frozen_across_commit_failure_and_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'PUSH_TOKEN': 'synthetic-local-token', 'DRY_RUN': 'true'}), redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory)
            pipeline.execute('prepare')
            pipeline.execute('catalog')
            original = command
            def fail_commit(args, *rest, **kwargs):
                if 'commit' in args:
                    raise RuntimeError('Synthetic failure before commit')
                return original(args, *rest, **kwargs)
            with patch('ci.pipeline.command', side_effect=fail_commit):
                with self.assertRaisesRegex(RuntimeError, 'before commit'):
                    pipeline.publish('catalog')
            frozen = read(pipeline.root / 'publication-metrics/catalog.json')
            body = pipeline.state['publicationIntent']['body']
            pipeline.state.setdefault('metricsCounters', {})['cacheHits'] = 999
            pipeline.save()
            pipeline.publish('catalog')
            pipeline.publish('catalog')
            published = pipeline.output / 'runs' / pipeline.state['runId'] / 'metrics/catalog.json'
            self.assertEqual(read(published), frozen)
            self.assertEqual(read(pipeline.output / 'run-metrics.json'), frozen)
            self.assertEqual(command(['git', 'log', '-1', '--format=%b'], pipeline.output).stdout.strip(), body)
            self.assertEqual(len(pipeline.state['publications']), 1)


if __name__ == '__main__':
    unittest.main()
