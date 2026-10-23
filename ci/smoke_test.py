"""Offline dry-run demo with temporary local repositories and checkpoint restores."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import HEX, file_manifest, read, write
from ci.pipeline import Pipeline, STAGES, command
from ci.checkpoints import restore, seal
from ci.runtime import utc


def configure_fixture_git(repository):
    # Newer Git can detach automatic maintenance after a fixture commit/push.
    # Keep temporary repositories quiescent before TemporaryDirectory cleanup.
    for key, value in (('gc.auto', '0'), ('maintenance.auto', 'false')):
        command(['git', 'config', '--local', key, value], repository)


def create_fixture_pipeline(root, publish=True):
    """Test scaffolding only; real CI initialize() uses the actual pinned repo."""
    root = Path(root)
    pipeline = Pipeline()
    pipeline.workspace, pipeline.root = root, root / 'runner/uniq2me-run'
    pipeline.work, pipeline.output = pipeline.root / 'work', root / 'uniq2me-ai'
    pipeline.state_path = pipeline.root / 'controller-state.json'
    pipeline.work.mkdir(parents=True)
    bare = root / 'local-test-remote.git'
    command(['git', 'init', '--bare', '-q', str(bare)], root)
    configure_fixture_git(bare)
    pipeline.output.mkdir()
    command(['git', 'init', '-q', '-b', 'main'], pipeline.output)
    configure_fixture_git(pipeline.output)
    (pipeline.output / 'README.md').write_text('SYNTHETIC offline test. No GitHub writes.\n')
    command(['git', 'add', 'README.md'], pipeline.output)
    command(['git', '-c', 'user.name=CI fixture', '-c', 'user.email=fixture@example.invalid',
             'commit', '-qm', 'Synthetic test base'], pipeline.output)
    command(['git', 'remote', 'add', 'origin', str(bare)], pipeline.output)
    command(['git', 'push', '-q', 'origin', 'main'], pipeline.output)
    command(['git', 'symbolic-ref', 'HEAD', 'refs/heads/main'], bare)
    base = command(['git', 'rev-parse', 'HEAD'], pipeline.output).stdout.strip()
    fixtures = pipeline.trusted / 'agent-rules/tests/fixtures'
    slots = []
    for sfs in HEX:
        area = read(fixtures / 'sfs-template.json')
        area['keyTitle'] = 'SYNTHETIC offline area ' + sfs
        for entry in area['set']:
            entry['repoId'] = '00' + sfs + entry['id']
            slot = {'sfsId': sfs, 'sfId': entry['id'], 'repoId': entry['repoId']}
            slots.append(slot)
            template = read(fixtures / 'sf-template.json')
            template['identity'] = {**slot, 'language': 'en'}
            write(pipeline.work / 'inputs/repo-sf/en' / (slot['repoId'] + '.json'), template)
        write(pipeline.work / 'inputs/json/en' / (sfs + '.json'), area)
    write(pipeline.work / 'inputs/schemas/mask.schema.json', read(fixtures / 'mask.schema.json'))
    (pipeline.work / 'inputs/main.tex').write_text('% SYNTHETIC offline methodology fixture\n')
    config = read(pipeline.trusted / 'agent-rules/run-config.example.json')
    write(pipeline.work / 'agent-rules/run-config.json', config)
    pipeline.state = {'runId': 'qwen3-offline-test', 'branch': 'dry-run/qwen3/261012',
        'publish': publish, 'startedAt': utc(), 'inputCommit': '1' * 40, 'outputBaseCommit': base,
        'expectedRemote': None, 'controllerCommit': command(['git', 'rev-parse', 'HEAD'], pipeline.trusted).stdout.strip(),
        'inputManifest': file_manifest(pipeline.work / 'inputs'),
        'rulesManifest': file_manifest(pipeline.work / 'agent-rules'), 'slots': slots,
        'timeoutsMinutes': {key: 5 for key in ('X', 'Y', 'Z', 'REVIEW_MINUTES', 'SOURCE_MINUTES', 'MASK_MINUTES')},
        'maximumAttempts': 2, 'stages': {}, 'publications': [], 'distillation': 'todo-not-run',
        'baseModes': {'catalog': 'empty', 'masks': 'empty'}, 'priorRelease': None,
        'dryRun': True, 'probeMcp': False, 'failOnceAt': 'none'}
    pipeline.save()
    return pipeline, bare


def restore_on_fresh_runner(pipeline, root):
    """A fresh checkout and temp directory must be sufficient to resume."""
    root = Path(root)
    root.mkdir(parents=True)
    destination = root / 'runner/uniq2me-run'
    shutil.copytree(pipeline.root, destination)
    remote = command(['git', 'remote', 'get-url', 'origin'], pipeline.output).stdout.strip()
    output = root / 'uniq2me-ai'
    command(['git', 'clone', '-q', '--branch', 'main', remote, str(output)], root)
    configure_fixture_git(output)
    new = Pipeline()
    new.workspace, new.root, new.work, new.output = root, destination, destination / 'work', output
    new.state_path = destination / 'controller-state.json'
    new.state = read(new.state_path)
    restore(new)
    return new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fail-once-at', choices=['none', *STAGES], default='extended')
    args = parser.parse_args()
    # These credentials are dummy values for file-based local Git remotes only.
    with tempfile.TemporaryDirectory(prefix='uniq2me-offline-') as directory, patch.dict(os.environ, {
        'PUSH_TOKEN': 'synthetic-local-token', 'DRY_RUN': 'true', 'GITHUB_OUTPUT': '',
        'GITHUB_STEP_SUMMARY': ''}):
        root = Path(directory)
        pipeline, bare = create_fixture_pipeline(root)
        pipeline.state['failOnceAt'] = args.fail_once_at
        pipeline.save()
        print('OFFLINE smoke test: temporary local Git remotes; zero model inference or GitHub writes')
        pipeline.execute('prepare')
        seal(pipeline)
        for index, stage in enumerate(STAGES):
            pipeline = restore_on_fresh_runner(pipeline, root / ('job-' + str(index)))
            try:
                pipeline.execute(stage)
            except RuntimeError as error:
                if 'Intentional one-time dry-run failure' not in str(error):
                    raise
                print(str(error))
                seal(pipeline)
                pipeline = restore_on_fresh_runner(pipeline, root / ('retry-' + str(index)))
                pipeline.execute(stage)
            pipeline.publish(stage)
            seal(pipeline)
        count = command(['git', 'rev-list', '--count', pipeline.state['branch']], bare).stdout.strip()
        assert count == '6', 'Expected one base commit and exactly five stage commits'
        print('PASS: 5 stage commits, fresh-runner checkpoint restores, retry/continuation, 256 positions per language')


if __name__ == '__main__':
    main()
