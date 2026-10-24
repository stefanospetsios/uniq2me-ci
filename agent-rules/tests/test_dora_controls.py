"""DORA form parity, taxonomy isolation and translations; synthetic/local only."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import yaml
from ci.contracts import HEX, file_manifest, read, write
from ci.dora import (DoraPipeline, load_profile, marker, plan_input, profile_for_catalog,
                     validate_profile_catalog)
from ci.incremental import Incremental
from ci.pipeline import Pipeline
from ci.refinement import parse_plan
from ci.refinement_masks import generate_masks
from ci.smoke_test import create_fixture_pipeline
from ci.taxonomy import ensure_taxonomy

ROOT = Path(__file__).resolve().parents[2]


class DoraControlsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        env = patch.dict(os.environ, {'CATALOG_PROFILE': 'dora', 'TARGET_BRANCH': 'dora',
            'RAG_MODE': 'repo-web', 'SOURCE_CACHE_MODE': 'off', 'REFINEMENT_PLAN': '',
            'UNIQ2ME_INPUT_COMMITS': '', 'UNIQ2ME_AI_INPUT_COMMITS': ''})
        env.start()
        self.addCleanup(env.stop)

    def pipeline(self, settings):
        original, _ = create_fixture_pipeline(self.root / 'run', publish=False)
        p = DoraPipeline()
        p.__dict__.update(original.__dict__)
        p.state.update(branch='dora', referenceInputs={})
        with patch.dict(os.environ, {'REFINEMENT_PLAN': json.dumps(settings)}), \
             patch.object(Pipeline, 'initialize', return_value=None):
            p.initialize()
        return p

    def test_forms_share_models_and_tasks_with_dora_defaults_and_no_legacy_input_injection(self):
        forms = [yaml.safe_load((ROOT / ('examples/' + profile + '.yml')).read_text())
                 for profile in ('air', 'dora')]
        air, dora = [form['on']['workflow_dispatch']['inputs'] for form in forms]
        self.assertEqual(len(dora), 25)
        self.assertEqual(set(air), set(dora))
        for key in air:
            self.assertEqual(air[key]['type'], dora[key]['type'])
            self.assertEqual(air[key].get('options'), dora[key].get('options'))
        self.assertEqual(dora['mask_instructions']['default'], 'create a new dora mask base on the questions')
        self.assertEqual(dora['translate_model']['default'], 'qwen3-coder-next:q4_K_M')
        self.assertEqual(forms[1]['jobs']['catalog']['with']['profile'], 'dora')
        values = {key: field.get('default', '') for key, field in dora.items()}
        self.assertNotIn('rework_questions', plan_input(values))
        self.assertEqual(parse_plan(plan_input(values))['taxonomyMode'], 'preserve')
        legacy = plan_input({'sfs': '0'})
        self.assertTrue(legacy['rework_questions'])
        self.assertFalse(legacy['complete_fields'])

    def test_ay_preserves_dora_and_t_and_m_use_dora_research_and_selected_models(self):
        p = self.pipeline({'question_mode': 'aY', 'sfs': '0', 'retry_models': '',
            'create_masks': True, 'mask_instructions': 'create 2 dora masks',
            'translate': True, 'translate_to': 'All', 'translate_model': 'qwen3-coder-next:q4_K_M'})
        self.assertEqual(p.state['refinementPlan']['maskRequest']['count'], 2)
        original = file_manifest(p.work / 'base')
        area = p.source_area('legal')
        self.assertIn('2022/2554', area['keyDescription'])
        self.assertNotIn('NIS2', area['keyTitle'] + area['keyDescription'])
        self.assertNotIn('Greek Law 5160', area['keyDescription'])
        from ci.source_research import validate_requested_authorities
        validate_requested_authorities('legal', {'dora': {'url': load_profile()['sources'][0]['url']}},
            {'keyDescription': 'French financial entities under DORA'}, framework='dora')
        config = read(p.work / 'agent-rules/run-config.json')
        self.assertEqual(config['masks']['jurisdictions'], ['EU'])
        self.assertIn('legal', p.state['sourceScopes'])
        p.sources()
        p.refine('0')
        p.pinned()
        self.assertEqual(file_manifest(p.work / 'base'), original)
        validate_profile_catalog(p.work / 'base', load_profile())
        for lang in ('fr', 'el', 'de'):
            self.assertTrue((p.work / 'repo-sf' / lang / '0000.json').exists())
            self.assertEqual(p.state['refinementPlan']['tasks']['translate_' + lang]['model'], 't')
        # Other areas lack translations, so masks must remain pending rather
        # than fabricate complete target-language bindings.
        p.state['primaryPassCompleted'] = ['0']
        Incremental(p).data()['completedPasses'] = ['primary']
        generate_masks(p)
        self.assertEqual(p.state['maskGeneration']['languages']['en']['status'], 'checked-draft')
        coverage = read(p.work / 'mask-coverage/en.json')
        self.assertTrue(all('DORA' in row['instrument'] and row['jurisdiction'] == 'EU' for row in coverage))
        for lang in ('fr', 'el', 'de'):
            self.assertEqual(p.state['maskGeneration']['languages'][lang]['status'], 'pending-translation')

    def test_an_derives_bound_dora_areas_and_publishes_reusable_profile_without_changing_inputs(self):
        p = self.pipeline({'question_mode': 'aN', 'catalog_brief': 'DORA for financial entities', 'retry_models': ''})
        original = file_manifest(p.work / 'inputs')
        ensure_taxonomy(p)
        profile = p.source_profile()
        self.assertEqual(profile['frameworkId'], 'dora')
        self.assertTrue(profile['id'].startswith('dora-generated-'))
        self.assertEqual(len(profile['areas']), 16)
        self.assertEqual(p.state['catalogProfile'], marker(profile))
        self.assertEqual(file_manifest(p.work / 'inputs'), original)
        validate_profile_catalog(p.work / 'base', profile)
        p.pinned()
        p.sources()
        p.refine('0')
        validate_profile_catalog(p.work, profile)
        runner = Incremental(p)
        runner.data()['visiblePositions'] = ['0000']
        files = runner.snapshot()
        self.assertIn('catalog/taxonomy/dora-profile.json', files)
        exported = self.root / 'exported'
        for name, contents in files.items():
            target = exported / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        self.assertEqual(profile_for_catalog(exported), profile)
        validate_profile_catalog(exported, profile)
        p.state['expectedRemote'] = '1' * 40
        write(p.output / 'catalog-profile.json', marker(load_profile()))
        with patch.dict(os.environ, {'REFINEMENT_PLAN': json.dumps({'question_mode': 'aY'})}), \
             self.assertRaisesRegex(ValueError, 'taxonomies differ'):
            p.check_destination_taxonomy(profile)
        with patch.dict(os.environ, {'REFINEMENT_PLAN': json.dumps({'question_mode': 'aN'})}):
            p.check_destination_taxonomy(profile)
        changed = copy.deepcopy(profile)
        changed['applicabilityPolicy'] = 'Assign every duty to every entity'
        write(exported / 'catalog/taxonomy/dora-profile.json', changed)
        with self.assertRaisesRegex(ValueError, 'protected regulatory'):
            profile_for_catalog(exported)

    def test_an_rejects_foreign_sources_and_invalid_article_ranges_and_recovers_interrupted_seed(self):
        p = self.pipeline({'question_mode': 'aN', 'catalog_brief': 'DORA taxonomy', 'retry_models': ''})
        original = file_manifest(p.work / 'base')
        def interrupt(path, value):
            if Path(path) == p.work / 'base/json/en/1.json':
                raise OSError('Synthetic interrupted DORA taxonomy')
            return write(path, value)
        with patch('ci.taxonomy.write', side_effect=interrupt), self.assertRaises(OSError):
            ensure_taxonomy(p)
        p.pinned()
        self.assertEqual(file_manifest(p.work / 'base'), original)
        with patch('ci.taxonomy.synthetic_taxonomy', side_effect=AssertionError('Do not reauthor reviewed data')):
            ensure_taxonomy(p)
        from ci.taxonomy import validate_taxonomy
        proposal = read(p.work / 'catalog/taxonomy/areas.json')
        for field, value in [('articles', ['65']), ('articles', ['44-31']), ('sourceIds', ['nis2'])]:
            bad = copy.deepcopy(proposal)
            bad['areas'][0][field] = value
            with self.assertRaises(ValueError):
                validate_taxonomy(bad, framework='dora')


if __name__ == '__main__':
    unittest.main()
