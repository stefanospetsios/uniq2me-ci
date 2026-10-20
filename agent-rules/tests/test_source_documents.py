"""HTTP identifiers and challenge pages must not weaken source admission."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ci.pipeline import official_url
from ci.source_documents import ensure_source_available, source_request_url

CHALLENGE = ("JavaScript is disabled. In order to continue, we need to verify that you're not a robot. "
             'This requires JavaScript. Enable JavaScript and then reload the page.')


class SourceDocumentTests(unittest.TestCase):
    def test_official_eli_identifiers_are_upgraded_without_changing_other_addresses(self):
        for path in ('/eli/reg/2022/2554/oj', '/eli/reg_del/2024/1774/oj/eng', '/eli/dir/2022/2555/oj',
                     '/eli/reg/2022/2554/oj/eng/pdf', '/eli/reg/2022/2554/2025-01-17/eng'):
            original = 'http://data.europa.eu' + path
            official_url(original)
            self.assertEqual(source_request_url(original), 'https://data.europa.eu' + path)
        self.assertEqual(source_request_url('https://docs.example.org/manual'), 'https://docs.example.org/manual')

    def test_identifier_exception_does_not_allow_arbitrary_http_or_credentials(self):
        for url in ('http://docs.example.org/manual', 'http://data.europa.eu/other-page',
                    'http://data.europa.eu.evil.invalid/eli/reg/2022/2554/oj',
                    'http://user:password@data.europa.eu/eli/reg/2022/2554/oj',
                    'http://data.europa.eu:8080/eli/reg/2022/2554/oj'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                official_url(url)

    def test_http_success_challenges_and_empty_text_are_retryable_download_failures(self):
        for body in (CHALLENGE, CHALLENGE.replace("you're", 'you are'),
                     'Just a moment... Enable JavaScript and cookies to continue.',
                     'Checking your browser before accessing the document. You will be redirected automatically.',
                     '', ' \n\t '):
            with self.subTest(body=body), self.assertRaisesRegex(OSError, 'unavailable or incomplete'):
                ensure_source_available(body)

    def test_javascript_guidance_and_optional_noscript_notices_remain_usable(self):
        for body in ('Enable JavaScript to use the interactive examples. The requirements follow.',
                     'This documentation explains how JavaScript detects bots and robots.',
                     'JavaScript is disabled. The complete regulatory text follows. Article 1 Subject matter.'):
            ensure_source_available(body)
