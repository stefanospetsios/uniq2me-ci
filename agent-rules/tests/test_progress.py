"""Stage/function diagnostics preserve errors, CLI failure records and context."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import read
from ci.pipeline import main
from ci.progress import current_stage, log, traced


class ProgressTests(unittest.TestCase):
    def test_nested_failure_has_stage_and_function_and_preserves_exception(self):
        error = ValueError('Synthetic invalid concern')
        class Example:
            @traced(stage_arg=1)
            def execute(self, stage):
                return self.generate()
            @traced
            def generate(self):
                log('generate.validate', 'checking candidate')
                raise error
        output = io.StringIO()
        with patch.dict(os.environ, {'STAGE': 'preflight'}), redirect_stdout(output):
            with self.assertRaises(ValueError) as caught:
                Example().execute(stage='catalog')
            self.assertIs(caught.exception, error)
            self.assertEqual(current_stage(), 'preflight')
        self.assertIn('catalog.execute: START', output.getvalue())
        self.assertIn('catalog.generate.validate: checking candidate', output.getvalue())
        self.assertIn('catalog.generate: FAILED: Synthetic invalid concern', output.getvalue())
        self.assertEqual(error._pipeline_function, 'generate')

    def test_cli_failure_is_reported_on_stdout_and_records_stage_and_function(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = Mock(root=Path(directory))
            error = TimeoutError('Synthetic model deadline exceeded')
            error._pipeline_function = 'OpenCode.ask'
            pipeline.execute.side_effect = error
            output = io.StringIO()
            with patch('ci.pipeline.Pipeline', return_value=pipeline), \
                 patch('ci.pipeline.signal.signal'), patch.object(sys, 'argv', ['pipeline.py', 'execute', 'catalog']), \
                 redirect_stdout(output):
                result = main()
            self.assertEqual(result, 1)
            self.assertIn('catalog.main: Pipeline stopped in OpenCode.ask', output.getvalue())
            failure = read(Path(directory) / 'last-failure.json')
            self.assertEqual(failure['stage'], 'catalog')
            self.assertEqual(failure['function'], 'OpenCode.ask')
            self.assertEqual(failure['reason'], str(error))


if __name__ == '__main__':
    unittest.main()
