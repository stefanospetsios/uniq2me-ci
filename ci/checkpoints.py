"""Artifact checkpoints move complete/partial work and Git history between jobs."""
import argparse
import os
import re
import sys
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.progress import log
from ci.contracts import byte_digest, file_manifest, read, require, write
from ci.pipeline import Pipeline, command, remote_head
from ci.runtime import http_json, utc


def output(name, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write(name + '=' + str(value) + '\n')


def select_artifact(artifacts, run_id, attempt=None):
    prefix = 'uniq2me-checkpoint-' + str(run_id) + '-'
    eligible = [a for a in artifacts if a['name'].startswith(prefix) and not a.get('expired')
                and (attempt is None or a['name'].endswith('-' + str(attempt)))]
    # GitHub artifact IDs are identifiers, not a chronological sequence. A later
    # stage can have a smaller ID than its predecessor, so order by creation time.
    return max(eligible, key=lambda a: (
        datetime.fromisoformat(a['created_at'].replace('Z', '+00:00')), int(a['id'])
    )) if eligible else None


def locate():
    run_id, repo = os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_REPOSITORY']
    require(bool(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)), 'Invalid controller repository')
    require(run_id.isdigit(), 'Invalid workflow run ID')
    fresh = os.environ.get('FRESH_START', 'false').lower() == 'true'
    resume_id = os.environ.get('RESUME_RUN_ID', '').strip()
    require(not resume_id or resume_id.isdigit(), 'Resume run ID must be numeric')
    require(not (fresh and resume_id), 'Choose either a fresh start or a checkpoint resume')
    if fresh and os.environ.get('STAGE') == 'preflight':
        output('artifact_id', '')
        output('artifact_name', '')
        log('checkpoints.locate', 'Fresh start: ignore previous checkpoints and regenerate from pinned templates')
        return
    token = os.environ['GITHUB_TOKEN']
    selected, artifact_run = None, run_id
    for candidate_run in dict.fromkeys([run_id, *([resume_id] if resume_id else [])]):
        if os.environ.get('CHECKPOINT_URL', '').strip():
            from ci.nexus_checkpoints import Nexus, records
            snapshots = records(Nexus(), repo, candidate_run,
                                os.environ['GITHUB_RUN_ATTEMPT'] if fresh else None)
            if snapshots:
                selected = snapshots[0]
                output('backend', 'nexus')
                output('marker_path', selected['markerPath'])
                output('artifact_id', '')
                output('artifact_name', selected['name'])
                output('artifact_run_id', candidate_run)
                log('checkpoints.locate', 'Nexus checkpoint: ' + selected['name'])
                return
        artifacts, page = [], 1
        while True:
            data = http_json(os.environ.get('GITHUB_API_URL', 'https://api.github.com') +
                f'/repos/{repo}/actions/runs/{candidate_run}/artifacts?per_page=100&page={page}',
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'})
            rows = data['artifacts']
            artifacts.extend(rows)
            if len(rows) < 100:
                break
            page += 1
        selected = select_artifact(artifacts, candidate_run, os.environ['GITHUB_RUN_ATTEMPT'] if fresh else None)
        if selected:
            artifact_run = candidate_run
            break
    require(not resume_id or selected is not None, 'Requested resume checkpoint is missing or expired')
    require(selected is not None or os.environ.get('STAGE', 'preflight') == 'preflight',
            'No checkpoint for this attempt. With fresh_start, use Re-run all jobs; '
            'a downstream job cannot recover from an earlier attempt')
    output('artifact_id', selected['id'] if selected else '')
    output('backend', 'github' if selected else '')
    output('artifact_name', selected['name'] if selected else '')
    output('artifact_run_id', artifact_run)
    log('checkpoints.locate', 'Checkpoint: ' + (selected['name'] if selected else 'new run'))


def seal(pipeline):
    if not pipeline.state_path.exists():
        output('ready', 'false')
        log('checkpoints.seal', 'Initialization has no restorable state yet')
        return
    bundle = pipeline.root / 'output.bundle'
    if (pipeline.output / '.git').exists():
        current = command(['git', 'rev-parse', 'HEAD'], pipeline.output).stdout.strip()
        pending = pipeline.state.get('publicationIntent')
        if pending and pending.get('incremental') and pending.get('commit') and current == pending['oldHead']:
            for name, checksum in pending['artifactFiles'].items():
                require(byte_digest(pipeline.output / name) == checksum, 'Prepared SF publication changed')
            command(['git', 'update-ref', 'HEAD', pending['commit'], current], pipeline.output)
            current = pending['commit']
        # A commit can succeed immediately before the process loses its journal save.
        # Only the exact expected CI commit and its exact parent may be recovered.
        if (pending and not pending.get('commit') and current != pending['parent']
                and not (pending.get('incremental') and current == pending['oldHead'])):
            require(command(['git', 'log', '-1', '--format=%s'], pipeline.output).stdout.strip()
                    == pending['message'], 'Unexpected commit while sealing publication recovery')
            require(command(['git', 'rev-parse', 'HEAD^'], pipeline.output).stdout.strip()
                    == pending['parent'], 'Unexpected parent while sealing publication recovery')
            pending['commit'] = current
            pipeline.save()
        temporary = pipeline.root / ('output-' + uuid.uuid4().hex + '.bundle')
        command(['git', 'bundle', 'create', str(temporary), 'HEAD'], pipeline.output)
        temporary.replace(bundle)
    else:
        require(bundle.exists(), 'No output checkout or saved Git bundle exists')
        current = read(pipeline.root / 'checkpoint-manifest.json')['outputHead']
    manifest_path = pipeline.root / 'checkpoint-manifest.json'
    files = file_manifest(pipeline.root)
    files.pop('checkpoint-manifest.json', None)
    write(manifest_path, {'recordType': 'ci-checkpoint', 'sealedAt': utc(),
        'workflowRunId': os.environ.get('GITHUB_RUN_ID'), 'runId': pipeline.state['runId'],
        'workflowAttempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
        'workflowRepository': pipeline.state.get('workflowRepository') or os.environ.get('GITHUB_REPOSITORY'),
        'controllerRepository': pipeline.state.get('controllerRepository'),
        'controllerCommit': pipeline.state['controllerCommit'], 'outputHead': current,
        'files': files, 'dryRun': pipeline.state.get('dryRun', False)})
    output('ready', 'true')
    log('checkpoints.seal', f'Sealed checkpoint: {len(files)} files, output HEAD {current}')


def restore(pipeline):
    manifest = read(pipeline.root / 'checkpoint-manifest.json')
    require(manifest['recordType'] == 'ci-checkpoint', 'Not a CI checkpoint')
    if os.environ.get('GITHUB_RUN_ID'):
        require(manifest['workflowRunId'] in (os.environ['GITHUB_RUN_ID'], os.environ.get('RESUME_RUN_ID')),
                'Checkpoint belongs to another run; choose it explicitly with resume_run_id')
    if os.environ.get('FRESH_START', 'false').lower() == 'true':
        require(manifest.get('workflowAttempt') == os.environ['GITHUB_RUN_ATTEMPT'],
                'Fresh start cannot restore a previous attempt; use Re-run all jobs')
    actual = file_manifest(pipeline.root)
    actual.pop('checkpoint-manifest.json', None)
    require(actual == manifest['files'], 'Checkpoint file hashes do not match')
    controller = command(['git', 'rev-parse', 'HEAD'], pipeline.trusted).stdout.strip()
    from ci.controller_updates import upgrade
    upgrade(pipeline, manifest, controller, command)
    require(os.environ.get('DRY_RUN', 'false').lower() == str(manifest['dryRun']).lower(),
            'Cannot switch between real and dry modes while resuming')
    if os.environ.get('PUBLISH'):
        require(os.environ['PUBLISH'].lower() == str(pipeline.state['publish']).lower(),
                'Cannot change publication mode while resuming')
    require(not command(['git', 'status', '--porcelain'], pipeline.output).stdout.strip(),
            'Fresh output checkout has unrelated changes')
    bundle = pipeline.root / 'output.bundle'
    command(['git', 'bundle', 'verify', str(bundle)], pipeline.output)
    command(['git', 'fetch', str(bundle), 'HEAD'], pipeline.output)
    command(['git', 'switch', '--detach', manifest['outputHead']], pipeline.output)
    pipeline.pinned()
    pipeline.save()
    if pipeline.state['publish']:
        observed = remote_head(pipeline.output, pipeline.state['branch'])
        pending = pipeline.state.get('publicationIntent', {}).get('commit')
        require(observed == pipeline.state['expectedRemote'] or (pending and observed == pending),
                f'Output branch {pipeline.state["branch"]} differs from this checkpoint: '
                f'expected={pipeline.state["expectedRemote"]}, observed={observed}, '
                f'pending={pending}. Check checkpoint selection and other branch writers; '
                'refuse to overwrite it')
    log('checkpoints.restore', f'Restored {pipeline.state["runId"]}; completed commits: {len(pipeline.state["publications"])}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['locate', 'seal', 'restore'])
    args = parser.parse_args()
    if args.command == 'locate':
        locate()
    else:
        globals()[args.command](Pipeline())


if __name__ == '__main__':
    main()
