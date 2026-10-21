"""Prune superseded checkpoints only after a confirmed replacement upload."""
import os
import re
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import require
from ci.progress import log
from ci.runtime import http_json

KEEP = 2
CALLERS = ('stefanospetsios/uniq2me-air', 'stefanospetsios/uniq2me-dora')


def chronology(artifact):
    created = datetime.fromisoformat(artifact['created_at'].replace('Z', '+00:00'))
    require(created.tzinfo is not None, 'Artifact creation time needs a timezone')
    require(type(artifact['id']) is int and artifact['id'] > 0, 'Invalid checkpoint artifact ID')
    return created, artifact['id']


def prune(repository, run_id, uploaded_id, uploaded_digest, token, *, api_url='https://api.github.com'):
    require(repository in CALLERS, 'Checkpoint retention is restricted to the AIR and DORA callers')
    require(re.fullmatch(r'[1-9][0-9]*', str(run_id)) is not None, 'Invalid workflow run ID')
    require(re.fullmatch(r'[1-9][0-9]*', str(uploaded_id)) is not None, 'A successful checkpoint upload ID is required')
    checksum = uploaded_digest.removeprefix('sha256:')
    require(re.fullmatch('[a-f0-9]{64}', checksum) is not None, 'A successful checkpoint upload digest is required')
    require(bool(token), 'Checkpoint retention needs the caller GitHub token')
    run_id, uploaded_id = int(run_id), int(uploaded_id)
    prefix = f'uniq2me-checkpoint-{run_id}-'
    base = api_url.rstrip('/') + '/repos/' + repository
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28'}

    def api(path, method='GET'):
        return http_json(base + path, method=method, timeout=30, headers=headers)

    def validate(artifact):
        require(isinstance(artifact, dict) and isinstance(artifact.get('name'), str) and
                artifact['name'].startswith(prefix),
                'Artifact is not a checkpoint of this workflow run')
        require(isinstance(artifact.get('workflow_run'), dict) and artifact['workflow_run'].get('id') == run_id,
                'Artifact belongs to another workflow run')
        require(type(artifact.get('expired')) is bool, 'Checkpoint expiry status is unavailable')
        chronology(artifact)

    uploaded = api(f'/actions/artifacts/{uploaded_id}')
    validate(uploaded)
    require(uploaded['id'] == uploaded_id and not uploaded.get('expired'), 'New checkpoint is unavailable')
    require(uploaded.get('digest') == 'sha256:' + checksum, 'Uploaded checkpoint digest does not match GitHub metadata')
    rows, page = [], 1
    while True:
        response = api(f'/actions/runs/{run_id}/artifacts?per_page=100&page={page}')
        require(isinstance(response, dict) and isinstance(response.get('artifacts'), list),
                'GitHub did not return an artifact inventory')
        rows.extend(response['artifacts'])
        if len(response['artifacts']) < 100:
            break
        page += 1
    require(all(isinstance(a, dict) and isinstance(a.get('name'), str) for a in rows),
            'Invalid artifact inventory entries')
    checkpoints = [a for a in rows if a['name'].startswith(prefix)]
    for artifact in checkpoints:
        validate(artifact)
    require(len({a['id'] for a in checkpoints}) == len(checkpoints), 'Artifact inventory changed while paginating')
    current = next((a for a in checkpoints if a['id'] == uploaded_id), None)
    require(current is not None and not current.get('expired') and
            all(current.get(key) == uploaded.get(key) for key in ('name', 'digest', 'created_at')),
            'New checkpoint is not yet consistently visible in the run inventory')
    newest = sorted((a for a in checkpoints if not a.get('expired')), key=chronology, reverse=True)[:KEEP]
    keep_ids = {a['id'] for a in newest}
    if uploaded_id not in keep_ids:
        return {'status': 'deferred', 'reason': 'Newer checkpoints superseded this upload; leave cleanup to their jobs',
                'kept': sorted(keep_ids), 'deleted': []}
    # The authenticated run inventory must expose usable, hashed recovery
    # points. Avoid redundant per-artifact GETs on every one of the 256 SFs.
    # Artifact IDs and ZIP contents are immutable after successful v4 uploads.
    for artifact in newest:
        require(isinstance(artifact.get('digest'), str) and
                re.fullmatch('sha256:[a-f0-9]{64}', artifact['digest']) is not None,
                'A retained checkpoint has no verifiable digest')
    deleted = []
    for artifact in sorted(checkpoints, key=chronology):
        if artifact['id'] in keep_ids:
            continue
        started = time.monotonic()
        try:
            api(f'/actions/artifacts/{artifact["id"]}', method='DELETE')
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        deleted.append(artifact['id'])
        # DELETE calls carry higher secondary-rate-limit weight than GET calls.
        time.sleep(max(0, 0.85 - (time.monotonic() - started)))
    return {'status': 'complete', 'kept': sorted(keep_ids), 'deleted': deleted}


def main():
    try:
        result = prune(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GITHUB_RUN_ID', ''),
            os.environ.get('CHECKPOINT_ARTIFACT_ID', ''), os.environ.get('CHECKPOINT_ARTIFACT_DIGEST', ''),
            os.environ.get('GITHUB_TOKEN', ''), api_url=os.environ.get('GITHUB_API_URL', 'https://api.github.com'))
    except urllib.error.HTTPError as error:
        detail = ('Actions: write is required; older read-only workflow reruns cannot prune checkpoints.'
                  if error.code == 403 else 'Cleanup can be retried after GitHub recovers.')
        print(f'::warning title=Checkpoint retention::Cleanup incomplete (GitHub HTTP {error.code}). {detail}', flush=True)
        return
    except (OSError, ValueError, KeyError, TypeError):
        # Housekeeping must not invalidate an uploaded recovery point or block
        # publication. Never print tokens, response bodies or request headers.
        print('::warning title=Checkpoint retention::Cleanup skipped because checkpoint metadata or the API '
              'could not be verified. Existing recovery points were left available.', flush=True)
        return
    if result['status'] == 'deferred':
        log('checkpoints.prune', result['reason'])
    else:
        log('checkpoints.prune', f'Kept {len(result["kept"])} latest checkpoints for this run; '
            f'deleted {len(result["deleted"])} superseded checkpoints')
    return result


if __name__ == '__main__':
    main()
