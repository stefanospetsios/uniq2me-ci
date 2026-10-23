"""Explicit taxonomy replacement preserves source provenance and the 16x16 schema."""
import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ci.contracts import HEX, digest, file_manifest, read, write
from ci.refinement import parse_plan, check_catalog
from ci.taxonomy import ensure_taxonomy, synthetic_taxonomy, validate_taxonomy
from test_refinement import fixture


class TaxonomyTests(unittest.TestCase):
    def test_taxonomy_requires_sixteen_distinct_ordered_areas_and_boundaries(self):
        proposal = synthetic_taxonomy()
        validate_taxonomy(proposal)
        invalid = copy.deepcopy(proposal)
        invalid['areas'][1]['title'] = invalid['areas'][0]['title']
        with self.assertRaisesRegex(ValueError, 'duplicate areas'):
            validate_taxonomy(invalid)
        for transform in (lambda value: value['areas'].pop(),
                          lambda value: value['areas'][0].update(boundaries=''),
                          lambda value: value.update(unresolved=['Unresolved domain gap'])):
            invalid = copy.deepcopy(proposal)
            transform(invalid)
            with self.assertRaises(ValueError):
                validate_taxonomy(invalid)

    def test_an_derives_a_new_draft_base_and_keeps_original_input_bytes(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), patch.dict(os.environ, {
                'REFINEMENT_PLAN': '', 'RAG_MODE': 'repo-web', 'SOURCE_CACHE_MODE': 'off',
                'TARGET_BRANCH': '', 'CATALOG_PROFILE': ''}):
            p, _ = fixture(Path(directory), {'question_mode': 'aY', 'sfs': 'all'})
            original = file_manifest(p.work / 'inputs')
            old_area = read(p.work / 'base/json/en/0.json')
            p.state['refinementPlan'] = parse_plan({'question_mode': 'aN', 'catalog_brief': 'SYNTHETIC new interest', 'sfs': 'all'})
            p.state.pop('frozenManifest')
            ensure_taxonomy(p)
            self.assertEqual(file_manifest(p.work / 'inputs'), original)
            self.assertNotEqual(read(p.work / 'base/json/en/0.json')['keyTitle'], old_area['keyTitle'])
            self.assertEqual(p.state['catalogProfile']['frameworkId'], 'air')
            self.assertFalse(p.state['catalogProfile']['taxonomyIsOfficial'])
            self.assertEqual(p.state['taxonomyGeneration']['proposalDigest'], digest(read(p.work / 'catalog/taxonomy/areas.json')))
            check_catalog(p.work / 'base', p.state['slots'])
            self.assertTrue(all(read(p.work / 'base/repo-sf/en' / (slot['repoId'] + '.json'))['isPlaceholder']
                                for slot in p.state['slots']))
            p.pinned()
            before = file_manifest(p.work / 'base')
            ensure_taxonomy(p)
            self.assertEqual(file_manifest(p.work / 'base'), before)
            p.sources()
            p.refine('A')
            self.assertTrue((p.work / 'catalog/questions/en/00A0.json').exists())
            self.assertEqual(read(p.work / 'json/en/A.json')['keyTitle'], 'SYNTHETIC proposed area A')

    def test_an_cannot_replace_a_dora_profile_or_change_after_sources_are_frozen(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), patch.dict(os.environ, {
                'REFINEMENT_PLAN': '', 'RAG_MODE': 'repo-web', 'SOURCE_CACHE_MODE': 'off',
                'TARGET_BRANCH': '', 'CATALOG_PROFILE': ''}):
            p, _ = fixture(Path(directory), {'question_mode': 'aY'})
            p.state['refinementPlan'] = parse_plan({'question_mode': 'aN', 'catalog_brief': 'SYNTHETIC new interest'})
            with self.assertRaisesRegex(ValueError, 'before source admission'):
                ensure_taxonomy(p)
            p.state.pop('frozenManifest')
            p.state['catalogProfile'] = {'frameworkId': 'dora'}
            with self.assertRaisesRegex(ValueError, 'DORA taxonomy'):
                ensure_taxonomy(p)

    def test_interrupted_seed_restores_verified_base_and_reuses_reviewed_taxonomy(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), patch.dict(os.environ, {
                'REFINEMENT_PLAN': '', 'RAG_MODE': 'repo-web', 'SOURCE_CACHE_MODE': 'off',
                'TARGET_BRANCH': '', 'CATALOG_PROFILE': ''}):
            p, _ = fixture(Path(directory), {'question_mode': 'aY'})
            p.state['refinementPlan'] = parse_plan({'question_mode': 'aN', 'catalog_brief': 'SYNTHETIC new interest'})
            p.state.pop('frozenManifest')
            old = file_manifest(p.work / 'base')
            def interrupt(path, value):
                if Path(path) == p.work / 'base/json/en/1.json':
                    raise OSError('Synthetic interrupted taxonomy write')
                return write(path, value)
            with patch('ci.taxonomy.write', side_effect=interrupt):
                with self.assertRaisesRegex(OSError, 'interrupted taxonomy'):
                    ensure_taxonomy(p)
            self.assertIn('taxonomySeedPending', p.state)
            p.pinned()
            self.assertEqual(file_manifest(p.work / 'base'), old)
            self.assertNotIn('taxonomySeedPending', p.state)
            with patch('ci.taxonomy.synthetic_taxonomy', side_effect=AssertionError('Do not reauthor reviewed data')):
                ensure_taxonomy(p)
            self.assertEqual(p.state['taxonomyGeneration']['status'], 'checked-draft')


if __name__ == '__main__':
    unittest.main()
