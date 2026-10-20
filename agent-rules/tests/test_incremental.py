"""Real local Git histories, checkpoint recovery, and zero live inference."""
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
from ci.contracts import byte_digest, read
from ci.checkpoints import seal, locate
from ci.incremental import Incremental
from ci.pipeline import command, remote_head
from ci.runtime import RetryableModelError, model_error
from test_refinement import fixture
from ci.refinement import RefinementPipeline
from ci.smoke_test import restore_on_fresh_runner


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        env = patch.dict(os.environ, {'REFINEMENT_PLAN': '', 'RAG_MODE': 'repo-web',
            'TARGET_BRANCH': '', 'SOURCE_CACHE_MODE': 'off', 'CATALOG_PROFILE': '',
            'PUSH_TOKEN': 'synthetic-local-token', 'GITHUB_RUN_ID': '', 'GITHUB_RUN_ATTEMPT': ''})
        env.start()
        self.addCleanup(env.stop)
        self.p, self.remote = fixture(Path(self.temp.name), {'sfs': 'A', 'retry_models': ''}, publish=True)
        self.run = Incremental(self.p)

    def sf(self, sf):
        self.run.factor('A', sf)
        self.assertTrue(self.run.prepare('sf', 'A', sf))
        self.run.publish()

    def test_sf_push_sfs_squash_and_catalog_squash_preserve_owner_base(self):
        base = self.run.head()
        self.assertTrue(self.run.begin('A'))
        self.sf('0')
        first = self.run.head()
        self.assertEqual(remote_head(self.p.output, self.p.state['branch']), first)
        published = read(self.p.output / 'repo-sf/en/00A0.json')
        unselected = read(self.p.output / 'repo-sf/en/00A1.json')
        self.assertIn('SYNTHETIC DRY RUN', published['question']['plain'])
        self.assertNotIn('SYNTHETIC DRY RUN', unselected['question']['plain'])
        self.run.prepare('sf', 'A', '0')
        self.run.publish()
        self.assertEqual(self.run.head(), first)
        for sf in '123456789ABCDEF':
            self.sf(sf)
        self.assertEqual(command(['git', 'rev-list', '--count', base + '..HEAD'], self.p.output).stdout.strip(), '16')
        self.run.finish_sfs('A')
        self.run.prepare('sfs', 'A')
        self.run.publish()
        self.assertEqual(command(['git', 'rev-list', '--count', base + '..HEAD'], self.p.output).stdout.strip(), '1')
        self.run.finish_pass('primary')
        self.run.prepare('catalog')
        self.run.publish()
        self.assertEqual(command(['git', 'rev-parse', 'HEAD^'], self.p.output).stdout.strip(), base)
        self.assertEqual(remote_head(self.p.output, self.p.state['branch']), self.run.head())
        self.assertFalse((self.p.output / 'next-base.json').exists())
        self.assertEqual(read(self.p.output / 'site-publication.json')['catalogAccepted'], False)

    def test_destination_edit_between_prepare_and_push_is_preserved(self):
        self.run.begin('A')
        self.run.factor('A', '0')
        self.run.prepare('sf', 'A', '0')
        # Another writer creates the formerly missing branch at a different commit.
        command(['git', 'push', 'origin', self.p.state['outputBaseCommit'] + ':refs/heads/' + self.p.state['branch']], self.p.output)
        with self.assertRaisesRegex(ValueError, 'Destination changed'):
            self.run.publish()
        self.assertEqual(remote_head(self.p.output, self.p.state['branch']), self.p.state['outputBaseCommit'])

    def test_failed_push_can_be_recovered_without_duplicate_sf_commit(self):
        self.run.begin('A')
        self.run.factor('A', '0')
        self.run.prepare('sf', 'A', '0')
        expected = self.run.head()
        with patch('ci.incremental.remote_head', side_effect=RuntimeError('Synthetic network outage')):
            with self.assertRaises(RuntimeError):
                self.run.publish()
        self.run.prepare('sf', 'A', '0')
        self.run.publish()
        self.assertEqual(self.run.head(), expected)
        self.assertEqual(len(self.run.data()['events']), 1)

    def test_pre_push_checkpoint_can_resume_on_a_fresh_runner(self):
        self.run.begin('A')
        self.run.factor('A', '0')
        self.run.prepare('sf', 'A', '0')
        expected = self.run.head()
        seal(self.p)
        with patch.dict(os.environ, {'DRY_RUN': 'true'}):
            fresh = restore_on_fresh_runner(self.p, Path(self.temp.name) / 'fresh')
        p = RefinementPipeline()
        p.__dict__.update(fresh.__dict__)
        runner = Incremental(p)
        runner.publish()
        self.assertEqual(runner.head(), expected)
        self.assertTrue(runner.begin('A'))
        with patch.object(p, 'fields_task', side_effect=AssertionError('A checked SF was regenerated')):
            runner.factor('A', '0')
        self.assertFalse(runner.prepare('sf', 'A', '0'))

    def test_explicit_cross_run_resume_keeps_pins_and_records_controller_upgrade(self):
        ancestor = command(['git', 'rev-parse', 'HEAD^'], self.p.trusted).stdout.strip()
        self.p.state['controllerCommit'] = ancestor
        self.p.save()
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '41', 'GITHUB_RUN_ATTEMPT': '1'}):
            seal(self.p)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '42', 'RESUME_RUN_ID': '41', 'DRY_RUN': 'true'}):
            fresh = restore_on_fresh_runner(self.p, Path(self.temp.name) / 'fresh')
        self.assertEqual(fresh.state['inputManifest'], self.p.state['inputManifest'])
        self.assertEqual(fresh.state['refinementPlan'], self.p.state['refinementPlan'])
        self.assertEqual(fresh.state['resumeLineage'][-1]['sourceControllerCommit'], ancestor)
        self.assertNotEqual(fresh.state['controllerCommit'], ancestor)

    def test_cross_run_restore_requires_explicit_source_selection(self):
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '41', 'GITHUB_RUN_ATTEMPT': '1'}):
            seal(self.p)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '42', 'RESUME_RUN_ID': '', 'DRY_RUN': 'true'}):
            with self.assertRaisesRegex(ValueError, 'another run'):
                restore_on_fresh_runner(self.p, Path(self.temp.name) / 'fresh')

    def test_locator_uses_source_run_only_until_the_new_run_has_a_checkpoint(self):
        output = Path(self.temp.name) / 'github-output.txt'
        artifact = {'id': 8, 'name': 'uniq2me-checkpoint-41-refine-1',
                    'created_at': '2026-10-19T19:39:23Z', 'expired': False}
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_REPOSITORY': 'fixture/fixture', 'GITHUB_TOKEN': 'synthetic-token',
            'GITHUB_OUTPUT': str(output), 'RESUME_RUN_ID': '41', 'FRESH_START': 'false'}):
            with patch('ci.checkpoints.http_json', side_effect=lambda url, **kwargs:
                       {'artifacts': [] if '/42/' in url else [artifact]}):
                locate()
            self.assertIn('artifact_run_id=41', output.read_text())
            output.write_text('')
            current = {**artifact, 'id': 9, 'name': 'uniq2me-checkpoint-42-refine-1'}
            with patch('ci.checkpoints.http_json', return_value={'artifacts': [current]}) as api:
                locate()
            self.assertEqual(api.call_count, 1)
            self.assertIn('artifact_run_id=42', output.read_text())

    def test_interrupted_task_missing_from_pending_register_is_recovered(self):
        self.p.state['modelRetryQueue'] = {'refine-complete_fields/00A0-fields': {
            'key': '00A0-fields', 'task': 'complete_fields', 'sfsId': 'A',
            'order': ['a', 'b', 'c'], 'attemptedTags': ['qwen3:235b']}}
        self.run.data()
        pending = self.p.state['refinementPending']['00A0']
        self.assertEqual(pending['status'], 'pending-provider-retry')
        self.assertEqual(pending['tasks'], ['complete_fields'])

    def test_prepared_squash_recovers_after_a_successful_remote_push(self):
        self.run.begin('A')
        for sf in '0123456789ABCDEF':
            self.sf(sf)
        self.run.finish_sfs('A')
        self.run.prepare('sfs', 'A')
        expected = self.run.head()
        seal(self.p)
        # Simulate loss of the final state save after the remote accepted the push.
        with patch.object(self.p, 'save', side_effect=RuntimeError('Lost state save')):
            with self.assertRaisesRegex(RuntimeError, 'Lost state save'):
                self.run.publish()
        with patch.dict(os.environ, {'DRY_RUN': 'true'}):
            fresh = restore_on_fresh_runner(self.p, Path(self.temp.name) / 'fresh')
        p = RefinementPipeline()
        p.__dict__.update(fresh.__dict__)
        runner = Incremental(p)
        runner.publish()
        self.assertEqual(runner.head(), expected)
        self.assertFalse(runner.begin('A'))

    def test_tampered_factor_cannot_be_published(self):
        self.run.begin('A')
        self.run.factor('A', '0')
        path = self.p.work / 'repo-sf/en/00A0.json'
        path.write_text(path.read_text().replace('SYNTHETIC DRY RUN', 'Unapproved change'))
        with self.assertRaisesRegex(ValueError, 'Checked SF bytes changed'):
            self.run.prepare('sf', 'A', '0')

    def test_a_later_sf_cannot_publish_a_tampered_earlier_sf(self):
        self.run.begin('A')
        self.sf('0')
        self.run.factor('A', '1')
        path = self.p.work / 'repo-sf/en/00A0.json'
        path.write_text(path.read_text().replace('SYNTHETIC DRY RUN', 'Unapproved change'))
        with self.assertRaisesRegex(ValueError, 'Earlier visible SF changed'):
            self.run.prepare('sf', 'A', '1')

    def test_all_tasks_and_translations_keep_the_final_catalog_structure(self):
        self.p.state['branch'] = 'en/fixture'
        self.p.state['refinementPlan']['tasks']['complete_fields']['enabled'] = False
        for task in ('rework_questions', 'ai_prompts', 'canonical_solutions', 'translate_fr', 'translate_el'):
            self.p.state['refinementPlan']['tasks'][task]['enabled'] = True
        self.p.save()
        self.run.begin('A')
        for sf in '0123456789ABCDEF':
            self.sf(sf)
        self.run.finish_sfs('A')
        self.run.prepare('sfs', 'A')
        self.run.publish()
        for language in ('en', 'fr', 'el'):
            record = read(self.p.output / 'repo-sf' / language / '00A0.json')
            self.assertEqual(record['identity']['language'], language)
            self.assertTrue((self.p.output / 'solutions' / language / '00A0.json').exists())
        self.assertEqual(read(self.p.output / 'site-publication.json')['languages'], ['en', 'fr', 'el'])
        for language in ('en', 'fr', 'el'):
            self.assertEqual(remote_head(self.p.output, language + '/fixture'), self.run.head())

    def test_local_only_run_does_not_touch_remote(self):
        self.p.state['publish'] = False
        self.run.begin('A')
        self.sf('0')
        self.assertIsNone(remote_head(self.p.output, self.p.state['branch']))

    def test_squash_refuses_a_foreign_commit(self):
        self.run.begin('A')
        for sf in '0123456789ABCDEF':
            self.sf(sf)
        (self.p.output / 'owner-note.txt').write_text('Independent edit')
        command(['git', 'add', 'owner-note.txt'], self.p.output)
        command(['git', '-c', 'user.name=Owner', '-c', 'user.email=owner@example.invalid', 'commit', '-qm', 'Owner edit'], self.p.output)
        self.run.finish_sfs('A')
        with self.assertRaisesRegex(ValueError, 'not owned'):
            self.run.prepare('sfs', 'A')


class ProviderErrorTests(unittest.TestCase):
    def test_observed_laguna_error_is_retryable_but_auth_and_unknown_errors_are_fatal(self):
        observed = {'name': 'APIError', 'data': {'message': 'empty Laguna tool call name',
            'statusCode': 500, 'isRetryable': True}}
        self.assertIsInstance(model_error(observed), RetryableModelError)
        for value in (None, 'unexpected', {'name': 'APIError', 'data': {'statusCode': 401, 'isRetryable': True}},
                      {'name': 'APIError', 'data': {'statusCode': 500, 'isRetryable': False}},
                      {'name': 'UnknownError', 'data': observed['data']}):
            self.assertNotIsInstance(model_error(value), RetryableModelError)

    def test_actual_opencode_error_envelope_is_typed_aborted_and_archived(self):
        from ci.runtime import OpenCode
        import json
        with tempfile.TemporaryDirectory() as directory:
            server = OpenCode(directory, ROOT, {}, directory, {'tag': 'laguna-s-2.1:q4_K_M',
                'digest': 'a'*64, 'effectiveContext': 32768})
            transcript = [{'info': {'role': 'assistant', 'providerID': 'ollama',
                'modelID': 'laguna-s-2.1:q4_K_M', 'error': {'name': 'APIError', 'data': {
                    'statusCode': 500, 'isRetryable': True, 'message': 'empty Laguna tool call name'}}}, 'parts': []}]
            calls = []
            def api(path, data=None, **kwargs):
                calls.append(path)
                if path == '/session':
                    return {'id': 'synthetic-laguna'}
                if path == '/session/status':
                    return {}
                if path.endswith('/message'):
                    return transcript
            server.api = api
            with redirect_stdout(io.StringIO()), self.assertRaises(RetryableModelError):
                server.ask('catalog-validator', 'Synthetic preflight', 30, 'observed-error')
            self.assertIn('/session/synthetic-laguna/abort', calls)
            self.assertEqual(json.loads((Path(directory)/'calls/observed-error/messages.json').read_text()), transcript)


if __name__ == '__main__':
    unittest.main()
