"""Website previews and actual Git publication/recovery, using local bare remotes."""
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci.contracts import HEX, file_manifest, read, same_shape, write
from ci.site_views import configure_workflow, site_view, template_skeleton, write_view
from ci.site_publish import publish_tree
from test_pipeline import FIXTURES, question, sf, solution


WORKFLOW = """name: Website
on: [push]
env:
  NEXT_TELEMETRY_DISABLED: '1'
jobs:
  build:
    runs-on: self-hosted
    steps:
      - run: |
          node uniq2me-pages/ci/checkout-catalogs.mjs --config uniq2me-pages/repositories/branches.json
"""


class WebsiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / 'work'
        self.state = {'runId': 'synthetic-site', 'branch': 'qwen3/test', 'ragMode': 'disabled',
                      'controllerCommit': '1' * 40, 'inputCommit': '2' * 40,
                      'stages': {}, 'publications': [], 'catalogPending': {}, 'catalogRosterPending': None}
        for sfs in HEX:
            area = read(FIXTURES / 'sfs-template.json')
            for entry in area['set']:
                entry['repoId'] = '00' + sfs + entry['id']
                slot = {'sfsId': sfs, 'sfId': entry['id'], 'repoId': entry['repoId']}
                template = read(FIXTURES / 'sf-template.json')
                template['identity'] = {**slot, 'language': 'en'}
                write(self.work / f'inputs/repo-sf/en/{slot["repoId"]}.json', template)
                write(self.work / f'catalog/questions/en/{slot["repoId"]}.json', question(slot))
            write(self.work / f'inputs/json/en/{sfs}.json', area)
        self.gate('catalog', ['catalog'])

    def gate(self, stage, paths):
        files = {str(Path(path) / name): checksum for path in paths
                 for name, checksum in file_manifest(self.work / path).items()}
        self.state['stages'][stage] = {'files': files}

    def git(self, repo, *args):
        return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_question_preview_preserves_exact_text_and_marks_pending_without_a_solution(self):
        key = '001E'
        self.state['catalogPending'][key] = {'reason': 'Synthetic rejected review'}
        self.state['catalogRosterPending'] = 'Pending questions'
        write(self.work / f'catalog/questions/en/{key}.json', {
            'recordType': 'pending-question', 'shortBinaryQuestion': 'Pending placeholder'})
        self.gate('catalog', ['catalog'])
        # These failed/partial extended files must never be presented as published work.
        write(self.work / 'solutions/en/0000.json', {'steps': ['UNCHECKED SOLUTION']})
        write(self.work / 'repo-sf/en/0000.json', {'question': {'plain': 'UNCHECKED SF'}})
        before = file_manifest(self.work)
        view = site_view(self.work, self.state, 'catalog')
        self.assertEqual(len([p for p in view if p.startswith('repo-sf/en/')]), 256)
        current = json.loads(view['repo-sf/en/0000.json'])
        self.assertEqual(current['question']['shortBinaryQuestion'],
                         read(self.work / 'catalog/questions/en/0000.json')['shortBinaryQuestion'])
        self.assertFalse(current['isPlaceholder'])
        self.assertIsNone(current['resultScore'])
        self.assertEqual(current['review'], {'status': 'draft', 'approvals': []})
        same_shape(current, read(self.work / 'inputs/repo-sf/en/0000.json'))
        self.assertTrue(json.loads(view[f'repo-sf/en/{key}.json'])['isPlaceholder'])
        self.assertFalse(any(p.startswith('solutions/') for p in view))
        self.assertNotIn(b'UNCHECKED', b'\n'.join(view.values()))
        self.assertEqual(before, file_manifest(self.work))
        self.assertIn(b'255 model-reviewed questions; 1 pending', view['pages/catalog/index.en-US.mdx'])

    def test_completed_view_preserves_gated_sf_bytes_and_displays_only_gated_solution(self):
        slot = {'sfsId': '0', 'sfId': '0', 'repoId': '0000'}
        q = read(self.work / 'catalog/questions/en/0000.json')
        write(self.work / 'repo-sf/en/0000.json', sf(slot, q))
        write(self.work / 'solutions/en/0000.json', solution(slot, q))
        self.gate('extended', ['repo-sf/en', 'solutions/en'])
        view = site_view(self.work, self.state, 'extended')
        self.assertEqual(view['repo-sf/en/0000.json'], (self.work / 'repo-sf/en/0000.json').read_bytes())
        self.assertIn(b'example-tool --version', view['pages/catalog/sets/0/0.en-US.mdx'])
        self.assertTrue(json.loads(view['site-status/en/0000.json'])['sfAuthoringComplete'])
        self.assertFalse(json.loads(view['site-status/en/0001.json'])['sfAuthoringComplete'])

    def test_pending_completed_record_keeps_its_checked_bytes_and_catalog_binding(self):
        key = '0000'
        self.state['catalogPending'][key] = {'reason': 'Synthetic pending question'}
        template = read(self.work / 'inputs/repo-sf/en/0000.json')
        write(self.work / 'repo-sf/en/0000.json', template)
        self.gate('extended', ['repo-sf/en'])
        view = site_view(self.work, self.state, 'extended')
        self.assertEqual(view['repo-sf/en/0000.json'], (self.work / 'repo-sf/en/0000.json').read_bytes())
        self.assertFalse(json.loads(view['site-status/en/0000.json'])['sfAuthoringComplete'])

    def test_changed_question_and_missing_gate_fail_before_export(self):
        write(self.work / 'catalog/questions/en/0000.json', {'bad': 'modified after review'})
        with self.assertRaisesRegex(ValueError, 'changed after validation'):
            site_view(self.work, self.state, 'catalog')
        with self.assertRaisesRegex(ValueError, 'has not passed'):
            site_view(self.work, self.state, 'extended')

    def test_workflow_configuration_is_idempotent_and_reads_its_own_repository(self):
        configured = configure_workflow(WORKFLOW)
        self.assertEqual(configured, configure_workflow(configured))
        self.assertIn('UNIQ2ME_CATALOG_REPOSITORY: ${{ github.repository }}', configured)
        self.assertIn('config.repository = process.env.GITHUB_REPOSITORY', configured)
        self.assertIn('--config catalog-branches.json', configured)

    def test_pinned_signer_aliases_become_valid_data_and_cannot_escape_their_directory(self):
        repo = self.root / 'templates'
        self.git(self.root, 'init', '-q', str(repo))
        write(repo / 'repo-signer/demoA.json', {'synthetic': True})
        (repo / 'repo-signer/demoB.json').symlink_to('demoA.json')
        self.git(repo, 'add', '.')
        self.git(repo, '-c', 'user.name=Synthetic', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'Pinned aliases')
        commit = self.git(repo, 'rev-parse', 'HEAD')
        data = template_skeleton(repo, commit)
        self.assertEqual(data['repo-signer/demoB.json'], data['repo-signer/demoA.json'])
        (repo / 'repo-signer/demoC.json').symlink_to('../../outside')
        self.git(repo, 'add', '.')
        self.git(repo, '-c', 'user.name=Synthetic', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'Invalid alias')
        with self.assertRaisesRegex(ValueError, 'escapes its data directory'):
            template_skeleton(repo, self.git(repo, 'rev-parse', 'HEAD'))

    def make_remote(self):
        bare, repo = self.root / 'remote.git', self.root / 'archive'
        self.git(self.root, 'init', '--bare', '-q', str(bare))
        self.git(self.root, 'init', '-q', '-b', 'main', str(repo))
        write_view(repo, {'.github/workflows/build-deploy.yml': configure_workflow(WORKFLOW).encode()})
        (repo / 'README.md').write_text('Synthetic output owner\n')
        self.git(repo, 'add', '.')
        self.git(repo, '-c', 'user.name=Synthetic', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'Base workflow')
        self.git(repo, 'remote', 'add', 'origin', str(bare))
        self.git(repo, 'push', 'origin', 'main')
        write(self.work / 'catalog/en.json', {'synthetic': True})
        write_view(repo, site_view(self.work, self.state, 'catalog'))
        self.git(repo, 'add', '.')
        self.git(repo, '-c', 'user.name=Synthetic', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'Checked catalog')
        return bare, repo, self.git(repo, 'rev-parse', 'HEAD')

    def test_merge_keeps_archive_parent_and_atomic_translation_branches(self):
        with patch.dict(os.environ, PUSH_TOKEN='synthetic-local-git-test-token'):
            bare, repo, source = self.make_remote()
            journal_root = self.root / 'journals'
            first = publish_tree(repo, journal_root, self.state, 'catalog', source)
            self.assertEqual(self.git(bare, 'rev-parse', 'en/qwen3'), first['commit'])
            self.assertIn(source, self.git(bare, 'show', '-s', '--format=%P', first['commit']).split())
            count = self.git(bare, 'rev-list', '--count', 'en/qwen3')
            self.assertEqual(publish_tree(repo, journal_root, self.state, 'catalog', source), first)
            self.assertEqual(count, self.git(bare, 'rev-list', '--count', 'en/qwen3'))
            # Later completed translations share the checked tree and update atomically.
            status = read(repo / 'site-publication.json')
            status['languages'] = ['en', 'fr', 'el']
            write(repo / 'site-publication.json', status)
            self.git(repo, 'add', 'site-publication.json')
            self.git(repo, '-c', 'user.name=Synthetic', '-c', 'user.email=test@example.invalid',
                     'commit', '-qm', 'Checked translations')
            source2 = self.git(repo, 'rev-parse', 'HEAD')
            second = publish_tree(repo, journal_root, self.state, 'translate-el', source2)
            for ref in ('en/qwen3', 'fr/qwen3', 'el/qwen3'):
                self.assertEqual(self.git(bare, 'rev-parse', ref), second['commit'])

    def test_interrupted_push_recovers_the_same_commit_and_concurrent_changes_are_blocked(self):
        from ci.pipeline import command as real_command
        with patch.dict(os.environ, PUSH_TOKEN='synthetic-local-git-test-token'):
            bare, repo, source = self.make_remote()
            journal_root = self.root / 'journals'
            def interrupted(args, cwd, **kwargs):
                result = real_command(args, cwd, **kwargs)
                if args[:3] == ['git', 'push', '--atomic']:
                    raise RuntimeError('Synthetic interruption after successful push')
                return result
            with patch('ci.pipeline.command', interrupted), self.assertRaisesRegex(RuntimeError, 'Synthetic interruption'):
                publish_tree(repo, journal_root, self.state, 'catalog', source)
            commit = self.state['sitePublications']['catalog']['commit']
            self.assertFalse(self.state['sitePublications']['catalog']['completed'])
            self.assertEqual(self.git(bare, 'rev-parse', 'en/qwen3'), commit)
            recovered = publish_tree(repo, journal_root, self.state, 'catalog', source)
            self.assertEqual(recovered['commit'], commit)
            self.assertTrue(recovered['completed'])
            self.git(bare, 'update-ref', 'refs/heads/en/qwen3', source)
            with self.assertRaisesRegex(ValueError, 'changed since the previous phase'):
                publish_tree(repo, journal_root, self.state, 'extended', source)


if __name__ == '__main__':
    unittest.main()
