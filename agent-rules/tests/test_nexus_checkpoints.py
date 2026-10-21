"""Offline storage failures, safe recovery and caller wiring; no live services."""
import hashlib
import io
import json
import os
import sys
import tempfile
import urllib.error
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci import checkpoints
from ci.contracts import byte_digest, file_manifest, write
from ci.nexus_checkpoints import Nexus, NoRedirect, download, probe, records, upload

REPO = 'stefanospetsios/uniq2me-dora'
ENV = {'GITHUB_REPOSITORY': REPO, 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
       'RESUME_RUN_ID': '', 'FRESH_START': 'false', 'CHECKPOINT_URL': 'https://storage.example.invalid/repository/checkpoints/',
       'CHECKPOINT_USER': 'checkpoint', 'CHECKPOINT_PASS': 'synthetic-storage-credential',
       'GITHUB_TOKEN': 'synthetic-github-token', 'GITHUB_OUTPUT': '', 'STAGE': 'preflight'}


class Store:
    def __init__(self):
        self.data = {}
        self.deleted = []
        self.fail_put = self.corrupt_get = ''

    def get(self, path):
        return b'corrupt' if self.corrupt_get and path.endswith(self.corrupt_get) else self.data[path]

    def put(self, path, body):
        if self.fail_put and path.endswith(self.fail_put):
            raise ValueError('synthetic storage failure')
        self.data[path] = body

    def assets(self):
        return [{'path': p, 'id': p, 'fileSize': len(b),
                 'checksum': {'sha256': hashlib.sha256(b).hexdigest()}} for p, b in self.data.items()]

    def delete(self, row):
        self.deleted.append(row['path'])
        self.data.pop(row['path'], None)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.root.mkdir()
        (self.root / '.hidden').write_text('hidden checkpoint content')
        (self.root / 'output.bundle').write_bytes(b'synthetic bundle')
        (self.root / 'work').mkdir()
        (self.root / 'work' / 'task.json').write_text('{}')
        self.seal()
        self.store = Store()
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.stdout = redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def seal(self, run='42', attempt='1'):
        files = file_manifest(self.root)
        files.pop('checkpoint-manifest.json', None)
        write(self.root / 'checkpoint-manifest.json', {'recordType': 'ci-checkpoint',
            'workflowRepository': REPO, 'workflowRunId': run, 'workflowAttempt': attempt, 'files': files})

    def save(self, suffix='sf-0-1'):
        return upload(self.store, self.root, 'uniq2me-checkpoint-42-' + suffix)

    def test_roundtrip_preserves_hidden_files_bundle_and_every_manifest_hash(self):
        marker = self.save()
        target = Path(self.temp.name) / 'restored'
        download(self.store, target, marker)
        self.assertEqual(file_manifest(target), file_manifest(self.root))
        self.assertEqual(len(records(self.store, REPO, '42')), 1)

    def test_five_uploads_keep_two_complete_snapshots_and_other_runs_untouched(self):
        self.store.data['unrelated/run/42/data.zip'] = b'untouched'
        markers = [self.save(f'sf-{i}-1') for i in range(5)]
        rows = records(self.store, REPO, '42')
        self.assertEqual({r['markerPath'] for r in rows}, set(markers[-2:]))
        self.assertEqual(len(self.store.deleted), 6)
        self.assertEqual(self.store.data['unrelated/run/42/data.zip'], b'untouched')

    def test_failed_zip_upload_never_deletes_or_replaces_previous_checkpoint(self):
        marker = self.save()
        self.store.fail_put = '.zip'
        with self.assertRaises(ValueError):
            self.save('sf-fail-1')
        self.assertEqual(records(self.store, REPO, '42')[0]['markerPath'], marker)
        self.assertEqual(self.store.deleted, [])

    def test_failed_marker_leaves_orphan_archive_ineligible_for_resume(self):
        marker = self.save()
        self.store.fail_put = '.json'
        with self.assertRaises(ValueError):
            self.save('sf-fail-1')
        self.assertEqual([r['markerPath'] for r in records(self.store, REPO, '42')], [marker])
        self.assertEqual(len(self.store.data), 3)

    def test_corrupt_upload_never_commits_marker(self):
        self.store.corrupt_get = '.zip'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.save()
        self.assertFalse(any(path.endswith('.json') for path in self.store.data))

    def test_source_change_during_packaging_does_not_displace_a_sealed_backup(self):
        original = zipfile.ZipFile.write
        def changing_write(archive, path, *args, **kwargs):
            if Path(path).name == '.hidden':
                Path(path).write_text('changed after validation')
            return original(archive, path, *args, **kwargs)
        with patch('ci.nexus_checkpoints.zipfile.ZipFile.write', new=changing_write):
            with self.assertRaisesRegex(ValueError, 'changed while packaging'):
                self.save()
        self.assertEqual(self.store.data, {})

    def test_corrupt_download_leaves_existing_state_untouched(self):
        marker = self.save()
        self.store.corrupt_get = '.zip'
        target = Path(self.temp.name) / 'restored'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            download(self.store, target, marker)
        self.assertFalse(target.exists())

    def test_does_not_overwrite_nonempty_destination(self):
        marker = self.save()
        with self.assertRaisesRegex(ValueError, 'not empty'):
            download(self.store, self.root, marker)
        self.assertEqual((self.root / '.hidden').read_text(), 'hidden checkpoint content')

    def test_wrong_run_or_repository_is_rejected_before_upload(self):
        for run in ('41',):
            self.seal(run=run)
            with self.assertRaisesRegex(ValueError, 'binding'):
                self.save()
        self.assertEqual(self.store.data, {})

    def test_explicit_resume_downloads_older_run_but_other_run_is_refused(self):
        marker = self.save()
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '43'}):
            with self.assertRaisesRegex(ValueError, 'outside'):
                download(self.store, Path(self.temp.name) / 'restore', marker)
            with patch.dict(os.environ, {'RESUME_RUN_ID': '42'}):
                download(self.store, Path(self.temp.name) / 'restore', marker)

    def test_fresh_attempt_filter_and_download_binding(self):
        marker = self.save()
        self.assertEqual(records(self.store, REPO, '42', attempt='2'), [])
        with patch.dict(os.environ, {'FRESH_START': 'true', 'GITHUB_RUN_ATTEMPT': '2'}):
            with self.assertRaisesRegex(ValueError, 'another attempt'):
                download(self.store, Path(self.temp.name) / 'restore', marker)

    def test_unsafe_archive_cannot_write_outside_staging_even_with_updated_checksum(self):
        marker = self.save()
        record = json.loads(self.store.data[marker])
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('../escaped', 'bad')
        body = raw.getvalue()
        record.update(size=len(body), sha256=hashlib.sha256(body).hexdigest())
        self.store.data[record['zipPath']] = body
        self.store.data[marker] = json.dumps(record).encode()
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            download(self.store, Path(self.temp.name) / 'restore', marker)
        self.assertFalse((Path(self.temp.name) / 'escaped').exists())

    def test_manifest_cannot_be_changed_by_modifying_archive_and_outer_checksum(self):
        marker = self.save()
        record = json.loads(self.store.data[marker])
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('checkpoint-manifest.json', '{}')
        body = raw.getvalue()
        record.update(size=len(body), sha256=hashlib.sha256(body).hexdigest())
        self.store.data[record['zipPath']] = body
        self.store.data[marker] = json.dumps(record).encode()
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            download(self.store, Path(self.temp.name) / 'restore', marker)

    def test_retention_error_keeps_successful_upload_and_old_backups(self):
        self.save()
        with patch('ci.nexus_checkpoints.prune', side_effect=ValueError('delete permission')):
            self.save('sf-second-1')
        self.assertEqual(len(records(self.store, REPO, '42')), 2)
        self.assertEqual(self.store.deleted, [])

    def test_retention_preserves_old_backups_if_retained_asset_metadata_changes(self):
        self.save()
        older = records(self.store, REPO, '42')[0]
        self.store.data[older['zipPath']] = b'x' * older['size']
        self.save('sf-next-1')
        self.assertEqual(self.store.deleted, [])
        self.assertEqual(len([p for p in self.store.data if p.endswith('.json')]), 2)

    def test_real_local_git_bundle_restores_after_nexus_roundtrip(self):
        from ci.checkpoints import seal
        from ci.pipeline import command
        from ci.smoke_test import create_fixture_pipeline, restore_on_fresh_runner
        fixture_root = Path(self.temp.name) / 'git-fixture'
        fixture_root.mkdir()
        with patch.dict(os.environ, {'DRY_RUN': 'true', 'PUBLISH': '', 'CONTROLLER_UPDATES': '',
                'PUSH_TOKEN': 'synthetic-local-token', 'CATALOG_PROFILE': ''}):
            pipeline, _ = create_fixture_pipeline(fixture_root, publish=False)
            pipeline.state['workflowRepository'] = REPO
            pipeline.save()
            expected = command(['git', 'rev-parse', 'HEAD'], pipeline.output).stdout.strip()
            seal(pipeline)
            marker = upload(self.store, pipeline.root, 'uniq2me-checkpoint-42-preflight-1')
            target = Path(self.temp.name) / 'downloaded-git-state'
            download(self.store, target, marker)
            pipeline.root, pipeline.work = target, target / 'work'
            restored = restore_on_fresh_runner(pipeline, Path(self.temp.name) / 'fresh-runner')
            self.assertEqual(command(['git', 'rev-parse', 'HEAD'], restored.output).stdout.strip(), expected)
            self.assertEqual(restored.state['inputManifest'], pipeline.state['inputManifest'])

    def test_probe_checks_roundtrip_and_removes_only_its_own_asset(self):
        self.store.data['unrelated'] = b'keep'
        probe(self.store)
        self.assertEqual(self.store.data, {'unrelated': b'keep'})

    def test_locator_uses_nexus_without_github_when_committed_checkpoint_exists(self):
        self.save()
        output = {}
        with patch('ci.nexus_checkpoints.Nexus', return_value=self.store), \
             patch('ci.checkpoints.http_json', side_effect=AssertionError('GitHub called')), \
             patch('ci.checkpoints.output', side_effect=lambda k, v: output.update({k: v})):
            checkpoints.locate()
        self.assertEqual(output['backend'], 'nexus')
        self.assertEqual(output['artifact_id'], '')
        self.assertTrue(output['marker_path'].endswith('.json'))

    def test_locator_falls_back_to_existing_github_checkpoint_for_migration(self):
        row = {'id': 123, 'name': 'uniq2me-checkpoint-42-refine-1', 'created_at': '2026-10-21T00:00:00Z'}
        output = {}
        with patch('ci.nexus_checkpoints.Nexus', return_value=self.store), \
             patch('ci.checkpoints.http_json', return_value={'artifacts': [row]}), \
             patch('ci.checkpoints.output', side_effect=lambda k, v: output.update({k: v})):
            checkpoints.locate()
        self.assertEqual(output['backend'], 'github')
        self.assertEqual(output['artifact_id'], 123)

    def test_storage_configuration_errors_fail_before_any_http(self):
        for url in ('http://storage.example.invalid/repository/checkpoints/',
                    'https://user:password@storage.example.invalid/repository/checkpoints/',
                    'https://storage.example.invalid/wrong'):
            with patch.dict(os.environ, {'CHECKPOINT_URL': url}):
                with self.assertRaises(ValueError):
                    Nexus()
        with patch.dict(os.environ, {'CHECKPOINT_PASS': ''}):
            with self.assertRaisesRegex(ValueError, 'CHECKPOINT_PASS'):
                Nexus()


class TransportTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.store = Nexus()
        self.response = Mock()
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.response.read.return_value = b'body'
        self.store.opener = Mock()
        self.store.opener.open.return_value = self.response

    def test_put_uses_binary_content_type_and_get_sends_only_authentication(self):
        self.store.put('safe/checkpoint.zip', b'payload')
        request = self.store.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'PUT')
        self.assertEqual(request.get_header('Content-type'), 'application/octet-stream')
        self.assertEqual(request.data, b'payload')
        self.assertTrue(request.get_header('Authorization').startswith('Basic '))
        self.store.get('safe/checkpoint.zip')
        request = self.store.opener.open.call_args.args[0]
        self.assertIsNone(request.get_header('Content-type'))

    def test_transient_http_error_retries_same_request_but_auth_failure_does_not(self):
        for status, expected in ((503, 3), (401, 1), (403, 1), (302, 1)):
            with self.subTest(status=status), patch('ci.nexus_checkpoints.time.sleep'):
                self.store.opener.open.reset_mock()
                self.store.opener.open.side_effect = urllib.error.HTTPError(
                    'https://private.example.invalid/?sensitive', status, 'provider detail', {}, None)
                with self.assertRaisesRegex(ValueError, f'HTTP {status}') as error:
                    self.store.get('checkpoint.zip')
                self.assertEqual(self.store.opener.open.call_count, expected)
                self.assertNotIn('private.example', str(error.exception))
                self.assertNotIn('provider detail', str(error.exception))

    def test_redirect_handler_never_forwards_authorization(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example.invalid/'))

    def test_paginated_inventory_normalizes_leading_slashes_and_rejects_cycles(self):
        pages = [{'items': [{'path': '/one', 'id': 'a'}], 'continuationToken': 'next'},
                 {'items': [{'path': 'two', 'id': 'b'}], 'continuationToken': None}]
        with patch.object(self.store, 'request', side_effect=[json.dumps(p).encode() for p in pages]) as request:
            self.assertEqual([r['path'] for r in self.store.assets()], ['one', 'two'])
            self.assertIn('continuationToken=next', request.call_args.args[0])
        with patch.object(self.store, 'request', return_value=json.dumps(pages[0]).encode()):
            with self.assertRaisesRegex(ValueError, 'repeated'):
                self.store.assets()


class WiringTests(unittest.TestCase):
    def doc(self, path):
        return yaml.load((ROOT / path).read_text(), Loader=yaml.BaseLoader)

    def test_secret_is_forwarded_at_each_bootstrap_boundary(self):
        for profile in ('air', 'dora'):
            job = self.doc(f'examples/{profile}.yml')['jobs']['catalog']
            self.assertEqual(job['secrets']['CHECKPOINT_PASS'], '${{ secrets.CHECKPOINT_PASS }}')
        for job in self.doc('.github/workflows/catalog.yml')['jobs'].values():
            self.assertEqual(job['secrets']['CHECKPOINT_PASS'], '${{ secrets.CHECKPOINT_PASS }}')
        stage = self.doc('.github/workflows/stage.yml')['jobs']['stage']
        self.assertNotIn('CHECKPOINT_PASS', stage['env'])
        self.assertEqual(stage['steps'][-1]['with']['checkpoint_pass'], '${{ secrets.CHECKPOINT_PASS }}')

    def test_every_nested_publication_forwards_nexus_without_exporting_credentials_to_inference(self):
        for path in ROOT.glob('.github/actions/*/action.yml'):
            for step in yaml.load(path.read_text(), Loader=yaml.BaseLoader)['runs']['steps']:
                if step.get('uses', '').startswith('./controller/.github/actions/'):
                    self.assertEqual(step['with']['checkpoint_pass'], '${{ inputs.checkpoint_pass }}')
                if 'incremental.py factor' in step.get('run', '') or 'finish-sfs' in step.get('run', ''):
                    self.assertNotIn('CHECKPOINT_PASS', step.get('env', {}))

    def test_uploads_are_mutually_exclusive_and_nexus_precedes_publication(self):
        for action in ('stage', 'catalog-publication'):
            steps = self.doc(f'.github/actions/{action}/action.yml')['runs']['steps']
            nexus = next(s for s in steps if s.get('id') == 'nexus_upload')
            github = next(s for s in steps if s.get('id') == 'checkpoint_upload')
            self.assertIn("inputs.checkpoint_url != ''", nexus['if'])
            self.assertIn("inputs.checkpoint_url == ''", github['if'])
            if action == 'catalog-publication':
                publish = next(s for s in steps if s.get('run') == 'python controller/ci/incremental.py publish')
                self.assertLess(steps.index(nexus), steps.index(publish))
                self.assertNotIn('always()', publish['if'])
