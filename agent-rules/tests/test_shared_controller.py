"""Public controller isolation and real local checkpoint migration, no inference."""
import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from ci.audit_public import findings
from ci.checkpoints import restore, seal
from ci.contracts import read, write, file_manifest
from ci.definitions import endpoint_config, methodology_source
from ci.pipeline import command
from ci.refinement import RefinementPipeline, configured_pipeline
from ci.dora import DoraPipeline
from test_refinement import fixture

ROOT = Path(__file__).resolve().parents[2]
CALLER = 'stefanospetsios/uniq2me-air'
SHARED = 'stefanospetsios/uniq2me-ci'


def document(path):
    return yaml.load((ROOT / path).read_text(), Loader=yaml.BaseLoader)


class SharedWorkflowTests(unittest.TestCase):
    def test_both_thin_forms_preserve_parameters_and_forward_only_named_secrets(self):
        for profile in ('air', 'dora'):
            caller = document('examples/' + profile + '.yml')
            self.assertEqual(len(caller['on']['workflow_dispatch']['inputs']), 25)
            self.assertEqual(list(caller['jobs']), ['catalog'])
            job = caller['jobs']['catalog']
            self.assertEqual(job['uses'], SHARED + '/.github/workflows/catalog.yml@main')
            self.assertEqual(job['with']['profile'], profile)
            self.assertEqual(job['with']['refinement_plan'], '${{ toJSON(inputs) }}')
            self.assertEqual(set(job['secrets']), {'UNIQ2ME_TOKEN_WRITE', 'uniq2me_token', 'SOURCE_CACHE_TOKEN', 'MCP_HEADERS_JSON', 'CHECKPOINT_PASS'})

    def test_failed_job_bootstrap_loads_main_and_runtime_steps_instead_of_caller_sha(self):
        stage = document('.github/workflows/stage.yml')['jobs']['stage']
        steps = stage['steps']
        loader = next(s for s in steps if s.get('with', {}).get('repository') == SHARED)
        self.assertEqual(loader['with']['ref'], 'main')
        self.assertEqual(loader['with']['fetch-depth'], '0')
        self.assertEqual(loader['with']['persist-credentials'], 'false')
        self.assertEqual(steps[-1]['uses'], './controller/.github/actions/stage')
        definitions = next(s for s in steps if s['name'] == 'Read pinned caller methodology and endpoint data')
        self.assertEqual(definitions['with']['ref'], '${{ github.sha }}')
        self.assertEqual(stage['env']['CONTROLLER_UPDATES'], 'main')
        self.assertNotIn('PUSH_TOKEN', stage['env'])
        self.assertNotIn('GITHUB_TOKEN', stage['env'])

    def test_public_jobs_cannot_run_live_on_external_callers_or_pr_events(self):
        stage = document('.github/workflows/stage.yml')
        self.assertEqual(list(stage['on']), ['workflow_call'])
        guard = stage['jobs']['stage']['if']
        self.assertIn("github.repository == '" + CALLER + "'", guard)
        self.assertIn("github.repository == 'stefanospetsios/uniq2me-dora'", guard)
        self.assertIn("github.event_name == 'workflow_dispatch'", guard)
        self.assertNotIn('pull_request', guard)
        public = document('.github/workflows/validate.yml')
        self.assertEqual(public['jobs']['offline']['runs-on'], 'ubuntu-latest')
        self.assertNotIn('secrets.', (ROOT / '.github/workflows/validate.yml').read_text())

    def test_composites_use_supported_contexts_and_valid_shell_scripts(self):
        for path in (ROOT / '.github/actions').glob('*/action.yml'):
            source = path.read_text()
            self.assertNotIn('vars.', source, path.name)
            self.assertNotIn('secrets.', source, path.name)
            action = yaml.load(source, Loader=yaml.BaseLoader)
            for step in action['runs']['steps']:
                if 'run' in step:
                    self.assertEqual(step['shell'], 'bash')
                    check = subprocess.run(['bash', '-n'], input=step['run'], text=True, capture_output=True)
                    self.assertEqual(check.returncode, 0, str(path) + ': ' + check.stderr)

    def test_default_profile_is_air_and_saved_dora_profile_is_recognized(self):
        with patch.dict(os.environ, {'CATALOG_PROFILE': '', 'REFINEMENT_PLAN': '{}'}):
            pipeline = RefinementPipeline()
            pipeline.state = {}
            self.assertIs(type(configured_pipeline(pipeline)), RefinementPipeline)
            pipeline.state = {'catalogProfile': {'frameworkId': 'dora'}}
            self.assertIs(type(configured_pipeline(pipeline)), DoraPipeline)

    def test_private_manuscript_is_absent_and_live_init_requires_caller_data(self):
        self.assertFalse((ROOT / 'methodology/main.tex').exists())
        with patch.dict(os.environ, {'METHODOLOGY_FILE': ''}):
            with self.assertRaisesRegex(ValueError, 'Live initialization requires'):
                methodology_source(ROOT, False)
            self.assertIn('SYNTHETIC', methodology_source(ROOT, True).read_text())

    def test_air_cannot_use_a_dora_destination_or_base(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), patch.dict(os.environ, {
                'REFINEMENT_PLAN': '', 'RAG_MODE': 'repo-web', 'SOURCE_CACHE_MODE': 'off', 'CATALOG_PROFILE': ''}):
            pipeline, _ = fixture(Path(directory), {'sfs': 'A'}, publish=False)
            write(pipeline.output / 'catalog-profile.json', {'frameworkId': 'dora'})
            with self.assertRaisesRegex(ValueError, 'AIR destination belongs to another framework'):
                pipeline.prepare_profile_inputs()
            write(pipeline.work / 'inputs/catalog-profile.json', {'frameworkId': 'dora'})
            pipeline.state['referenceInputs'] = {}
            with self.assertRaisesRegex(ValueError, 'AIR base belongs to another framework'):
                pipeline.select_profile_base({})

    def test_only_endpoint_urls_are_imported_from_caller_configuration(self):
        original = read(ROOT / 'opencode.json')
        with tempfile.TemporaryDirectory() as directory:
            private = copy.deepcopy(original)
            for name in private['mcp']:
                private['mcp'][name]['url'] = 'https://' + name + '.example.invalid/mcp'
            private['permission'] = {'*': 'allow'}
            private['instructions'] = ['DO NOT IMPORT THIS CALLER INSTRUCTION']
            private['provider'] = {'malicious-provider': {}}
            write(Path(directory) / 'opencode.json', private)
            with patch.dict(os.environ, {'CONTROLLER_DEFINITIONS': directory}):
                result = endpoint_config(original)
            self.assertEqual(result['permission'], original['permission'])
            self.assertEqual(result['instructions'], original['instructions'])
            self.assertEqual(result['provider'], original['provider'])
            self.assertEqual(result['mcp'], private['mcp'])
            self.assertNotEqual(result['mcp'], original['mcp'])

    def test_endpoint_credentials_are_rejected_without_printing_the_url(self):
        config = read(ROOT / 'opencode.json')
        with tempfile.TemporaryDirectory() as directory:
            private = copy.deepcopy(config)
            private['mcp']['github']['url'] = 'https://user:synthetic-secret@example.invalid/mcp'
            write(Path(directory) / 'opencode.json', private)
            with patch.dict(os.environ, {'CONTROLLER_DEFINITIONS': directory}):
                with self.assertRaises(ValueError) as failure:
                    endpoint_config(config)
            self.assertNotIn('synthetic-secret', str(failure.exception))

    def test_public_audit_detects_credentials_and_addresses_without_echoing_values(self):
        self.assertIn('GitHub credential', findings(('gh' + 'p_' + 'a' * 40).encode()))
        self.assertIn('private infrastructure address', findings(('/Users/' + 'stefanos/' + 'private').encode()))
        self.assertEqual(findings(b'http://127.0.0.1:8101/mcp'), [])


class SharedCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        env = patch.dict(os.environ, {'REFINEMENT_PLAN': '', 'RAG_MODE': 'repo-web',
            'TARGET_BRANCH': '', 'SOURCE_CACHE_MODE': 'off', 'CATALOG_PROFILE': '',
            'CONTROLLER_DEFINITIONS': '', 'CONTROLLER_REPOSITORY': SHARED, 'CONTROLLER_UPDATES': 'main',
            'GITHUB_REPOSITORY': CALLER, 'GITHUB_RUN_ID': '111', 'GITHUB_RUN_ATTEMPT': '1',
            'RESUME_RUN_ID': '', 'FRESH_START': 'false', 'DRY_RUN': 'true', 'PUBLISH': 'false'})
        env.start()
        self.addCleanup(env.stop)
        self.pipeline, self.remote = fixture(self.root / 'original', {'sfs': 'A'}, publish=False)
        self.trusted = self.root / 'shared-controller'
        self.trusted.mkdir()
        command(['git', 'init', '-q', '-b', 'main'], self.trusted)
        command(['git', 'remote', 'add', 'origin', 'https://github.com/' + SHARED + '.git'], self.trusted)
        write(self.trusted / 'controller-compatibility.json', {'legacyControllers': {CALLER: ['b' * 40]}})
        self.revision(1)
        self.pipeline.trusted = self.trusted
        self.pipeline.state.update(controllerCommit=self.head(), controllerRepository=SHARED, workflowRepository=CALLER)
        write(self.pipeline.work / '.task-cache/offline-probe.json', {'recordType': 'synthetic-checkpoint-probe'})
        self.pipeline.save()
        self.frozen = file_manifest(self.pipeline.work)
        seal(self.pipeline)

    def head(self):
        return command(['git', 'rev-parse', 'HEAD'], self.trusted).stdout.strip()

    def revision(self, version):
        write(self.trusted / '.github/actions/stage/action.json', {'syntheticVersion': version})
        command(['git', 'add', '.'], self.trusted)
        command(['git', '-c', 'user.name=Offline fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'Synthetic controller revision ' + str(version)], self.trusted)

    def fresh_runner(self):
        root = self.root / 'new-runner'
        checkpoint = root / 'checkpoint'
        shutil.copytree(self.pipeline.root, checkpoint)
        output = root / 'uniq2me-ai'
        command(['git', 'clone', '-q', '--branch', 'main', str(self.remote), str(output)], root)
        pipeline = RefinementPipeline()
        pipeline.trusted, pipeline.workspace, pipeline.root = self.trusted, root, checkpoint
        pipeline.work, pipeline.output = checkpoint / 'work', output
        pipeline.state_path = checkpoint / 'controller-state.json'
        pipeline.state = read(pipeline.state_path)
        restore(pipeline)
        return pipeline

    def test_same_run_retry_adopts_current_main_and_keeps_all_frozen_work(self):
        previous = self.head()
        self.revision(2)
        restored = self.fresh_runner()
        self.assertEqual(restored.state['controllerCommit'], self.head())
        self.assertEqual(file_manifest(restored.work), self.frozen)
        lineage = restored.state['resumeLineage'][-1]
        self.assertEqual(lineage['sourceControllerCommit'], previous)
        self.assertEqual(lineage['policy'], 'current-main-controller')
        self.assertTrue(lineage['retainedInputAndRulePins'])
        seal(restored)
        self.assertEqual(read(restored.root / 'checkpoint-manifest.json')['controllerRepository'], SHARED)

    def test_unknown_controller_origin_is_rejected(self):
        self.revision(2)
        command(['git', 'remote', 'set-url', 'origin', 'https://example.invalid/untrusted.git'], self.trusted)
        with self.assertRaisesRegex(ValueError, 'origin is not trusted'):
            self.fresh_runner()

    def test_unrelated_controller_history_is_rejected(self):
        command(['git', 'checkout', '--orphan', 'unrelated'], self.trusted)
        self.revision(2)
        with self.assertRaisesRegex(ValueError, 'must descend'):
            self.fresh_runner()

    def test_wrong_profile_is_rejected_even_without_a_controller_change(self):
        with patch.dict(os.environ, {'CATALOG_PROFILE': 'dora'}):
            with self.assertRaisesRegex(ValueError, 'between frameworks'):
                self.fresh_runner()

    def test_reviewed_legacy_controller_migrates_only_with_explicit_source_run(self):
        self.pipeline.state['controllerCommit'] = 'b' * 40
        self.pipeline.state.pop('controllerRepository')
        self.pipeline.save()
        seal(self.pipeline)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '222', 'RESUME_RUN_ID': '111'}):
            restored = self.fresh_runner()
        self.assertEqual(restored.state['controllerRepository'], SHARED)
        self.assertEqual(restored.state['resumeLineage'][-1]['policy'], 'reviewed-legacy-controller')
        self.assertEqual(file_manifest(restored.work), self.frozen)

    def test_unreviewed_legacy_commit_is_rejected(self):
        self.pipeline.state['controllerCommit'] = 'c' * 40
        self.pipeline.state.pop('controllerRepository')
        self.pipeline.save()
        seal(self.pipeline)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '222', 'RESUME_RUN_ID': '111'}):
            with self.assertRaisesRegex(ValueError, 'reviewed migration registry'):
                self.fresh_runner()

    def test_tampered_checkpoint_is_rejected_before_controller_upgrade(self):
        (self.pipeline.work / '.task-cache/offline-probe.json').write_text('tampered')
        self.revision(2)
        with self.assertRaisesRegex(ValueError, 'file hashes do not match'):
            self.fresh_runner()


if __name__ == '__main__':
    unittest.main()
