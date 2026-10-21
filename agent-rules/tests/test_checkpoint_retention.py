"""Checkpoint rotation against an in-memory GitHub API, never real artifacts."""
import copy
import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import yaml
from ci.checkpoint_retention import main, prune

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = 'stefanospetsios/uniq2me-dora'
TOKEN = 'synthetic-artifact-token'


def artifact(identifier, age, *, run=42, expired=False, name=None):
    return {'id': identifier, 'name': name or f'uniq2me-checkpoint-{run}-sf-{identifier}-1',
            'created_at': (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=age)).isoformat(),
            'expired': expired, 'digest': 'sha256:' + f'{identifier:064x}', 'workflow_run': {'id': run}}


class CheckpointRetentionTests(unittest.TestCase):
    def setUp(self):
        self.rows = [artifact(900, 1), artifact(88, 2), artifact(3, 3), artifact(2, 4)]
        self.calls = []
        self.delete_errors, self.missing_on_delete = {}, set()
        self.rebuild_store()
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        for target, kwargs in (('ci.checkpoint_retention.http_json', {'side_effect': self.api}),
                               ('ci.checkpoint_retention.time.sleep', {})):
            mock = patch(target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def rebuild_store(self):
        self.store = {a['id']: copy.deepcopy(a) for a in self.rows}

    def api(self, url, *, method, headers, **kwargs):
        self.calls.append((method, url))
        self.assertEqual(headers['Authorization'], 'Bearer ' + TOKEN)
        self.assertTrue(url.startswith('https://api.github.com/repos/' + REPOSITORY + '/'))
        parsed = urlsplit(url)
        if '/actions/runs/42/artifacts' in parsed.path:
            self.assertEqual(method, 'GET')
            page = int(parse_qs(parsed.query)['page'][0])
            return {'artifacts': copy.deepcopy(self.rows[(page-1)*100:page*100]), 'total_count': len(self.rows)}
        identifier = int(parsed.path.rsplit('/', 1)[1])
        if method == 'DELETE':
            if identifier in self.delete_errors:
                raise HTTPError(url, self.delete_errors[identifier], 'Do not print synthetic provider detail', {}, None)
            if identifier in self.missing_on_delete:
                self.store.pop(identifier, None)
                self.rows = [a for a in self.rows if a['id'] != identifier]
            if identifier not in self.store:
                raise HTTPError(url, 404, 'Synthetic already deleted', {}, None)
            self.store.pop(identifier)
            self.rows = [a for a in self.rows if a['id'] != identifier]
            return None
        self.assertEqual(method, 'GET')
        return copy.deepcopy(self.store[identifier])

    def prune(self, **kwargs):
        values = {'repository': REPOSITORY, 'run_id': '42', 'uploaded_id': '2',
                  'uploaded_digest': f'{2:064x}', 'token': TOKEN}
        values.update(kwargs)
        return prune(**values)

    def deletes(self):
        return [int(url.rsplit('/', 1)[1]) for method, url in self.calls if method == 'DELETE']

    def test_creation_time_wins_over_id_and_other_artifacts_and_runs_are_untouched(self):
        others = [artifact(800, 0, run=41), artifact(700, 8, name='source-download-retries-42-1'),
                  artifact(600, 9, name='uniq2me-checkpoint-export-42')]
        self.rows.extend(others)
        self.rebuild_store()
        result = self.prune()
        self.assertEqual(result, {'status': 'complete', 'kept': [2, 3], 'deleted': [900, 88]})
        self.assertEqual(set(self.store), {2, 3, 800, 700, 600})

    def test_every_page_is_read_before_deleting_any_checkpoint(self):
        self.rows = [artifact(9000 + n, n) for n in range(105)] + [artifact(2, 200)]
        self.rebuild_store()
        result = self.prune()
        self.assertEqual(result['kept'], [2, 9104])
        self.assertEqual(len(result['deleted']), 104)
        page_two = next(i for i, (_, url) in enumerate(self.calls) if 'page=2' in url)
        first_delete = next(i for i, (method, _) in enumerate(self.calls) if method == 'DELETE')
        self.assertLess(page_two, first_delete)
        self.assertEqual(set(self.store), {2, 9104})

    def test_expired_checkpoint_never_displaces_a_usable_recovery_point(self):
        self.rows.append(artifact(1000, 900, expired=True))
        self.rebuild_store()
        result = self.prune()
        self.assertEqual(result['kept'], [2, 3])
        self.assertIn(1000, result['deleted'])

    def test_two_or_fewer_checkpoints_do_not_issue_deletes(self):
        for rows in ([artifact(2, 4)], [artifact(3, 3), artifact(2, 4)]):
            with self.subTest(count=len(rows)):
                self.rows = rows
                self.rebuild_store()
                self.assertEqual(self.prune()['deleted'], [])
        self.assertEqual(self.deletes(), [])

    def test_failed_or_missing_upload_outputs_never_even_contact_github(self):
        for values in ({'uploaded_id': ''}, {'uploaded_id': '../2'}, {'uploaded_digest': ''},
                       {'uploaded_digest': 'not-a-digest'}, {'token': ''}, {'run_id': '../42'},
                       {'repository': 'stefanospetsios/uniq2me-ci'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.prune(**values)
        self.assertEqual(self.calls, [])

    def test_wrong_digest_blocks_all_deletions(self):
        with self.assertRaisesRegex(ValueError, 'digest does not match'):
            self.prune(uploaded_digest='f' * 64)
        self.assertEqual(self.deletes(), [])

    def test_wrong_run_or_artifact_kind_blocks_all_deletions(self):
        for changes in ({'workflow_run': {'id': 43}}, {'name': 'source-download-retries-42-1'},
                        {'expired': True}, {'id': 55}):
            with self.subTest(changes=changes):
                self.rebuild_store()
                self.store[2].update(changes)
                with self.assertRaises(ValueError):
                    self.prune()
        self.assertEqual(self.deletes(), [])

    def test_upload_not_visible_in_inventory_leaves_all_old_checkpoints(self):
        self.rows = self.rows[:-1]  # The metadata endpoint has it; listing is not consistent yet.
        with self.assertRaisesRegex(ValueError, 'not yet consistently visible'):
            self.prune()
        self.assertEqual(self.deletes(), [])

    def test_unverifiable_previous_recovery_point_blocks_cleanup_before_first_delete(self):
        self.rows[2]['digest'] = None
        with self.assertRaisesRegex(ValueError, 'retained checkpoint has no verifiable digest'):
            self.prune()
        self.assertEqual(self.deletes(), [])

    def test_270_consecutive_uploads_keep_two_recovery_points_with_bounded_api_use(self):
        self.rows, self.store = [], {}
        for age in range(270):
            item = artifact(1000 + age, age)
            self.rows.append(item)
            self.store[item['id']] = copy.deepcopy(item)
            result = self.prune(uploaded_id=str(item['id']), uploaded_digest=item['digest'])
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(set(self.store), {1000 + n for n in range(max(0, age-1), age+1)})
        self.assertLessEqual(len(self.calls), 270 * 3)

    def test_concurrent_newer_uploads_defer_cleanup_in_the_older_job(self):
        self.rows.extend([artifact(40, 5), artifact(50, 6)])
        self.rebuild_store()
        result = self.prune()
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['kept'], [40, 50])
        self.assertEqual(self.deletes(), [])

    def test_duplicate_inventory_cannot_trigger_deletion_from_a_moving_snapshot(self):
        self.rows.append(copy.deepcopy(self.rows[0]))
        with self.assertRaisesRegex(ValueError, 'changed while paginating'):
            self.prune()
        self.assertEqual(self.deletes(), [])

    def test_missing_artifact_during_delete_is_an_idempotent_success(self):
        self.missing_on_delete.add(900)
        result = self.prune()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(set(self.store), {2, 3})

    def test_read_only_token_and_partial_api_failure_warn_without_blocking_publication(self):
        env = {'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_RUN_ID': '42', 'CHECKPOINT_ARTIFACT_ID': '2',
               'CHECKPOINT_ARTIFACT_DIGEST': f'{2:064x}', 'GITHUB_TOKEN': TOKEN,
               'GITHUB_API_URL': 'https://api.github.com'}
        for identifier, code in ((900, 403), (88, 503)):
            with self.subTest(code=code):
                self.rebuild_store()
                self.delete_errors = {identifier: code}
                output = io.StringIO()
                with patch.dict(os.environ, env), redirect_stdout(output):
                    self.assertIsNone(main())
                self.assertIn('::warning', output.getvalue())
                self.assertIn(str(code), output.getvalue())
                self.assertNotIn(TOKEN, output.getvalue())
                self.assertNotIn('synthetic provider detail', output.getvalue())
                self.assertIn(2, self.store)
                self.assertIn(3, self.store)
                if code == 503:
                    self.assertNotIn(900, self.store)  # A retry can finish this partial cleanup.


class RetentionWorkflowTests(unittest.TestCase):
    def document(self, path):
        return yaml.load((ROOT / path).read_text(), Loader=yaml.BaseLoader)

    def test_both_upload_paths_pass_successful_upload_identity_and_scope_the_cleanup_token(self):
        for path in ('.github/actions/catalog-publication/action.yml', '.github/actions/stage/action.yml'):
            with self.subTest(path=path):
                steps = self.document(path)['runs']['steps']
                upload = next(s for s in steps if s.get('uses') == 'actions/upload-artifact@v4')
                cleanup = next(s for s in steps if 'checkpoint_retention.py' in s.get('run', ''))
                self.assertEqual(upload['id'], 'checkpoint')
                self.assertLess(steps.index(upload), steps.index(cleanup))
                self.assertEqual(cleanup['if'], "${{ always() && steps.checkpoint.outcome == 'success' }}")
                self.assertEqual(cleanup['env']['GITHUB_TOKEN'], '${{ github.token }}')
                self.assertEqual(cleanup['env']['CHECKPOINT_ARTIFACT_ID'], '${{ steps.checkpoint.outputs.artifact-id }}')
                self.assertEqual(cleanup['env']['CHECKPOINT_ARTIFACT_DIGEST'], '${{ steps.checkpoint.outputs.artifact-digest }}')
                self.assertNotIn('PUSH_TOKEN', cleanup['env'])

    def test_callers_and_all_reusable_levels_allow_artifact_deletion_but_public_checks_stay_read_only(self):
        for path in ('examples/air.yml', 'examples/dora.yml', '.github/workflows/catalog.yml', '.github/workflows/stage.yml'):
            self.assertEqual(self.document(path)['permissions'], {'contents': 'read', 'actions': 'write'})
        self.assertEqual(self.document('.github/workflows/validate.yml')['permissions'], {'contents': 'read'})
