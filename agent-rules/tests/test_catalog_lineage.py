"""Exercise catalog repair lineage without live inference or publication."""
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import digest, read, write
from ci.pipeline import Pipeline
from test_pipeline import SOURCES, question, review


SLOT = {'sfsId': '0', 'sfId': '0', 'repoId': '0000'}


class Server:
    def __init__(self, candidates, reject_first_review):
        self.candidates = iter(candidates)
        self.reject_first_review = reject_first_review
        self.prompts = []
        self.reviews = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def ask(self, role, prompt, seconds, call_id, **kwargs):
        self.prompts.append((role, prompt))
        if role == 'question-author':
            self.current = copy.deepcopy(next(self.candidates))
            return self.current
        self.reviews += 1
        payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"candidate":'):])
        result = review(self.current, payload['requiredChecks'], self.current['fixtures'])
        if self.reject_first_review and self.reviews == 1:
            result['decision'] = 'rejected'
            result['checks'][2].update(passed=False, rationale='Synthetic technical term needs plain wording')
        return result


class CatalogLineageTests(unittest.TestCase):
    @contextmanager
    def catalog(self, candidates, *, previous=None, reject_first_review=False):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RUNNER_TEMP': directory}), \
             redirect_stdout(io.StringIO()):
            pipeline = Pipeline()
            pipeline.state = {'runId': 'lineage-test', 'slots': [SLOT],
                'maximumAttempts': len(candidates), 'baseModes': {'catalog': 'existing' if previous else 'empty'},
                'timeoutsMinutes': {'X': 0, 'REVIEW_MINUTES': 0}, 'model': {'digest': 'a' * 64}}
            pipeline.check_previous = lambda _: None
            pipeline.gate = lambda *args, **kwargs: None
            pipeline.roster = lambda _: None
            pipeline.frozen_sources = lambda: SOURCES
            write(pipeline.work / 'frozen/concerns.json', [{'id': '0-concern', 'sfsId': '0'}])
            write(pipeline.work / 'inputs/json/en/0.json', {'keyTitle': 'Synthetic area'})
            if previous:
                write(pipeline.work / 'inputs/prior-release/catalog/questions/en/0000.json', previous)
            server = Server(candidates, reject_first_review)
            pipeline.server = lambda _: server
            yield pipeline, server

    def test_one_repair_can_fix_both_errors_from_the_live_0077_failure(self):
        invalid = question(SLOT)
        invalid['contract']['alternatives'] = []
        invalid['fixtures'][-1]['evidence'] = ''
        repaired = question(SLOT)
        with self.catalog([invalid, repaired]) as (pipeline, server):
            pipeline.catalog()
            self.assertEqual(server.reviews, 1)
            repair_prompt = server.prompts[1][1]
            self.assertIn('alternatives must be filled', repair_prompt)
            self.assertIn('fixture.evidence must be filled', repair_prompt)
            self.assertIn('describe the absence of evidence', repair_prompt)
            self.assertEqual(read(pipeline.work / '.task-cache/catalog/0000.json')['value'], repaired)

    def test_wording_repair_retains_initial_lineage_in_complete_json_example(self):
        first = question(SLOT)
        repaired = copy.deepcopy(first)
        repaired['shortBinaryQuestion'] = 'Has the synthetic test condition been met?'
        with self.catalog([first, repaired], reject_first_review=True) as (pipeline, server):
            pipeline.catalog()
            saved = read(pipeline.work / 'catalog/questions/en/0000.json')
            self.assertEqual(saved, repaired)
            self.assertIsNone(saved['parentQuestionDigest'])
            self.assertEqual(saved['semanticRelation'], 'initial')
            for role, prompt in server.prompts:
                if role == 'question-author':
                    example, _ = json.JSONDecoder().raw_decode(prompt.split('\nReturn ', 1)[1])
                    self.assertIn('parentQuestionDigest', example)
                    self.assertIsNone(example['parentQuestionDigest'])
                    self.assertEqual(example['semanticRelation'], 'initial')
                    self.assertIn('changeReason', example)
            self.assertIn('semanticRelation must remain "initial"', server.prompts[2][1])
            self.assertEqual(read(pipeline.work / '.task-cache/catalog/0000.json')['value'], repaired)

    def test_lineage_repair_retains_wording_review_and_exact_invalid_candidate(self):
        first = question(SLOT)
        invalid = copy.deepcopy(first)
        invalid.update(shortBinaryQuestion='Has the synthetic test condition been met?', semanticRelation='changed')
        repaired = {**invalid, 'semanticRelation': 'initial'}
        with self.catalog([first, invalid, repaired], reject_first_review=True) as (pipeline, server):
            pipeline.catalog()
            self.assertEqual(server.reviews, 2)  # The invalid lineage never reaches semantic review.
            prompt = server.prompts[3][1]
            self.assertIn('semanticRelation must be one of ["initial"]; received "changed"', prompt)
            self.assertIn('Synthetic technical term needs plain wording', prompt)
            self.assertIn(json.dumps(invalid, ensure_ascii=False), prompt)
            self.assertIn(digest(invalid), prompt)
            self.assertEqual(read(pipeline.work / 'catalog/questions/en/0000.json'), repaired)

    def test_invalid_lineage_remains_a_strict_failure(self):
        for field, value in [('semanticRelation', 'changed'), ('semanticRelation', 'preserved'),
                             ('semanticRelation', None), ('parentQuestionDigest', 'sha256:' + '0' * 64),
                             ('parentQuestionDigest', None), ('changeReason', None)]:
            with self.subTest(field=field, value=value):
                invalid = question(SLOT)
                if value is None:
                    invalid.pop(field)
                else:
                    invalid[field] = value
                with self.catalog([invalid]) as (pipeline, server):
                    with self.assertRaisesRegex(RuntimeError, 'failed bounded generation'):
                        pipeline.catalog()
                    self.assertEqual(server.reviews, 0)
                    self.assertFalse((pipeline.work / '.task-cache/catalog/0000.json').exists())

    def test_existing_parent_allows_preserved_or_changed_and_rejects_initial_or_wrong_digest(self):
        previous = question(SLOT)
        for relation in ('preserved', 'changed', 'initial', 'wrong-parent'):
            with self.subTest(relation=relation):
                value = question(SLOT)
                value.update(parentQuestionDigest=digest(previous), semanticRelation=relation)
                if relation == 'wrong-parent':
                    value.update(parentQuestionDigest=digest({'rejectedAttempt': True}), semanticRelation='changed')
                with self.catalog([value], previous=previous) as (pipeline, server):
                    if relation in ('initial', 'wrong-parent'):
                        with self.assertRaisesRegex(RuntimeError, 'failed bounded generation'):
                            pipeline.catalog()
                        self.assertEqual(server.reviews, 0)
                    else:
                        pipeline.catalog()
                        self.assertEqual(read(pipeline.work / 'catalog/questions/en/0000.json'), value)
                        example, _ = json.JSONDecoder().raw_decode(server.prompts[0][1].split('\nReturn ', 1)[1])
                        self.assertEqual(example['parentQuestionDigest'], digest(previous))


if __name__ == '__main__':
    unittest.main()
