"""Quotation equivalence must never become approximate evidence admission."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ci.source_passages import PassageValidationError, check_source_passages


def source(quotation):
    return {'id': '1-source-2', 'textPath': 'synthetic-cisa.txt', 'passages': [
        {'id': 'p2', 'locator': 'Synthetic heading', 'text': quotation}]}


class SourcePassageTests(unittest.TestCase):
    def test_present_short_heading_reports_length_and_whitespace_padding_does_not_help(self):
        actual = 'Article 2: Scope\nFinancial entities must identify their applicable obligations.'
        for quotation in ('Article 2: Scope', 'Article 2: Scope\n\n', '  Article  2:  Scope  '):
            with self.subTest(quotation=quotation):
                with self.assertRaises(PassageValidationError) as failure:
                    check_source_passages(source(quotation), actual)
                row = failure.exception.failures[0]
                self.assertEqual(row['ruleId'], 'quotation-too-short')
                self.assertEqual(row['normalizedCharacters'], 16)
                self.assertEqual(row['minimumCharacters'], 20)
                self.assertEqual(row['claimedText'], quotation)
                self.assertIn('longer substantive contiguous passage', row['reason'])
                self.assertNotIn('not found', row['reason'])
                self.assertIn(row['sourceExcerpt'], actual)

    def test_minimum_length_boundary_still_accepts_exact_twenty_character_quotation(self):
        actual = 'Keep access records.'
        with self.assertRaises(PassageValidationError) as failure:
            check_source_passages(source('Keep access records'), actual)
        self.assertEqual(failure.exception.failures[0]['normalizedCharacters'], 19)
        self.assertEqual(check_source_passages(source(actual), actual)['sourceStatus'],
                         'fetched-and-passage-checked')

    def test_long_enough_missing_quotation_retains_the_match_failure(self):
        with self.assertRaises(PassageValidationError) as failure:
            check_source_passages(source('Keep change records.'), 'Keep access records.')
        self.assertEqual(failure.exception.failures[0]['ruleId'], 'quotation-not-found')
        self.assertIn('not found', str(failure.exception))

    def test_observed_cisa_apostrophes_pass_without_changing_the_claim_or_source_bytes(self):
        quotation = ("Functionality in the ILM sub-capability is focused on the lifecycle management "
                     "of a user's identity and their associated privileges throughout the user's "
                     "association with the agency.")
        actual = quotation.replace("user's", 'user’s')
        candidate = source(quotation)
        before = copy.deepcopy(candidate)
        checked = check_source_passages(candidate, actual)
        self.assertEqual(candidate, before)
        self.assertEqual(checked['passages'], before['passages'])
        self.assertEqual(checked['sourceStatus'], 'fetched-and-passage-checked')
        self.assertEqual(checked['typographyNormalizedPassages'], ['p2'])
        self.assertEqual(checked['quotationMatchPolicy'], 'unicode-typography-v1')

    def test_whitespace_composition_quotes_and_pdf_ligatures_are_formatting_only(self):
        quotation = 'The café office must maintain "verified" identity records.'
        actual = 'The cafe\u0301\u00a0of\ufb01ce\nmust maintain “veri\u00adfied” identity records.'
        checked = check_source_passages(source(quotation), actual)
        self.assertEqual(checked['typographyNormalizedPassages'], ['p2'])

    def test_semantic_changes_numbers_case_math_and_identifier_hyphens_remain_rejected(self):
        cases = [
            ('Accounts must not share privileged access credentials.',
             'Accounts must share privileged access credentials.'),
            ('Recovery must finish within 24 hours for all devices.',
             'Recovery must finish within 48 hours for all devices.'),
            ('The maximum value is 2 m² for the selected enclosure.',
             'The maximum value is 2 m2 for the selected enclosure.'),
            ('The measured offset must equal −3 for every sample.',
             'The measured offset must equal -3 for every sample.'),
            ('Use the identifier master-key for the selected account.',
             'Use the identifier masterkey for the selected account.'),
            ('Provision the token CASE_SENSITIVE for this application.',
             'Provision the token case_sensitive for this application.'),
            ('The measured duration is 5′ for the selected operation.',
             "The measured duration is 5' for the selected operation."),
        ]
        for actual, quotation in cases:
            with self.subTest(quotation=quotation), self.assertRaises(PassageValidationError):
                check_source_passages(source(quotation), actual)

    def test_joined_separate_spans_and_fuzzy_paraphrases_remain_rejected(self):
        actual = ('Privileged accounts require documented approval. '
                  'This sentence must not be silently omitted. '
                  'Administrators maintain audit trails for all changes.')
        for quotation in [
                'Privileged accounts require documented approval. Administrators maintain audit trails for all changes.',
                'Privileged users require formal approval and administrators must retain audit records.']:
            with self.subTest(quotation=quotation), self.assertRaises(PassageValidationError):
                check_source_passages(source(quotation), actual)

    def test_a_quotation_cannot_truncate_a_number_or_an_identifier(self):
        for actual, quotation in [
            ('Recovery must finish within 240 hours.', 'Recovery must finish within 24'),
            ('The maximum allowed duration is 24.5 hours.', 'The maximum allowed duration is 24.'),
            ('The configured account is administrator_extended.', 'The configured account is administrator'),
        ]:
            with self.subTest(quotation=quotation), self.assertRaises(PassageValidationError):
                check_source_passages(source(quotation), actual)

    def test_all_passage_errors_are_returned_with_exact_source_reading_hints(self):
        actual = 'Administrators maintain audit trails for all privileged account changes.'
        candidate = source('Administrators maintain audit trails for all unprivileged account changes.')
        candidate['passages'].append({'id': 'p3', 'locator': 'Synthetic heading',
            'text': 'Administrators maintain audit records for all privileged account changes.'})
        with self.assertRaises(PassageValidationError) as failure:
            check_source_passages(candidate, actual)
        self.assertEqual([row['passageId'] for row in failure.exception.failures], ['p2', 'p3'])
        for row in failure.exception.failures:
            self.assertIn(row['sourceExcerpt'], actual)
            self.assertTrue(row['excerptIsReadingHint'])
            self.assertEqual(row['textPath'], 'synthetic-cisa.txt')
        self.assertIn('1-source-2/p2', str(failure.exception))
        self.assertIn('1-source-2/p3', str(failure.exception))

    def test_malformed_passages_report_all_errors_without_uncaught_type_failures(self):
        candidate = {'id': '1-source-2', 'passages': [None,
            {'id': ['invalid'], 'text': 42}, {'id': 'p1', 'locator': ''}]}
        with self.assertRaises(PassageValidationError) as failure:
            check_source_passages(candidate, 'A substantive source passage containing more than twenty characters.')
        self.assertGreaterEqual(len(failure.exception.failures), 5)


if __name__ == '__main__':
    unittest.main()
