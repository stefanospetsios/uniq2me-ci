"""Catalog task passes and requested mask publication; synthetic inference only."""
import copy
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import yaml
from ci.contracts import HEX, catalog_digest, read, write
from ci.incremental import Incremental
from ci.refinement import parse_plan
from ci.refinement_masks import generate_masks, validate_translation
from ci.source_research import validate_requested_authorities
from test_refinement import ROOT, fixture


class CompactFormTests(unittest.TestCase):
    def test_requested_french_scope_cannot_drop_its_national_authority(self):
        area = {'keyDescription': 'Current French national transposition status'}
        sources = {'eu': {'url': 'https://eur-lex.europa.eu/eli/dir/2022/2555/oj'}}
        with self.assertRaisesRegex(ValueError, 'official French authority'):
            validate_requested_authorities('legal', sources, area)
        sources['fr'] = {'url': 'https://messervices.cyber.gouv.fr/nis2'}
        validate_requested_authorities('legal', sources, area)
        sources['fr']['url'] = 'https://cyber.gouv.fr.example.invalid/nis2'
        with self.assertRaises(ValueError):
            validate_requested_authorities('legal', sources, area)

    def test_new_air_form_fits_github_limit_and_explicitly_locks_sfs_areas(self):
        form = yaml.load((ROOT / 'examples/air.yml').read_text(), Loader=yaml.BaseLoader)['on']['workflow_dispatch']['inputs']
        self.assertEqual(len(form), 25)
        self.assertEqual(form['question_mode']['options'], ['aY', 'aN', 'skip'])
        self.assertEqual(form['question_model']['options'], ['a', 'b', 'c'])
        self.assertIn('keep 16 areas', form['question_mode']['description'])
        for key in ('model_a', 'model_b', 'model_c', 'translate_model'):
            self.assertEqual(form[key]['type'], 'choice')
            self.assertEqual(len(form[key]['options']), 10)
            self.assertEqual(form[key]['options'], form['model_a']['options'])
        self.assertEqual(form['translate_model']['default'], 'qwen3-coder-next:q4_K_M')
        self.assertEqual(form['translate_to']['options'], ['All', 'French', 'German', 'Greek'])
        self.assertEqual(form['mask_instructions']['default'], 'create a new nis2 mask base on the questions')
        self.assertEqual(form['mask_instructions']['type'], 'string')
        self.assertEqual(form['catalog_brief']['type'], 'string')
        plan = parse_plan({'question_mode': 'rewrite', 'question_model': 'a',
            'catalog_brief': 'NIS2 with official EU, Greek and French sources',
            'mask_instructions': 'try to find 5 good examples as Masks for NIS2. spend if you want 30minutes'})
        self.assertTrue(plan['tasks']['rework_questions']['enabled'])
        self.assertFalse(plan['tasks']['complete_fields']['enabled'])
        self.assertEqual(plan['taskSchedule'], 'catalog-task-passes')
        self.assertEqual(plan['maskRequest']['count'], 5)

    def test_t_uses_its_selected_model_for_all_requested_languages_and_m_is_explicit(self):
        plan = parse_plan({'question_mode': 'aY', 'translate': True, 'translate_to': 'All',
            'translate_model': 'qwen3-coder-next:q4_K_M', 'create_masks': False,
            'mask_instructions': 'create a new nis2 mask base on the questions'})
        self.assertNotIn('maskRequest', plan)
        self.assertEqual(plan['taxonomyMode'], 'preserve')
        self.assertEqual(plan['models']['t'], 'qwen3-coder-next:q4_K_M')
        for language in ('fr', 'el', 'de'):
            self.assertEqual(plan['tasks']['translate_' + language], {'enabled': True, 'model': 't'})
        one = parse_plan({'create_masks': True, 'mask_instructions': 'create a new nis2 mask base on the questions'})
        self.assertEqual(one['maskRequest']['count'], 1)
        for settings in ({'question_mode': 'aN'}, {'question_mode': 'aN', 'sfs': 'A', 'catalog_brief': 'New domain'}):
            with self.assertRaises(ValueError):
                parse_plan(settings)

    def test_legacy_plan_and_empty_mask_request_preserve_original_resume_binding(self):
        old = parse_plan({'sfs': 'A'})
        self.assertNotIn('create_masks', old['tasks'])
        self.assertNotIn('taskSchedule', old)
        self.assertNotIn('catalogBrief', old)
        self.assertEqual(old, parse_plan({'sfs': 'A', 'mask_instructions': '', 'catalog_brief': ''}))
        for settings in ({'question_mode': 'rewrite', 'complete_fields': False},
                         {'question_mode': 'unexpected'}, {'mask_instructions': []}, {'catalog_brief': []}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                parse_plan(settings)


class RefinementMaskTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        env = patch.dict(os.environ, {'REFINEMENT_PLAN': '', 'RAG_MODE': 'repo-web',
            'SOURCE_CACHE_MODE': 'off', 'CATALOG_PROFILE': '', 'TARGET_BRANCH': ''})
        env.start()
        self.addCleanup(env.stop)

    def pipeline(self, **settings):
        p, _ = fixture(self.root / 'run', {'sfs': 'A', 'retry_models': '', **settings}, publish=False)
        return p

    def mark_sf_passes_finished(self, p):
        p.state['primaryPassCompleted'] = ['A']
        runner = Incremental(p)
        runner.data()['completedPasses'] = ['primary']
        p.save()
        return runner

    def test_whole_catalog_task_pass_replays_checked_inputs_without_loading_earlier_models(self):
        p = self.pipeline(question_mode='rewrite', ai_prompts=True, ai_prompts_model='b',
                          canonical_solutions=True, canonical_solutions_model='c')
        areas = {sfs: read(p.work / 'json/en' / (sfs + '.json')) for sfs in HEX}
        runner = Incremental(p)
        activated = []
        original = p.activate
        def activate(task):
            activated.append((task, p.state['refinementPlan']['tasks'][task]['model']))
            original(task)
        p.activate = activate
        for task in ('rework_questions', 'ai_prompts', 'canonical_solutions'):
            p.state['activeTaskPass'] = task
            self.assertTrue(runner.begin('A'))
            if task == 'rework_questions':
                with self.assertRaisesRegex(ValueError, 'Finish the primary pass'):
                    runner.begin('A', 'b')
            for sf in HEX:
                runner.factor('A', sf)
                runner.prepare('sf', 'A', sf)
                runner.publish()
            runner.finish_sfs('A')
            if task == 'rework_questions':
                self.assertEqual(p.state['sfsGates']['A']['tasks'], ['rework_questions'])
            runner.prepare('sfs', 'A')
            runner.publish()
            runner.finish_pass('primary')
            if task != 'canonical_solutions':
                self.assertNotIn('primary', runner.data()['completedPasses'])
        self.assertEqual(runner.data()['completedTaskPasses'], ['rework_questions', 'ai_prompts', 'canonical_solutions'])
        self.assertEqual(runner.data()['completedPasses'], ['primary'])
        self.assertEqual([label for _, label in activated], sorted([label for _, label in activated]))
        for sfs in HEX:
            current = read(p.work / 'json/en' / (sfs + '.json'))
            for key in ('keyTitle', 'keyDescription', 'keyShortInfo', 'keyWords', 'promptAi', 'meta'):
                self.assertEqual(current.get(key), areas[sfs].get(key))
        self.assertTrue((p.work / 'solutions/en/00A0.json').exists())
        p.state.pop('activeTaskPass')
        runner.prepare('catalog')
        runner.publish()
        self.assertIn('catalogCommit', runner.data())

    def test_five_masks_are_checked_exported_and_bound_after_final_annotations(self):
        p = self.pipeline(mask_instructions='Create five NIS2 examples')
        runner = self.mark_sf_passes_finished(p)
        generate_masks(p)
        state = p.state['maskGeneration']['languages']['en']
        self.assertEqual(state['status'], 'checked-draft')
        self.assertEqual(state['generatedMaskCount'], 5)
        self.assertEqual(state['catalogDigest'], catalog_digest(p.work, 'en'))
        files = runner.snapshot()
        self.assertEqual(len([name for name in files if name.startswith('repo-sfs-masks/en/')]), 5)
        self.assertIn('mask-coverage/en.json', files)
        self.assertIn('mask-status/en.json', files)
        source = next((p.work / 'repo-sfs-masks/en').glob('*.json'))
        write(source, {**read(source), 'title': 'Independent tampering'})
        with self.assertRaisesRegex(ValueError, 'Checked mask bytes changed'):
            runner.snapshot()

    def test_masks_wait_for_all_sf_passes_and_translation_preserves_selections(self):
        p = self.pipeline(mask_instructions='Create five NIS2 examples', translate_fr=True)
        with self.assertRaisesRegex(ValueError, 'Finish all SF task/model passes'):
            generate_masks(p)
        self.mark_sf_passes_finished(p)
        for source in (p.work / 'json/en').glob('*.json'):
            write(p.work / 'json/fr' / source.name, read(source))
        for source in (p.work / 'repo-sf/en').glob('*.json'):
            record = read(source)
            record['identity']['language'] = 'fr'
            write(p.work / 'repo-sf/fr' / source.name, record)
        generate_masks(p)
        states = p.state['maskGeneration']['languages']
        self.assertEqual(states['fr']['status'], 'checked-draft')
        self.assertNotEqual(states['fr']['catalogDigest'], states['en']['catalogDigest'])
        original = {'masks': [read(file) for file in sorted((p.work / 'repo-sfs-masks/en').glob('*.json'))],
                    'coverage': read(p.work / 'mask-coverage/en.json')}
        translated = {'masks': [read(file) for file in sorted((p.work / 'repo-sfs-masks/fr').glob('*.json'))],
                      'coverage': read(p.work / 'mask-coverage/fr.json')}
        validate_translation(translated, original)
        translated['masks'][0]['selection']['positions'][0]['sfId'] = 'F'
        with self.assertRaisesRegex(ValueError, 'bits changed'):
            validate_translation(translated, original)

    def test_incomplete_translations_do_not_receive_fabricated_mask_bindings(self):
        p = self.pipeline(mask_instructions='Create five NIS2 examples', translate_fr=True)
        self.mark_sf_passes_finished(p)
        generate_masks(p)
        self.assertEqual(p.state['maskGeneration']['languages']['en']['generatedMaskCount'], 5)
        self.assertEqual(p.state['maskGeneration']['languages']['fr']['status'], 'pending-translation')
        self.assertFalse((p.work / 'repo-sfs-masks/fr').exists())

    def test_selected_translation_model_is_checked_before_any_model_is_warmed(self):
        p = self.pipeline(translate=True, translate_to='German', translate_model='qwen3-coder-next:q4_K_M')
        p.state['dryRun'] = False
        available = {'models': [{'name': tag} for slot, tag in p.state['refinementPlan']['models'].items() if slot != 't']}
        with patch('ci.refinement.http_json', return_value=available), patch('ci.pipeline.Pipeline.preflight') as preflight:
            with self.assertRaisesRegex(ValueError, 'qwen3-coder-next:q4_K_M'):
                p.preflight()
            preflight.assert_not_called()

    def test_mask_translations_use_t_model_for_french_german_and_greek(self):
        p = self.pipeline(create_masks=True, mask_instructions='Create five draft masks',
                          translate=True, translate_to='All', translate_model='qwen3-coder-next:q4_K_M')
        self.mark_sf_passes_finished(p)
        for language in ('fr', 'el', 'de'):
            for source in (p.work / 'json/en').glob('*.json'):
                write(p.work / 'json' / language / source.name, read(source))
            for source in (p.work / 'repo-sf/en').glob('*.json'):
                record = read(source)
                record['identity']['language'] = language
                write(p.work / 'repo-sf' / language / source.name, record)
        activated = []
        activate = p.activate
        def observed(task):
            activated.append((task, p.state['refinementPlan']['tasks'][task]['model']))
            activate(task)
        p.activate = observed
        generate_masks(p)
        self.assertEqual(activated, [('create_masks', 'a'), ('translate_fr', 't'), ('translate_el', 't'), ('translate_de', 't')])
        self.assertEqual({lang: value['generatedMaskCount'] for lang, value in p.state['maskGeneration']['languages'].items()},
                         {'en': 5, 'fr': 5, 'el': 5, 'de': 5})


if __name__ == '__main__':
    unittest.main()
