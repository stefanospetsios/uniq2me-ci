"""Exercise scoped authentication without a network or real credentials."""
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci.git_auth import git_environment
from ci.pipeline import command
from ci.reference_inputs import _git
from ci.source_cache import git


class GitAuthenticationTests(unittest.TestCase):
    def test_all_git_routes_supply_compatible_headers_without_persisting_credentials(self):
        token = 'synthetic-test-token'
        expected = 'AUTHORIZATION: basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode()
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['git', 'init', '-q', directory], check=True)
            path = Path(directory) / '.git/config'
            before = path.read_bytes()
            args = ['config', '--get-urlmatch', 'http.extraheader', 'https://github.com/example/catalog.git']
            with patch.dict(os.environ, {'PUSH_TOKEN': token}):
                self.assertEqual(command(['git', *args], directory, authenticated=True).stdout.strip(), expected)
            self.assertEqual(_git(directory, args, token=token).stdout.decode().strip(), expected)
            self.assertEqual(git(args, directory, token=token).stdout.strip(), expected)
            self.assertEqual(path.read_bytes(), before)

    def test_legacy_encoding_preserves_other_settings_and_disables_credential_tracing(self):
        env = git_environment('synthetic-test-token', {'PATH': os.environ['PATH'],
            'GIT_CONFIG_PARAMETERS': "'example.preserved=yes'", 'GIT_TRACE_CURL': '1', 'GIT_CURL_VERBOSE': '1'})
        self.assertNotIn('GIT_CONFIG_COUNT', env)
        self.assertNotIn('GIT_TRACE_CURL', env)
        self.assertNotIn('GIT_CURL_VERBOSE', env)
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        result = subprocess.check_output(['git', 'config', '--get', 'example.preserved'], env=env, text=True)
        self.assertEqual(result.strip(), 'yes')


if __name__ == '__main__':
    unittest.main()
