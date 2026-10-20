"""Exercise production dry mode and crash recovery against local Git remotes."""
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
from ci.checkpoints import select_artifact, seal
from ci.contracts import byte_digest, read
from ci.pipeline import STAGES, command, remote_head
from ci.smoke_test import create_fixture_pipeline, restore_on_fresh_runner
from ci.dry_run import candidate


ENV = {'PUSH_TOKEN': 'synthetic-local-git-token', 'DRY_RUN': 'true',
       'GITHUB_OUTPUT': '', 'GITHUB_STEP_SUMMARY': ''}


class CheckpointRecovery(unittest.TestCase):
    def test_selects_latest_nonexpired_checkpoint_from_same_run(self):
        rows = [{'id': 1, 'name': 'uniq2me-checkpoint-42-catalog-1', 'expired': False},
                {'id': 2, 'name': 'uniq2me-checkpoint-42-extended-1', 'expired': False},
                {'id': 3, 'name': 'uniq2me-checkpoint-41-masks-2', 'expired': False},
                {'id': 4, 'name': 'uniq2me-checkpoint-42-masks-2', 'expired': True},
                {'id': 5, 'name': 'uniq2me-failure-42-masks-2', 'expired': False}]
        for row in rows:
            row['created_at'] = f'2026-10-13T13:00:0{row["id"]}Z'
        self.assertEqual(select_artifact(rows, '42')['id'], 2)
        self.assertIsNone(select_artifact(rows, '43'))

    def test_checkpoint_ids_do_not_define_creation_order(self):
        # Regression from run 38400808303: masks restored catalog although the
        # extended checkpoint was newer and its commit was already published.
        rows = [
            {'id': 11816391518, 'name': 'uniq2me-checkpoint-38400808303-catalog-2',
             'created_at': '2026-10-13T13:08:15Z'},
            {'id': 11815433052, 'name': 'uniq2me-checkpoint-38400808303-extended-2',
             'created_at': '2026-10-13T13:08:48Z'},
        ]
        self.assertEqual(select_artifact(rows, '38400808303')['id'], 11815433052)
        rows.append({'id': 11816272389, 'name': 'uniq2me-checkpoint-38400808303-masks-2',
                     'created_at': '2026-10-13T13:09:17Z'})
        self.assertEqual(select_artifact(rows, '38400808303')['id'], 11816272389)

    def test_checkpoint_timestamp_ties_have_a_stable_selection(self):
        rows = [dict(id=i, name=f'uniq2me-checkpoint-42-catalog-{i}',
                     created_at='2026-10-13T13:08:15Z') for i in (2, 1)]
        self.assertEqual(select_artifact(rows, '42')['id'], 2)

    def test_fresh_attempt_ignores_old_work_but_keeps_current_stage_handoff(self):
        rows = [dict(id=i, name=f'uniq2me-checkpoint-42-{stage}-{attempt}',
                     created_at=f'2026-10-13T13:08:0{i}Z') for i, stage, attempt in
                ((1, 'preflight', 2), (2, 'catalog', 1), (3, 'prepare', 2))]
        # A late old-attempt upload cannot replace this attempt's handoff.
        rows[1]['created_at'] = '2026-10-13T14:00:00Z'
        self.assertEqual(select_artifact(rows, '42')['id'], 2)
        self.assertEqual(select_artifact(rows, '42', attempt='2')['id'], 3)
        self.assertIsNone(select_artifact(rows, '42', attempt='3'))

    def test_dry_run_resumes_partial_tasks_and_finishes_exactly_five_commits(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), \
             redirect_stdout(io.StringIO()), \
             patch('ci.pipeline.ollama_preflight', side_effect=AssertionError('Dry run invoked Ollama')), \
             patch('ci.runtime.OpenCode.start', side_effect=AssertionError('Dry run started an agent')):
            root = Path(directory)
            pipeline, bare = create_fixture_pipeline(root)
            pipeline.state['failOnceAt'] = 'extended'
            pipeline.save()
            pipeline.execute('prepare')
            pipeline.execute('catalog')
            pipeline.publish('catalog')
            catalog_commit = pipeline.state['publications'][0]['commit']
            with self.assertRaisesRegex(RuntimeError, 'Intentional one-time'):
                pipeline.execute('extended')
            self.assertEqual(len(list((pipeline.work / '.task-cache/solutions').glob('*.json'))), 8)
            seal(pipeline)
            resumed = restore_on_fresh_runner(pipeline, root / 'fresh-runner')
            with patch('ci.dry_run.candidate', wraps=candidate) as generated:
                resumed.execute('extended')
                self.assertEqual(generated.call_count, (256 - 8) + 256)
            resumed.publish('extended')
            for stage in ('masks', 'translate-fr', 'translate-el'):
                resumed.execute(stage)
                resumed.publish(stage)
            self.assertEqual([p['stage'] for p in resumed.state['publications']], STAGES)
            self.assertEqual(resumed.state['publications'][0]['commit'], catalog_commit)
            self.assertEqual(command(['git', 'rev-list', '--count', resumed.state['branch']], bare).stdout.strip(), '6')
            with patch('ci.dry_run.candidate', side_effect=AssertionError('Completed stage regenerated')):
                for stage in STAGES:
                    resumed.execute(stage)
                    resumed.publish(stage)
            self.assertEqual(len(resumed.state['publications']), 5)
            manifest = read(resumed.output / 'release-manifest.json')
            self.assertEqual(manifest['status'], 'dry-run-validated')
            self.assertTrue(manifest['synthetic'])
            self.assertEqual(manifest['model']['inferenceCalls'], 0)
            self.assertFalse((resumed.output / 'next-base.json').exists())
            self.assertTrue((resumed.output / 'dry-run-base.json').exists())
            marker = read(resumed.output / 'pipeline-status.json')
            self.assertFalse(marker['multilingualComplete'])
            self.assertTrue(marker['testStagesComplete'])

    def test_push_success_before_lost_acknowledgment_does_not_duplicate_commit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            root = Path(directory)
            pipeline, bare = create_fixture_pipeline(root)
            pipeline.execute('prepare')
            pipeline.execute('catalog')
            def interrupted_ack(repo, branch):
                value = remote_head(repo, branch)
                pending = pipeline.state.get('publicationIntent', {}).get('commit')
                if pending and value == pending:
                    raise RuntimeError('Synthetic failure after push succeeded')
                return value
            with patch('ci.pipeline.remote_head', side_effect=interrupted_ack):
                with self.assertRaisesRegex(RuntimeError, 'after push succeeded'):
                    pipeline.publish('catalog')
            commit = pipeline.state['publicationIntent']['commit']
            self.assertEqual(remote_head(pipeline.output, pipeline.state['branch']), commit)
            self.assertEqual(pipeline.state['publications'], [])
            seal(pipeline)
            resumed = restore_on_fresh_runner(pipeline, root / 'ack-retry')
            resumed.execute('catalog')
            resumed.publish('catalog')
            resumed.publish('catalog')
            self.assertEqual(len(resumed.state['publications']), 1)
            self.assertEqual(resumed.state['publications'][0]['commit'], commit)
            self.assertEqual(command(['git', 'rev-list', '--count', resumed.state['branch']], bare).stdout.strip(), '2')

    def test_seal_recovers_commit_created_before_its_journal_was_saved(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            root = Path(directory)
            pipeline, bare = create_fixture_pipeline(root)
            pipeline.execute('prepare')
            pipeline.execute('catalog')
            save = pipeline.save
            def interrupted_save():
                if pipeline.state.get('publicationIntent', {}).get('commit'):
                    raise RuntimeError('Synthetic process loss immediately after commit')
                save()
            with patch.object(pipeline, 'save', side_effect=interrupted_save):
                with self.assertRaisesRegex(RuntimeError, 'immediately after commit'):
                    pipeline.publish('catalog')
            commit = command(['git', 'rev-parse', 'HEAD'], pipeline.output).stdout.strip()
            pipeline.state = read(pipeline.state_path)  # Recover only bytes that actually reached disk.
            self.assertIsNone(pipeline.state['publicationIntent']['commit'])
            seal(pipeline)
            self.assertEqual(pipeline.state['publicationIntent']['commit'], commit)
            resumed = restore_on_fresh_runner(pipeline, root / 'journal-retry')
            resumed.publish('catalog')
            self.assertEqual(resumed.state['publications'][0]['commit'], commit)
            self.assertEqual(command(['git', 'rev-list', '--count', resumed.state['branch']], bare).stdout.strip(), '2')

    def test_tampered_checkpoint_is_rejected_before_checkout(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            root = Path(directory)
            pipeline, _ = create_fixture_pipeline(root)
            pipeline.execute('prepare')
            seal(pipeline)
            (pipeline.work / 'frozen/sources.json').write_text('{}\n')
            with self.assertRaisesRegex(ValueError, 'file hashes'):
                restore_on_fresh_runner(pipeline, root / 'tampered')

    def test_local_commit_only_dry_run_can_move_between_runners(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            root = Path(directory)
            pipeline, _ = create_fixture_pipeline(root, publish=False)
            pipeline.execute('prepare')
            pipeline.execute('catalog')
            pipeline.publish('catalog')
            self.assertIsNone(remote_head(pipeline.output, pipeline.state['branch']))
            commit = pipeline.state['localHead']
            seal(pipeline)
            resumed = restore_on_fresh_runner(pipeline, root / 'local-commit-retry')
            self.assertEqual(command(['git', 'rev-parse', 'HEAD'], resumed.output).stdout.strip(), commit)
            self.assertFalse(resumed.state['publications'][0]['pushed'])

    def test_unrelated_remote_commit_is_rejected_with_commit_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), redirect_stdout(io.StringIO()):
            root = Path(directory)
            pipeline, _ = create_fixture_pipeline(root)
            pipeline.execute('prepare')
            pipeline.execute('catalog')
            pipeline.publish('catalog')
            expected = pipeline.state['expectedRemote']
            seal(pipeline)
            (pipeline.output / 'unrelated.txt').write_text('Another writer changed this branch.\n')
            command(['git', 'add', 'unrelated.txt'], pipeline.output)
            command(['git', '-c', 'user.name=Other writer', '-c',
                     'user.email=other@example.invalid', 'commit', '-qm', 'Unrelated change'], pipeline.output)
            observed = command(['git', 'rev-parse', 'HEAD'], pipeline.output).stdout.strip()
            command(['git', 'push', 'origin', 'HEAD:refs/heads/' + pipeline.state['branch']], pipeline.output)
            with self.assertRaises(ValueError) as caught:
                restore_on_fresh_runner(pipeline, root / 'unrelated-remote')
            self.assertIn(pipeline.state['branch'], str(caught.exception))
            self.assertIn('expected=' + expected, str(caught.exception))
            self.assertIn('observed=' + observed, str(caught.exception))
            self.assertEqual(remote_head(pipeline.output, pipeline.state['branch']), observed)


if __name__ == '__main__':
    unittest.main()
