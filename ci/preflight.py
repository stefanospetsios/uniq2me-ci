"""Runner prerequisites; live capability checks are orchestrated by Pipeline."""
import copy
import platform
import shutil
import sys
from pathlib import Path

from ci.contracts import require
from ci.source_passages import PassageValidationError, check_source_passages

PILOT_URL = 'https://docs.ollama.com/faq'


class SourcePilot:
    """Check preflight quotations against one fixed, independently fetched snapshot."""
    def __init__(self, snapshot, archive, checks):
        self.snapshot, self.checks = snapshot, checks
        self.path = Path(archive) / snapshot['textPath']
        self.source_text = self.path.read_text(encoding='utf-8')
        self.admitted = None

    def evidence(self):
        return {'url': PILOT_URL, 'localTextPath': str(self.path),
                'byteDigest': self.snapshot['byteDigest'], 'textDigest': self.snapshot['textDigest'],
                'textExcerpt': self.source_text[:40000], 'excerptTruncated': len(self.source_text) > 40000}

    def __call__(self, result, transcript):
        require(isinstance(result, dict) and result.get('status') == 'ready',
                'Runner agent JSON smoke test did not report ready')
        source = result.get('source')
        require(isinstance(source, dict) and source.get('url') == PILOT_URL,
                'Runner smoke test must retrieve the requested official document')
        self.checks['agentJsonAndToolUse'] = 'passed'
        candidate = copy.deepcopy(self.snapshot)
        candidate['passages'] = source.get('passages')
        try:
            self.admitted = check_source_passages(candidate, self.source_text)
        except PassageValidationError as error:
            error.details = {'phase': 'preflight-evidence', 'url': PILOT_URL,
                'localTextPath': str(self.path), 'byteDigest': self.snapshot['byteDigest'],
                'textDigest': self.snapshot['textDigest'],
                'passageFailures': error.failures}
            raise
        return result


def runner_checks(workspace, need_opencode):
    require(sys.version_info >= (3, 10), 'Runner requires Python 3.10+')
    programs = ['git', 'curl', 'jq', 'pdftotext'] + (['opencode'] if need_opencode else [])
    found = {name: shutil.which(name) is not None for name in programs}
    require(all(found.values()), 'Missing runner programs: ' + ', '.join(name for name in found if not found[name]))
    free = shutil.disk_usage(workspace).free
    require(free > 0, 'Runner workspace has no free disk space')
    return {'pythonVersion': platform.python_version(), 'platform': platform.system(),
            'programsPresent': found, 'workspaceFreeBytes': free}
