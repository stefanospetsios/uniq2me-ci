"""Check public controller files/history without exposing any matched secret."""
import argparse
import re
import subprocess
from pathlib import Path

PATTERNS = {
    'GitHub credential': rb'(?:github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{30,})',
    'private key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=]{20,}',
    'private infrastructure address': rb'kic[.]uoi[.]gr|/Users[/]stefanos[/]|/home[/]stefanos[/]',
}


def findings(data):
    return [name for name, pattern in PATTERNS.items() if re.search(pattern, data)]


def audit(root, history=False):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root)
    problems = []
    paths = git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')
    for name in sorted(set(paths) - {''}):
        path = root / name
        if not path.exists():
            continue
        if path.is_symlink():
            problems.append((name, 'symlink'))
            continue
        if name.endswith(('.zip', '.log', '.bundle', '.pyc')) or name.startswith((
                'data/', 'runs/', 'catalog/', 'json/', 'repo-sf/', 'rag-cache/', 'runtime/')):
            problems.append((name, 'runtime/research/catalog artifact'))
        if name.endswith('.tex') and not name.startswith('agent-rules/tests/fixtures/'):
            problems.append((name, 'private manuscript'))
        for category in findings(path.read_bytes()):
            problems.append((name, category))
    if history:
        for row in git('rev-list', '--objects', '--all').decode().splitlines():
            sha, _, name = row.partition(' ')
            if git('cat-file', '-t', sha).strip() != b'blob':
                continue
            for category in findings(git('cat-file', 'blob', sha)):
                problems.append(('history:' + sha + ':' + name, category))
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true')
    args = parser.parse_args()
    problems = audit(Path(__file__).resolve().parents[1], args.history)
    for name, category in problems:
        print(f'Public audit rejected {name}: {category}')
    if problems:
        raise SystemExit(1)
    print('Public file' + (' and history' if args.history else '') + ' audit passed. Pattern scanning is not a guarantee that every sensitive value is detectable.')


if __name__ == '__main__':
    main()
