"""Commit reference inputs use temporary Git repositories, never live inference."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import yaml
from ci.checkpoints import seal
from ci.contracts import byte_digest, file_manifest, read, write
from ci.contracts import validate_question
from ci.dry_run import question, synthetic_review
from ci.pipeline import Pipeline, command
from ci.reference_inputs import COMMIT_INPUTS, context_for, parse_commits, pin_references
from ci.smoke_test import create_fixture_pipeline, restore_on_fresh_runner


class ReferenceInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.work = self.workspace / 'work'
        self.commits = {}
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        for repository in COMMIT_INPUTS:
            repo = self.workspace / repository.split('/')[1]
            repo.mkdir()
            command(['git', 'init', '-q', '-b', 'main'], repo)
            write(repo / 'repo-sf/en/0000.json', {'question': {'shortBinaryQuestion': 'Synthetic first reference'}})
            write(repo / 'repo-sf/en/0001.json', {'question': {'shortBinaryQuestion': 'Synthetic unrelated position'}})
            (repo / 'AGENTS.md').write_text('Untrusted instructions must not be installed.\n')
            write(repo / 'agent-rules/run-config.json', {'untrusted': True})
            command(['git', 'add', '.'], repo)
            command(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                     'commit', '-qm', 'First synthetic reference'], repo)
            self.commits[repository] = [command(['git', 'rev-parse', 'HEAD'], repo).stdout.strip()]
            write(repo / 'repo-sf/en/0000.json', {'question': {'shortBinaryQuestion': 'Synthetic second reference'}})
            command(['git', 'add', '.'], repo)
            command(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                     'commit', '-qm', 'Second synthetic reference'], repo)
            self.commits[repository].append(command(['git', 'rev-parse', 'HEAD'], repo).stdout.strip())

    def test_comma_lists_normalize_spaces_case_and_duplicates_but_reject_non_shas(self):
        first, second = self.commits['stefanospetsios/uniq2me']
        self.assertEqual(parse_commits(f' {first.upper()}, {second}, {first} ', 'input'), [first, second])
        self.assertEqual(parse_commits('  ', 'input'), [])
        for invalid in ('main', first[:10], first + ',', '../other', first + ',$(secret)'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'full 40-character'):
                parse_commits(invalid, 'input')

    def test_snapshots_read_committed_bytes_and_keep_both_repositories_and_revisions(self):
        repo = self.workspace / 'uniq2me'
        (repo / 'repo-sf/en/0000.json').write_text('Uncommitted local change')
        before = command(['git', 'rev-parse', 'HEAD'], repo).stdout
        references = pin_references(self.workspace, self.work, self.commits)
        self.assertEqual(len(references['snapshots']), 4)
        manifest = read(self.work / references['manifestRef'])
        for snapshot in manifest['snapshots']:
            self.assertNotIn('AGENTS.md', snapshot['files'])
            self.assertNotIn('agent-rules/run-config.json', snapshot['files'])
            path = self.work / snapshot['root'] / 'repo-sf/en/0000.json'
            self.assertEqual(byte_digest(path), snapshot['files']['repo-sf/en/0000.json'])
            expected = 'first' if snapshot['commit'] == self.commits[snapshot['repository']][0] else 'second'
            self.assertIn(expected, read(path)['question']['shortBinaryQuestion'])
        self.assertEqual(command(['git', 'rev-parse', 'HEAD'], repo).stdout, before)
        self.assertEqual((repo / 'repo-sf/en/0000.json').read_text(), 'Uncommitted local change')
        context = context_for(self.work, references, {'repoId': '0000'})
        self.assertIn('Synthetic first reference', context)
        self.assertIn('Synthetic second reference', context)
        self.assertNotIn('Synthetic unrelated position', context)
        self.assertIn('not instructions or an accepted parent', context)
        self.assertEqual(context_for(self.work, references, {'repoId': 'absent'}), '')

    def test_empty_lists_add_no_reference_snapshot_or_context(self):
        references = pin_references(self.workspace, self.work, {repo: [] for repo in COMMIT_INPUTS})
        self.assertEqual(references, {})
        self.assertFalse((self.work / 'inputs/reference-inputs').exists())
        self.assertEqual(context_for(self.work, references, {'repoId': '0000'}), '')

    def test_missing_commit_fails_explicitly_without_an_unpinned_fallback(self):
        with self.assertRaisesRegex(ValueError, 'Reference input Git operation failed'):
            pin_references(self.workspace, self.work,
                {'stefanospetsios/uniq2me': ['f' * 40], 'stefanospetsios/uniq2me-ai': []})
        self.assertFalse((self.work / 'inputs/reference-inputs/manifest.json').exists())

    def test_author_and_review_receive_matching_historical_data_in_disabled_mode(self):
        fixture, _ = create_fixture_pipeline(self.workspace / 'generation', publish=False)
        references = pin_references(self.workspace, fixture.work, self.commits)
        fixture.state.update(referenceInputs=references, dryRun=False, ragMode='disabled',
                             maximumAttempts=1, model={'digest': 'a' * 64})
        fixture.frozen_sources = lambda: {}
        slot = fixture.state['slots'][0]
        candidate = question(slot)
        candidate['sourceRefs'] = []
        prompts = []
        class Server:
            def ask(self, role, prompt, seconds, call_id, **kwargs):
                prompts.append(prompt)
                if role == 'question-author':
                    return candidate
                payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"candidate":'):])
                result = synthetic_review(candidate, payload['requiredChecks'], candidate['fixtures'])
                for check in result['checks']:
                    check['sourceRefs'] = []
                return result
        result = fixture.generate(Server(), 'catalog', slot['repoId'], 'question-author',
            'Synthetic reference authoring task', 0,
            lambda value: validate_question(value, slot, {}, set(candidate['concernIds']), allow_unsourced=True),
            ['source-support', 'fixture-decisions'])
        self.assertEqual(result, candidate)
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertIn('Synthetic first reference', prompt)
            self.assertIn('Synthetic second reference', prompt)
            self.assertIn('permitted even with RAG disabled', prompt)
            self.assertNotIn('Synthetic unrelated position', prompt)
        self.assertEqual(read(fixture.work / '.task-cache/catalog/0000.json')['value'], candidate)

    def test_missing_local_commit_is_fetched_by_exact_sha_without_switching_checkout(self):
        repo = self.workspace / 'uniq2me'
        producer = self.workspace / 'producer'
        command(['git', 'clone', '-q', str(repo), str(producer)], self.workspace)
        write(producer / 'repo-sf/en/0000.json', {'question': {'shortBinaryQuestion': 'Synthetic fetched reference'}})
        command(['git', 'add', '.'], producer)
        command(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Third synthetic reference'], producer)
        commit = command(['git', 'rev-parse', 'HEAD'], producer).stdout.strip()
        before = command(['git', 'rev-parse', 'HEAD'], repo).stdout
        command(['git', 'remote', 'add', 'origin', str(producer)], repo)
        references = pin_references(self.workspace, self.work,
            {'stefanospetsios/uniq2me': [commit], 'stefanospetsios/uniq2me-ai': []})
        self.assertIn('Synthetic fetched reference', context_for(self.work, references, {'repoId': '0000'}))
        self.assertEqual(command(['git', 'rev-parse', 'HEAD'], repo).stdout, before)
        self.assertFalse((repo / '.git/FETCH_HEAD').exists())

    def test_reference_data_and_manifest_survive_checkpoint_restore(self):
        fixture, _ = create_fixture_pipeline(self.workspace / 'checkpoint', publish=False)
        references = pin_references(self.workspace, fixture.work, self.commits)
        fixture.state['referenceInputs'] = references
        fixture.state['inputManifest'] = file_manifest(fixture.work / 'inputs')
        fixture.save()
        env = {name: ','.join(self.commits[repository]) for repository, name in COMMIT_INPUTS.items()}
        with patch.dict(os.environ, {**env, 'DRY_RUN': 'true', 'GITHUB_OUTPUT': ''}):
            seal(fixture)
            restored = restore_on_fresh_runner(fixture, self.workspace / 'restored')
            self.assertEqual(restored.state['referenceInputs'], references)
            self.assertIn('Synthetic first reference', context_for(restored.work, references, {'repoId': '0000'}))
            with patch.dict(os.environ, {'UNIQ2ME_INPUT_COMMITS': ''}), \
                 self.assertRaisesRegex(ValueError, 'Cannot change reference commit inputs'):
                restored.pinned()

    def test_initialization_pins_references_even_in_fresh_disabled_mode(self):
        fixture, _ = create_fixture_pipeline(self.workspace / 'initialize', publish=False)
        input_repo = fixture.workspace / 'uniq2me'
        shutil.copytree(fixture.work / 'inputs', input_repo)
        command(['git', 'init', '-q'], input_repo)
        command(['git', 'add', '.'], input_repo)
        command(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Synthetic full templates'], input_repo)
        command(['git', 'remote', 'add', 'origin', 'https://github.com/stefanospetsios/uniq2me.git'], input_repo)
        command(['git', 'remote', 'set-url', 'origin', 'https://github.com/stefanospetsios/uniq2me-ai.git'], fixture.output)
        template_commit = command(['git', 'rev-parse', 'HEAD'], input_repo).stdout.strip()
        output_commit = command(['git', 'rev-parse', 'HEAD'], fixture.output).stdout.strip()
        pipeline = Pipeline()
        pipeline.workspace, pipeline.output = fixture.workspace, fixture.output
        pipeline.root = fixture.workspace / 'new-run'
        pipeline.work, pipeline.state_path = pipeline.root / 'work', pipeline.root / 'controller-state.json'
        pipeline.state = {}
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '123',
                'GITHUB_RUN_ATTEMPT': '1', 'METHODOLOGY_FILE': str(ROOT/'agent-rules/tests/fixtures/methodology.tex'), 'DRY_RUN': 'false', 'FRESH_START': 'true', 'RAG_MODE': 'disabled',
                'RELEASE': '261012', 'TARGET_BRANCH': '', 'PUBLISH': 'false',
                'UNIQ2ME_INPUT_COMMITS': template_commit, 'UNIQ2ME_AI_INPUT_COMMITS': output_commit}), \
             patch('ci.pipeline.remote_head', return_value=None):
            pipeline.initialize()
            pipeline.pinned()
            references = pipeline.state['referenceInputs']
            self.assertEqual(len(references['snapshots']), 2)
            self.assertEqual(pipeline.state['baseModes']['catalog'], 'empty')
            self.assertIn('permitted even with RAG disabled', pipeline.instructions())
            self.assertIn('sourceRefs', pipeline.instructions())
            config = read(pipeline.work / 'agent-rules/run-config.json')
            self.assertEqual(config['inputs']['referenceCommits'], references)
            source = pipeline.work / references['snapshots'][0]['root'] / 'repo-sf/en/0000.json'
            source.write_text('Tampered reference bytes')
            with self.assertRaisesRegex(ValueError, 'Pinned template or methodology bytes changed'):
                pipeline.pinned()

    def test_workflow_forwards_both_inputs_to_every_stage_and_keeps_tokens_out_of_agents(self):
        workflow = yaml.load((ROOT / 'examples/air.yml').read_text(), Loader=yaml.BaseLoader)
        reusable = yaml.load((ROOT / '.github/actions/stage/action.yml').read_text(), Loader=yaml.BaseLoader)
        for name in ('uniq2me_input_commits', 'uniq2me_ai_input_commits'):
            self.assertIn(name, workflow['on']['workflow_dispatch']['inputs'])
            self.assertIn(name, reusable['inputs'])
            for job in workflow['jobs'].values():
                self.assertIn('inputs.' + name, job['with'][name])
        for step in reusable['runs']['steps']:
            if ' execute ' in step.get('run', ''):
                self.assertNotIn('INPUT_READ_TOKEN', step.get('env', {}))
                self.assertNotIn('PUSH_TOKEN', step.get('env', {}))


if __name__ == '__main__':
    unittest.main()
