"""Bounded model fallbacks with real candidate/review gates and no inference."""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ci.contracts import digest, read
from ci.dry_run import synthetic_review
from ci.pipeline import CandidateAttemptsExhausted
from ci.refinement import parse_plan
from ci.runtime import ModelAnswerError, RetryableModelError
from test_refinement import fixture


class RetryModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.environment = patch.dict(os.environ, {'REFINEMENT_PLAN': '', 'RAG_MODE': 'repo-web',
            'TARGET_BRANCH': '', 'SOURCE_CACHE_MODE': 'off', 'CATALOG_PROFILE': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.pipeline, _ = fixture(Path(self.temp.name), {
            'sfs': 'A', 'complete_fields': False, 'ai_prompts': True, 'retry_models': 'b,c'})
        self.slot = next(s for s in self.pipeline.state['slots'] if s['sfsId'] == 'A')
        self.record = read(self.pipeline.work / 'repo-sf/en' / (self.slot['repoId'] + '.json'))
        self.pipeline.state['dryRun'] = False
        self.pipeline.state['model']['digest'] = 'a' * 64
        self.calls, self.warmed = [], []
        self.behavior = lambda label, key: 'ok'
        self.answer = None
        template_model = copy.deepcopy(self.pipeline.state['model'])

        def warm(seconds):
            label = self.pipeline.state['activeModelLabel']
            self.warmed.append(label)
            self.pipeline.state['model'] = {**template_model,
                'tag': self.pipeline.state['refinementPlan']['models'][label], 'digest': label * 64}
            self.pipeline.state['modelRecords'][self.pipeline.state['model']['tag']] = self.pipeline.state['model']
        self.pipeline.ensure_live_model = warm
        owner = self

        class Server:
            def __init__(self, label):
                self.label = label

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def ask(self, role, prompt, seconds, call_id, **kwargs):
                owner.calls.append((self.label, role, call_id))
                self_behavior = owner.behavior(self.label, call_id)
                if isinstance(self_behavior, Exception):
                    raise self_behavior
                if role == 'catalog-validator':
                    payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"candidate":'):])
                    review = synthetic_review(payload['candidate'], payload['requiredChecks'], payload['fixtures'])
                    if self_behavior == 'review-rejected':
                        review['decision'] = 'rejected'
                        review['checks'][0].update(passed=False, rationale='Synthetic unsupported claim')
                    return review
                if self_behavior == 'invalid':
                    return {'fieldUpdateFormat': 'Incorrect wrapper copied into output'}
                if owner.answer is not None:
                    return copy.deepcopy(owner.answer(call_id) if callable(owner.answer) else owner.answer)
                return {'promptAiOptimization': 'Synthetic checked prompt by ' + self.label}

        self.primary = Server('a')
        self.pipeline.server = lambda stage: Server(self.pipeline.state['activeModelLabel'])

    def run_task(self, label='primary'):
        self.pipeline.state['refinementPassModel'] = label
        self.pipeline.activate('ai_prompts')
        server = self.pipeline.server('synthetic-pass')
        return self.pipeline.prompt_task(server, self.slot, copy.deepcopy(self.record))

    def test_retry_order_is_explicit_validated_and_can_be_disabled(self):
        self.assertEqual(parse_plan({})['retryModels'], ['b', 'c'])
        self.assertEqual(parse_plan({'retry_models': ' C, B '})['retryModels'], ['c', 'b'])
        self.assertEqual(parse_plan({'retry_models': ''})['retryModels'], [])
        for order in ('b,d', 'b,b', 'b,', ['b'], 'b;curl example.invalid'):
            with self.subTest(order=order), self.assertRaises(ValueError):
                parse_plan({'retry_models': order})

    def test_primary_failure_queues_task_without_loading_another_model(self):
        self.behavior = lambda label, key: 'invalid'
        with self.assertRaises(CandidateAttemptsExhausted):
            self.run_task()
        self.assertEqual(self.warmed, ['a'])
        self.assertTrue(all(label == 'a' for label, _, _ in self.calls))
        queue = self.pipeline.state['modelRetryQueue']['refine-ai_prompts/00A0']
        self.assertEqual(queue['attemptedTags'], [self.pipeline.state['refinementPlan']['models']['a']])

    def test_schema_failure_then_review_rejection_reaches_c_in_separate_passes(self):
        self.behavior = lambda label, key: {'a': 'invalid', 'b': 'review-rejected', 'c': 'ok'}[label]
        with self.assertRaises(CandidateAttemptsExhausted):
            self.run_task()
        with self.assertRaises(CandidateAttemptsExhausted):
            self.run_task('b')
        result = self.run_task('c')
        self.assertIn('by c', result['promptAiOptimization'])
        route = read(self.pipeline.root / 'model-retries/refine-ai_prompts/00A0.json')
        self.assertEqual(route['acceptedSlot'], 'c')
        self.assertEqual([a['status'] for a in route['attempts']], ['rejected', 'rejected', 'checked'])
        for label in ('b', 'c'):
            self.assertTrue(any(l == label and role == 'catalog-validator' for l, role, _ in self.calls))
        self.assertNotIn('refine-ai_prompts/00A0', self.pipeline.state['modelRetryQueue'])

    def test_checked_cache_from_a_is_reused_in_b_without_loading_a(self):
        expected = self.run_task()
        calls = list(self.calls)
        self.pipeline.state = read(self.pipeline.state_path)
        actual = self.run_task('b')
        self.assertEqual(actual, expected)
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.warmed, ['a', 'b'])
        self.assertEqual(self.pipeline.state['taskModelOutcomes']['refine-ai_prompts/00A0']['slot'], 'a')

    def test_changed_input_does_not_reuse_checked_answer(self):
        self.run_task()
        before = len(self.calls)
        self.record['question']['shortBinaryQuestion'] = 'Is this a different bound criterion?'
        self.run_task('b')
        self.assertGreater(len(self.calls), before)
        self.assertTrue(all(label == 'b' for label, _, _ in self.calls[before:]))

    def test_checked_route_with_missing_cache_stops_without_regeneration(self):
        self.run_task()
        calls = list(self.calls)
        (self.pipeline.work / '.task-cache/refine-ai_prompts/00A0.json').unlink()
        with self.assertRaisesRegex(ValueError, 'no matching task cache'):
            self.run_task('b')
        self.assertEqual(self.calls, calls)

    def test_final_annotations_do_not_rebind_modified_checked_files(self):
        self.pipeline.refine('A')
        path = self.pipeline.work / 'repo-sf/en/00A0.json'
        path.write_text(path.read_text().replace('Synthetic checked prompt', 'Unapproved replacement'))
        with self.assertRaisesRegex(ValueError, 'Checked SFS bytes changed'):
            self.pipeline.retry_models()
        self.assertFalse(self.pipeline.state.get('modelPassesComplete'))

    def test_malformed_assistant_json_is_queued_for_a_later_pass(self):
        self.behavior = lambda label, key: ModelAnswerError('Synthetic malformed assistant JSON') if label == 'a' else 'ok'
        with self.assertRaises(CandidateAttemptsExhausted):
            self.run_task()
        self.assertIn('by b', self.run_task('b')['promptAiOptimization'])

    def test_service_and_identity_failures_do_not_try_another_model(self):
        for error in (RuntimeError('Synthetic model service unavailable'), ValueError('Pinned model identity changed')):
            with self.subTest(error=error):
                self.behavior = lambda label, key: error
                with self.assertRaises(RuntimeError):
                    self.run_task()
                self.assertTrue(all(label == 'a' for label, _, _ in self.calls))
                self.assertEqual(self.pipeline.state.get('modelRetryQueue', {}), {})

    def test_observed_retryable_laguna_failure_reaches_the_next_model_pass(self):
        self.behavior = lambda label, key: RetryableModelError(500, 'empty Laguna tool call name') if label in ('a', 'b') else 'ok'
        for label in ('primary', 'b'):
            with self.assertRaises(CandidateAttemptsExhausted):
                self.run_task(label)
        self.assertIn('by c', self.run_task('c')['promptAiOptimization'])
        route = read(self.pipeline.root / 'model-retries/refine-ai_prompts/00A0.json')
        self.assertEqual([a['status'] for a in route['attempts']], ['rejected', 'rejected', 'checked'])

    def test_truncated_author_then_retryable_final_review_reaches_c_without_accepting_b(self):
        # Run 39100825307, job 118290612933 failed on this second review.
        from ci.runtime import model_error

        def observed(label, call_id):
            if label == 'a':
                return 'invalid'
            if label == 'b' and call_id.endswith('-1-author'):
                return ModelAnswerError('Unterminated string starting at: line 1 column 929 (char 928)')
            if label == 'b' and call_id.endswith('-2-review'):
                return model_error({'name': 'APIError', 'data': {
                    'message': 'empty Laguna tool call name', 'statusCode': 500, 'isRetryable': True}})
            return 'ok'

        self.behavior = observed
        for selected in ('primary', 'b'):
            with self.assertRaises(CandidateAttemptsExhausted):
                self.run_task(selected)

        queue_key = 'refine-ai_prompts/00A0'
        self.assertIn(queue_key, self.pipeline.state['modelRetryQueue'])
        self.assertNotIn(queue_key, self.pipeline.state.get('taskModelOutcomes', {}))
        self.assertFalse((self.pipeline.work / '.task-cache/refine-ai_prompts/00A0.json').exists())
        self.assertTrue(any(label == 'b' and role == 'catalog-validator'
                            and call_id.endswith('-2-review') for label, role, call_id in self.calls))
        route_path = self.pipeline.root / 'model-retries/refine-ai_prompts/00A0.json'
        route = read(route_path)
        self.assertNotIn('acceptedSlot', route)
        self.assertIn('HTTP 500', route['attempts'][-1]['reason'])

        self.assertIn('by c', self.run_task('c')['promptAiOptimization'])
        self.assertNotIn(queue_key, self.pipeline.state['modelRetryQueue'])
        self.assertEqual(self.pipeline.state['taskModelOutcomes'][queue_key]['slot'], 'c')
        self.assertEqual([a['status'] for a in read(route_path)['attempts']],
                         ['rejected', 'rejected', 'checked'])

    def test_incremental_publication_keeps_model_pass_order_and_recovers_with_c(self):
        from ci.incremental import Incremental
        self.behavior = lambda label, key: ('invalid' if label == 'a' and '00A0-' in key else
            RetryableModelError(500, 'empty Laguna tool call name') if label == 'b' else 'ok')
        runner = Incremental(self.pipeline)
        base = runner.head()
        self.assertTrue(runner.begin('A'))
        for sf in '0123456789ABCDEF':
            runner.factor('A', sf)
            runner.prepare('sf', 'A', sf)
            runner.publish()
        runner.finish_sfs('A')
        runner.prepare('sfs', 'A')
        runner.publish()
        runner.finish_pass('primary')
        primary_calls = len(self.calls)
        self.assertTrue(all(label == 'a' for label, _, _ in self.calls))
        for model in ('b', 'c'):
            self.assertTrue(runner.begin('A', model))
            self.assertEqual(runner.batch()[1]['factors'], ['0'])
            runner.factor('A', '0')
            runner.prepare('sf', 'A', '0')
            runner.publish()
            runner.finish_sfs('A')
            runner.prepare('sfs', 'A')
            runner.publish()
            runner.finish_pass(model)
        runner.prepare('catalog')
        runner.publish()
        labels = [label for label, _, _ in self.calls[primary_calls:]]
        self.assertEqual(labels, sorted(labels))
        self.assertIn('by c', read(self.pipeline.output / 'repo-sf/en/00A0.json')['question']['promptAiOptimization'])
        self.assertEqual(self.pipeline.state.get('refinementPending'), {})
        self.assertEqual(__import__('ci.pipeline', fromlist=['command']).command(
            ['git', 'rev-parse', 'HEAD^'], self.pipeline.output).stdout.strip(), base)

    def test_json_route_probe_is_bounded_and_failed_model_is_not_called_for_every_sf(self):
        self.pipeline.state['model']['tag'] = self.pipeline.state['refinementPlan']['models']['a']
        self.behavior = lambda label, key: RetryableModelError(500, 'empty Laguna tool call name')
        self.pipeline.check_model_route()
        self.pipeline.check_model_route()
        self.assertEqual(len(self.calls), 2)
        with self.assertRaises(CandidateAttemptsExhausted):
            self.run_task()
        self.assertEqual(len(self.calls), 2)

    def test_successful_primary_never_loads_fallback_models(self):
        self.pipeline.refine('A')
        self.pipeline.retry_models()
        self.assertEqual(self.warmed, ['a'])
        self.assertTrue(all(label == 'a' for label, _, _ in self.calls))

    def test_all_primary_positions_finish_before_b_and_b_finishes_before_c(self):
        self.pipeline.state['refinementPlan']['sfs'] = ['A', 'B']
        self.behavior = lambda label, key: ('invalid' if label in ('a', 'b') and
            any(repo in key for repo in ('00A0-', '00B0-')) else 'ok')
        self.pipeline.refine('A')
        self.pipeline.refine('B')
        primary_count = len(self.calls)
        self.assertTrue(all(label == 'a' for label, _, _ in self.calls))
        self.pipeline.retry_models()
        remaining = [label for label, _, _ in self.calls[primary_count:]]
        self.assertEqual(remaining, sorted(remaining))
        self.assertTrue('b' in remaining and 'c' in remaining)
        self.assertEqual(self.warmed, ['a', 'a', 'b', 'b', 'c', 'c'])
        for scope in ('A', 'B'):
            self.assertEqual(self.pipeline.state['sfsGates'][scope]['pendingSlots'], [])
        marker = read(self.pipeline.root / 'refinement-tasks/ai_prompts/00A0.json')
        self.assertEqual(marker['requestedModel'], self.pipeline.state['refinementPlan']['models']['a'])
        self.assertEqual(marker['models']['refine-ai_prompts/00A0']['slot'], 'c')
        calls = list(self.calls)
        self.pipeline.retry_models()
        self.assertEqual(calls, self.calls)

    def test_retry_models_cannot_run_before_the_primary_pass_finishes(self):
        with self.assertRaisesRegex(ValueError, 'Finish the primary pass'):
            self.pipeline.retry_models()
        self.assertEqual(self.warmed, [])

    def test_exhausted_position_is_annotated_and_other_positions_finish(self):
        self.behavior = lambda label, key: 'invalid' if '00A0-' in key else 'ok'
        self.pipeline.refine('A')
        self.pipeline.retry_models()
        original = read(self.pipeline.work / 'base/repo-sf/en/00A0.json')
        current = read(self.pipeline.work / 'repo-sf/en/00A0.json')
        self.assertEqual(current['question']['shortBinaryQuestion'], original['question']['shortBinaryQuestion'])
        self.assertTrue(current['question']['plain'].startswith('[non-validated]'))
        self.assertTrue(current['meta']['title'].startswith('[non-validated]'))
        self.assertEqual(current['review'], {'status': 'draft', 'approvals': []})
        self.assertEqual(self.pipeline.state['sfsGates']['A']['pendingSlots'], ['00A0'])
        self.pipeline.publish_sfs('A')
        status = read(self.pipeline.output / 'refinement-status.json')
        self.assertEqual(status['status'], 'selected-tasks-partial-draft')
        self.assertEqual(status['pendingPositions'][0]['annotation'], '[non-validated]')
        page = (self.pipeline.output / 'pages/catalog/sets/A/0.en-US.mdx').read_text()
        self.assertIn('[non-validated]', page)
        self.assertNotIn('[non-validated]', read(self.pipeline.output / 'repo-sf/en/00A1.json')['question']['plain'])

    def test_disabled_retry_order_never_loads_b_or_c_and_still_annotates(self):
        self.pipeline.state['refinementPlan']['retryModels'] = []
        self.behavior = lambda label, key: 'invalid' if '00A0-' in key else 'ok'
        self.pipeline.refine('A')
        self.pipeline.retry_models()
        self.assertEqual(self.warmed, ['a'])
        self.assertEqual(self.pipeline.state['refinementPending']['00A0']['annotation'], '[non-validated]')

    def test_duplicate_model_tags_are_not_loaded_as_a_fallback(self):
        plan = self.pipeline.state['refinementPlan']
        plan['models']['b'] = plan['models']['a']
        self.behavior = lambda label, key: 'invalid' if label == 'a' and '00A0-' in key else 'ok'
        self.pipeline.refine('A')
        self.pipeline.retry_models()
        self.assertEqual(self.warmed, ['a', 'c'])

    def test_publication_retains_later_sfs_base_until_its_own_commit(self):
        self.pipeline.state['refinementPlan']['sfs'] = ['A', 'B']
        self.pipeline.refine('A')
        self.pipeline.refine('B')
        self.pipeline.retry_models()
        base_b = read(self.pipeline.work / 'base/repo-sf/en/00B0.json')
        ready_b = read(self.pipeline.work / 'repo-sf/en/00B0.json')
        self.pipeline.publish_sfs('A')
        self.assertEqual(read(self.pipeline.output / 'repo-sf/en/00B0.json'), base_b)
        self.assertEqual(read(self.pipeline.work / 'repo-sf/en/00B0.json'), ready_b)
        self.pipeline.publish_sfs('B')
        self.assertEqual(read(self.pipeline.output / 'repo-sf/en/00B0.json'), ready_b)
        self.assertEqual([p['stage'] for p in self.pipeline.state['publications']], ['sfs-A', 'sfs-B'])

    def test_a_question_cache_is_reused_while_b_repairs_its_fields(self):
        from ci.dry_run import question
        plan = self.pipeline.state['refinementPlan']
        plan['tasks']['rework_questions']['enabled'] = True
        plan['tasks']['ai_prompts']['enabled'] = False
        candidate = question(self.slot)
        candidate.update(parentQuestionDigest=digest(self.record['question']['shortBinaryQuestion']), semanticRelation='changed')
        self.answer = candidate
        self.behavior = lambda label, key: 'invalid' if label == 'a' and '-fields-' in key else 'ok'
        self.pipeline.activate('rework_questions')
        self.pipeline.question_task(self.primary, self.slot, self.record)
        current = copy.deepcopy(self.record)
        current['question']['shortBinaryQuestion'] = candidate['shortBinaryQuestion']
        with self.assertRaises(CandidateAttemptsExhausted):
            self.pipeline.fields_task(self.primary, 'rework_questions', self.slot, current, candidate)
        self.pipeline.state['refinementPassModel'] = 'b'
        self.pipeline.activate('rework_questions')
        server = self.pipeline.server('synthetic-b')
        before = list(self.calls)
        self.pipeline.question_task(server, self.slot, self.record)
        self.assertEqual(self.calls, before)
        source = next(iter(self.pipeline.frozen_sources().values()))
        self.answer = {'question': {key: 'Synthetic checked ' + key for key in ('plain','technical-short','keywords')},
            'meta': {'title':'Synthetic title'}, 'clarificationQuestion':[{'key1':'Evidence?', 'key2':'Synthetic guidance'}],
            'wikiLinks':[{'key1':source['title'], 'key2':source['url']}]}
        self.pipeline.fields_task(server, 'rework_questions', self.slot, current, candidate)
        self.assertEqual(self.warmed, ['a','b'])
        self.assertTrue(all(label == 'b' for label, _, _ in self.calls[len(before):]))

    def test_resume_after_service_failure_in_b_does_not_reload_a_or_repeat_checked_b_work(self):
        def behavior(label, key):
            if label == 'a' and any(repo in key for repo in ('00A0-', '00A1-')):
                return 'invalid'
            if label == 'b' and '00A1-' in key:
                return RuntimeError('Synthetic temporary model-service outage')
            return 'ok'
        self.behavior = behavior
        self.pipeline.refine('A')
        with self.assertRaises(RuntimeError):
            self.pipeline.retry_models()
        completed = [row for row in self.calls if row[0] == 'b' and '00A0-' in row[2]]
        self.behavior = lambda label, key: 'ok'
        before = list(self.warmed)
        self.pipeline.refine('A')
        self.assertEqual(before, self.warmed)
        self.pipeline.retry_models()
        self.assertEqual(completed, [row for row in self.calls if row[0] == 'b' and '00A0-' in row[2]])
        self.assertNotIn('a', self.warmed[len(before):])
        self.assertEqual(self.pipeline.state['sfsGates']['A']['pendingSlots'], [])
