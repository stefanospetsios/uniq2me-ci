"""Bounded grace and honest draft continuation; no live inference or GitHub writes."""
import io
import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import QUESTION_CHECKS, ReviewRejected, read, validate_question, validate_review, write
from ci.dry_run import FixtureServer, candidate, synthetic_review
from ci.metrics import collect
from ci.pipeline import CatalogReviewExhausted, CatalogTimeout, Pipeline, STAGES, command
from ci.runtime import OpenCode
from ci.smoke_test import create_fixture_pipeline
from test_pipeline import SOURCES, question, review


class CatalogGraceTests(unittest.TestCase):
    def test_rejected_reviews_include_reasons_and_exhaust_as_pending_before_timeout(self):
        slot = {'sfsId': '0', 'sfId': '0', 'repoId': '0000'}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RUNNER_TEMP': directory}), \
             redirect_stdout(io.StringIO()) as stdout:
            clock, prompts = [0], []
            pipeline = Pipeline()
            pipeline.state = {'maximumAttempts': 2, 'catalogGraceSeconds': 30,
                'timeoutsMinutes': {'REVIEW_MINUTES': 2}, 'model': {'digest': 'a' * 64}}
            pipeline.frozen_sources = lambda: SOURCES
            class Server:
                current = None
                def ask(self, role, prompt, seconds, call_id, **kwargs):
                    prompts.append(prompt)
                    if role == 'question-author':
                        clock[0] += 22 if self.current is None else 47
                        self.current = question(slot)
                        if len(prompts) > 1:
                            self.current['changeReason'] += ' Synthetic repaired candidate'
                        return copy.deepcopy(self.current)
                    clock[0] += 27 if len(prompts) == 2 else 22
                    value = review(self.current, QUESTION_CHECKS, self.current['fixtures'])
                    value['decision'] = 'rejected'
                    value['checks'][1].update(passed=False, rationale='Synthetic criterion combines two obligations')
                    value['unresolved'] = ['Synthetic requirement: split into one assessable condition']
                    return value
            with patch('ci.pipeline.time.monotonic', side_effect=lambda: clock[0]), \
                 self.assertRaises(CatalogReviewExhausted) as failure:
                pipeline.generate(Server(), 'catalog', '0000', 'question-author', 'Test', 2,
                    lambda v: validate_question(v, slot, SOURCES, {'0-concern'}), QUESTION_CHECKS)
            self.assertEqual(clock[0], 118)  # No timeout: exactly the live failure's distinction.
            self.assertEqual(failure.exception.status, 'pending-review-rejected')
            self.assertEqual(failure.exception.reviewFailure['failedChecks'][0]['ruleId'], 'single-condition')
            self.assertIn('Synthetic criterion combines two obligations', stdout.getvalue())
            self.assertIn('Synthetic requirement: split', prompts[2])
            self.assertIn('Previous rejected candidate to correct', prompts[2])
            self.assertFalse((pipeline.work / '.task-cache/catalog/0000.json').exists())
            self.assertEqual(read(pipeline.root / 'review-attempts/catalog/0000-2.json')['review']['decision'], 'rejected')

    def test_rejected_review_with_wrong_artifact_binding_is_still_a_strict_failure(self):
        slot = {'sfsId': '0', 'sfId': '0', 'repoId': '0000'}
        value = review(question(slot), QUESTION_CHECKS)
        value.update(decision='rejected', artifactDigest='sha256:' + '0' * 64)
        with self.assertRaisesRegex(ValueError, 'different bytes') as failure:
            validate_review(value, question(slot), QUESTION_CHECKS, SOURCES)
        self.assertNotIsInstance(failure.exception, ReviewRejected)

    def test_completed_answer_during_grace_is_kept_without_aborting(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as stdout:
            clock, calls = [0], []
            server = OpenCode(directory, ROOT, {}, directory,
                              {'digest': 'a' * 64, 'effectiveContext': 262144})
            def api(path, data=None, **kwargs):
                calls.append(path)
                if path == '/session':
                    return {'id': 'test-session'}
                if path == '/session/status':
                    clock[0] += 10
                    return {'test-session': {'type': 'idle' if clock[0] >= 80 else 'busy'}}
                if path.endswith('/message'):
                    return [{'info': {'role': 'assistant', 'providerID': 'ollama',
                        'modelID': 'qwen3:235b', 'time': {'completed': 1}, 'finish': 'stop'},
                        'parts': [{'type': 'text', 'text': '{"finished":true}'}]}]
            server.api, server.check_residency = api, lambda: None
            with patch('ci.runtime.time.monotonic', side_effect=lambda: clock[0]), \
                 patch('ci.runtime.time.sleep'):
                self.assertEqual(server.ask('question-author', 'Test', 60, 'grace', grace_seconds=30),
                                 {'finished': True})
            self.assertNotIn('/session/test-session/abort', calls)
            envelope = read(Path(directory) / 'calls/grace/envelope.json')
            self.assertTrue(envelope['graceUsed'])
            self.assertEqual(envelope['graceSeconds'], 30)
            self.assertIn('entering 30.0s finalization grace', stdout.getvalue())

    def test_grace_expiry_aborts_and_archives_instead_of_waiting_forever(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            clock, aborted = [0], [False]
            server = OpenCode(directory, ROOT, {}, directory,
                              {'digest': 'a' * 64, 'effectiveContext': 262144})
            def api(path, data=None, **kwargs):
                if path == '/session':
                    return {'id': 'test-session'}
                if path.endswith('/abort'):
                    aborted[0] = True
                if path == '/session/status':
                    clock[0] += 10
                    return {'test-session': {'type': 'idle' if aborted[0] else 'busy'}}
                if path.endswith('/message'):
                    return []
            server.api = api
            with patch('ci.runtime.time.monotonic', side_effect=lambda: clock[0]), \
                 patch('ci.runtime.time.sleep'), self.assertRaises(TimeoutError):
                server.ask('question-author', 'Test', 60, 'expiry', grace_seconds=30)
            self.assertTrue(aborted[0])
            envelope = read(Path(directory) / 'calls/expiry/envelope.json')
            self.assertTrue(envelope['graceUsed'])
            self.assertEqual(envelope['status'], 'failed')

    def test_author_and_review_share_one_grace_period(self):
        self.generation_budget(review_timeout=False)

    def test_review_timeout_preserves_valid_but_unreviewed_candidate(self):
        self.generation_budget(review_timeout=True)

    def generation_budget(self, review_timeout):
        clock, calls = [0], []
        slot = {'sfsId': '0', 'sfId': '0', 'repoId': '0000'}
        class Server:
            def ask(self, role, prompt, seconds, call_id, **kwargs):
                calls.append((seconds, kwargs.get('grace_seconds', 0)))
                if role == 'question-author':
                    clock[0] += 110
                    return question(slot)
                clock[0] += 40 if review_timeout else 35
                if review_timeout:
                    raise TimeoutError('Synthetic review reached its deadline')
                return review(question(slot), QUESTION_CHECKS, question(slot)['fixtures'])
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RUNNER_TEMP': directory}), \
             redirect_stdout(io.StringIO()):
            pipeline = Pipeline()
            pipeline.state = {'maximumAttempts': 1, 'catalogGraceSeconds': 30,
                'timeoutsMinutes': {'REVIEW_MINUTES': 2}, 'model': {'digest': 'a' * 64}}
            pipeline.frozen_sources = lambda: SOURCES
            with patch('ci.pipeline.time.monotonic', side_effect=lambda: clock[0]):
                def run():
                    return pipeline.generate(Server(), 'catalog', '0000', 'question-author', 'Test', 2,
                        lambda v: validate_question(v, slot, SOURCES, {'0-concern'}), QUESTION_CHECKS)
                if review_timeout:
                    with self.assertRaises(CatalogTimeout) as failure:
                        run()
                    self.assertEqual(failure.exception.candidate, question(slot))
                    self.assertFalse((pipeline.work / '.task-cache/catalog/0000.json').exists())
                else:
                    self.assertEqual(run(), question(slot))
            self.assertEqual(calls, [(120, 30), (10, 30)])
            self.assertLessEqual(clock[0], 150)


class PendingPublicationTests(unittest.TestCase):
    def test_roster_timeout_preserves_questions_and_marks_acceptance_pending(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            pipeline, _ = create_fixture_pipeline(directory, publish=False)
            pipeline.execute('prepare')
            pipeline.state['dryRun'] = False
            class Server:
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass
                def ask(self, role, prompt, seconds, call_id, **kwargs):
                    assert kwargs['grace_seconds'] == 30
                    raise TimeoutError('Synthetic full roster deadline')
            pipeline.server = lambda _: Server()
            def generate(server, stage, key, role, prompt, minutes, validate, checks, fixtures=None):
                value = candidate(pipeline, stage, key)
                validate(value)
                return value
            pipeline.generate = generate
            pipeline.execute('catalog')
            self.assertEqual(read(pipeline.work / 'catalog/en.json')['questionCount'], 256)
            self.assertFalse(pipeline.state.get('catalogPending'))
            self.assertTrue(pipeline.state['catalogRosterPending'])
            self.assertFalse(pipeline.accepted_release())
            self.assertEqual(pipeline.state['stages']['catalog']['verificationStatus'], 'unverified-draft')
            self.assertIn('catalog/roster-pending.json', read(pipeline.work / 'catalog-feedback.json')['files'])

    def test_one_pending_question_continues_five_pushes_and_preserves_accepted_base(self):
        self.publication(pending_all=False)

    def test_all_questions_timeout_and_all_five_draft_milestones_still_publish(self):
        self.publication(pending_all=True)

    def test_exhausted_review_continues_all_five_draft_commits_without_accepting_candidate(self):
        self.publication(pending_all=False, rejected=True)

    def publication(self, pending_all, rejected=False):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'PUSH_TOKEN': 'synthetic-local-token', 'GITHUB_OUTPUT': ''}), redirect_stdout(io.StringIO()):
            pipeline, bare = create_fixture_pipeline(directory)
            pipeline.execute('prepare')
            # Everything is synthetic scaffolding, but exercise real-mode publication policy.
            pipeline.state['dryRun'] = False
            pipeline.state['ragMode'] = 'repo-web'
            pipeline.server = lambda _: FixtureServer(pipeline)
            pending_id = pipeline.state['slots'][-1]['repoId']
            old_pointer = {'recordType': 'test-old-accepted-base'}
            write(pipeline.output / 'next-base.json', old_pointer)
            stale_mask = pipeline.output / 'repo-sfs-masks/en/old.json'
            write(stale_mask, {'stale': True})
            stale_solution = pipeline.output / 'solutions/en' / (pending_id + '.json')
            write(stale_solution, {'stale': True})
            stale_translation = pipeline.output / 'translations/fr' / (pending_id + '.json')
            write(stale_translation, {'stale': True})
            command(['git', 'add', 'next-base.json', 'repo-sfs-masks/en/old.json',
                     'solutions/en/' + pending_id + '.json',
                     'translations/fr/' + pending_id + '.json'], pipeline.output)
            command(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                     'commit', '--amend', '-qm', 'Synthetic accepted base'], pipeline.output)
            pipeline.state['outputBaseCommit'] = command(['git', 'rev-parse', 'HEAD'], pipeline.output).stdout.strip()
            def generate(server, stage, key, role, prompt, minutes, validate, checks, fixtures=None):
                if stage == 'catalog' and (pending_all or key == pending_id):
                    value = candidate(pipeline, stage, key)
                    validate(value)
                    if rejected:
                        rejected_review = synthetic_review(value, checks, value['fixtures'])
                        rejected_review['decision'] = 'rejected'
                        rejected_review['unresolved'] = ['Synthetic criterion needs repair']
                        problem = ReviewRejected(rejected_review)
                        raise CatalogReviewExhausted(str(problem), value, rejected_review, problem.details)
                    raise CatalogTimeout('Synthetic exhausted question review', value)
                if pending_all:
                    raise AssertionError('No inference is needed when every question is pending')
                if stage == 'masks' or key.startswith('masks-'):
                    raise AssertionError('Incomplete catalogs cannot generate legal MASKS')
                value = candidate(pipeline, stage, key)
                validate(value)
                write(pipeline.work / 'reviews' / stage / (key + '.json'),
                      synthetic_review(value, checks, fixtures))
                return value
            pipeline.generate = generate
            for stage in STAGES:
                pipeline.execute(stage)
                pipeline.publish(stage)
                pipeline.publish(stage)  # Re-publication stays idempotent.
            count = 256 if pending_all else 1
            self.assertEqual(len(pipeline.state['publications']), 5)
            self.assertEqual(command(['git', 'rev-list', '--count', pipeline.state['branch']], bare).stdout.strip(), '6')
            self.assertEqual(read(pipeline.output / 'next-base.json'), old_pointer)
            manifest = read(pipeline.output / 'release-manifest.json')
            self.assertEqual(manifest['status'], 'unverified-draft')
            self.assertEqual(manifest['languages']['en']['sfCount'], 256 - count)
            self.assertEqual(len(manifest['pendingQuestions']), count)
            self.assertEqual(read(pipeline.output / 'catalog/en.json')['pendingQuestionCount'], count)
            pending_record = read(pipeline.output / 'catalog/pending' / (pending_id + '.json'))
            self.assertEqual(pending_record['status'], 'pending-review-rejected' if rejected else 'pending-timeout')
            if rejected:
                self.assertEqual(pending_record['review']['decision'], 'rejected')
            self.assertFalse(stale_mask.exists())
            self.assertFalse(stale_solution.exists())
            self.assertFalse(stale_translation.exists())
            self.assertEqual(read(pipeline.output / 'mask-status/en.json')['generatedMaskCount'], 0)
            for language in ('en', 'fr', 'el'):
                self.assertTrue(read(pipeline.output / 'repo-sf' / language / (pending_id + '.json'))['isPlaceholder'])
                status = read(pipeline.output / 'completion-status' / language / (pending_id + '.json'))
                self.assertFalse(status['completed'])
                self.assertFalse(status['translated'])
            self.assertEqual(collect(pipeline.root, pipeline.state)['outputStatus'], 'unverified-draft')
            self.assertEqual(collect(pipeline.root, pipeline.state)['progress']['pendingQuestions'], count)
            self.assertFalse(read(pipeline.output / 'pipeline-status.json')['multilingualComplete'])
            feedback = pipeline.output / 'catalog-feedback.json'
            pipeline.load_catalog_feedback(feedback)
            restored = pipeline.work / 'inputs/prior-draft/catalog/pending' / (pending_id + '.json')
            self.assertTrue(read(restored)['candidate'])
            pointer = read(feedback)
            pointer['files']['catalog/pending/' + pending_id + '.json'] = 'sha256:' + '0' * 64
            write(feedback, pointer)
            with self.assertRaisesRegex(ValueError, 'Draft feedback bytes changed'):
                pipeline.load_catalog_feedback(feedback)

    def test_invalid_candidates_without_timeout_still_fail_the_gate(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RUNNER_TEMP': directory}), \
             redirect_stdout(io.StringIO()):
            pipeline = Pipeline()
            pipeline.state = {'maximumAttempts': 1, 'catalogGraceSeconds': 30,
                'timeoutsMinutes': {'REVIEW_MINUTES': 2}, 'model': {'digest': 'a' * 64}}
            pipeline.frozen_sources = lambda: SOURCES
            class Server:
                def ask(self, *args, **kwargs):
                    return {'invalid': True}
            with self.assertRaisesRegex(RuntimeError, 'failed bounded generation'):
                pipeline.generate(Server(), 'catalog', '0000', 'question-author', 'Test', 2,
                    lambda v: (_ for _ in ()).throw(ValueError('Invalid contract')), QUESTION_CHECKS)
