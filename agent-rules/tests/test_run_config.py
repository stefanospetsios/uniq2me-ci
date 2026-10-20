"""Boundary tests use local synthetic configuration records, not run evidence."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from validate_run_config import validate


class ConfigurationBoundaries(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / 'run-config.example.json').read_text())

    def test_template_never_claims_runtime_readiness(self):
        result = validate(self.data, allow_unconfigured=True)
        self.assertEqual(result['status'], 'template-unconfigured')
        self.assertGreater(len(result['unconfigured']), 0)

    def test_wrong_output_repository_is_rejected(self):
        self.data['repositories']['output'] = 'stefanospetsios/uniq2me'
        self.assertIn('output repository must be stefanospetsios/uniq2me-ai',
                      validate(self.data, True)['errors'])

    def test_reference_commit_configuration_requires_every_requested_snapshot(self):
        self.data['inputs']['referenceCommits'] = {
            'manifestRef': 'inputs/reference-inputs/manifest.json', 'manifestDigest': 'sha256:' + 'a' * 64,
            'requestedCommits': {'stefanospetsios/uniq2me': ['1' * 40], 'stefanospetsios/uniq2me-ai': []},
            'snapshots': []}
        errors = validate(self.data, True)['errors']
        self.assertIn('Every requested reference commit needs its exact snapshot', errors)

    def test_reference_commits_reject_unknown_repositories_and_unpinned_refs(self):
        self.data['inputs']['referenceCommits'] = {
            'manifestRef': 'inputs/reference-inputs/manifest.json', 'manifestDigest': 'sha256:' + 'a' * 64,
            'requestedCommits': {'someone/other': ['main']}, 'snapshots': []}
        errors = validate(self.data, True)['errors']
        self.assertIn('reference commits must use the two declared repositories', errors)
        self.assertIn('reference commits must contain full commit SHAs: someone/other', errors)

    def test_rag_mode_cannot_leave_web_discovery_enabled_when_disabled(self):
        self.data['rag'].update(mode='disabled', enabled=False, repositoryRetrieval=False)
        errors = validate(self.data, True)['errors']
        self.assertIn('RAG webResearch disagrees with mode', errors)
        self.assertIn('Web source discovery must match the selected RAG mode', errors)

    def test_existing_masks_need_original_catalog(self):
        self.data['inputs']['masks'].update(mode='existing', ref='fixture-mask-library')
        self.assertIn('existing masks require defining catalog bindings',
                      validate(self.data, True)['errors'])

    def test_boolean_is_not_a_token_budget(self):
        self.data['budgets']['X'] = True
        self.assertIn('X has an invalid value', validate(self.data, True)['errors'])

    def test_order_cannot_skip_solution_then_completion(self):
        self.data['authoringOrder'].reverse()
        self.assertIn('question/solution/completion order must be preserved',
                      validate(self.data, True)['errors'])

    def test_selective_plan_records_its_actual_task_order_and_model_a(self):
        sys.path.insert(0,str(ROOT.parent))
        from ci.refinement import parse_plan
        plan=parse_plan({'sfs':'A','complete_fields':False,'ai_prompts':True,'model_a':'custom-model:q8'})
        self.data.update(refinementPlan=plan,authoringOrder=['ai_prompts'])
        self.data['models']['primary']['tag']='custom-model:q8'
        errors=validate(self.data,True)['errors']
        self.assertNotIn('Refinement authoring order differs from the selected tasks',errors)
        self.assertNotIn('primary model tag differs from the requested deployment',errors)
        self.data['authoringOrder']=['canonical_solutions']
        self.assertIn('Refinement authoring order differs from the selected tasks',validate(self.data,True)['errors'])

    def test_context_fallback_must_be_explicit(self):
        primary = self.data['models']['primary']
        primary.update(digest='0' * 64, nativeContext=131072, observedContext=131072,
                       preflightStatus='passed', nativeToolCallingSupported=True)
        result = validate(self.data, True, True)
        self.assertIn('primary context reduction requires explicit fallback and reason', result['errors'])
        primary.update(explicitContextFallback=True, fallbackReason='Synthetic test fixture: native limit')
        self.assertNotIn('primary context reduction requires explicit fallback and reason',
                         validate(self.data, True, True)['errors'])

    def test_optional_review_is_not_silently_enabled(self):
        self.data['review']['crossLlmEnabled'] = True
        self.assertIn('cross-LLM review needs a selected optional model',
                      validate(self.data, True)['errors'])

    def test_student_weight_feedback_is_rejected(self):
        self.data['distillation']['authoringTeacherWeightsReceiveStudentFeedback'] = True
        self.assertIn('student weights must not feed the authoring recurrence',
                      validate(self.data, True)['errors'])

    def test_force_push_is_rejected(self):
        self.data['publication']['allowForcePush'] = True
        self.assertIn('force-push must remain disabled', validate(self.data, True)['errors'])

    def test_minutes_do_not_become_token_budgets(self):
        self.data['budgets'].update(mode='time-limited-token-metered', maximumAttempts=2)
        self.data['timeoutsMinutes'] = {key: 5 for key in
            ('X', 'Y', 'Z', 'REVIEW_MINUTES', 'SOURCE_MINUTES', 'MASK_MINUTES')}
        result = validate(self.data, True)
        self.assertFalse(any('timeout' in error for error in result['errors']))
        self.assertFalse(any(key + ' is unconfigured' in result['unconfigured']
                             for key in ('X', 'Y', 'R', 'G', 'reservedCompletionX')))
        self.data['timeoutsMinutes']['Z'] = True
        self.assertIn('timeout Z has an invalid value', validate(self.data, True)['errors'])

    def test_zero_minutes_disable_deadlines_and_negative_minutes_are_invalid(self):
        self.data['budgets'].update(mode='time-limited-token-metered', maximumAttempts=2)
        self.data['timeoutsMinutes'] = {key: 0 for key in
            ('X', 'Y', 'Z', 'REVIEW_MINUTES', 'SOURCE_MINUTES', 'MASK_MINUTES')}
        self.assertFalse(any('timeout' in error for error in validate(self.data, True)['errors']))
        self.data['timeoutsMinutes']['X'] = -1
        self.assertIn('timeout X has an invalid value', validate(self.data, True)['errors'])


if __name__ == '__main__':
    unittest.main()
