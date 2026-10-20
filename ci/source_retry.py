"""Persist failed source downloads and retry them without model inference or admission."""
import argparse
import copy
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import digest, read, require, write
from ci.rag_store import filters, language
from ci.runtime import utc
from ci.source_research import url_identity


def retry_key(candidate):
    scope = candidate.get('id', '').split('-source-')[0] or None
    return digest([url_identity(candidate['url']), scope])[7:]


def previous_record(outbox, candidate, previous_root=None):
    relative = Path('download-retries') / (retry_key(candidate) + '.json')
    prior = {}
    for root in (previous_root, outbox):
        if root is not None and (Path(root) / relative).exists():
            path = Path(root) / relative
            require(not path.is_symlink() and not path.parent.is_symlink(), 'Retry queue must not contain symlinks')
            prior = read(path)
    return relative, prior


def record_failure(outbox, candidate, reason, context, previous_root=None):
    key = retry_key(candidate)
    relative, prior = previous_record(outbox, candidate, previous_root)
    now = utc()
    scope = candidate.get('id', '').split('-source-')[0] or None
    record = {'schemaVersion': 1, 'recordType': 'source-download-retry', 'id': key,
        'cachedAt': now, 'originRunId': prior.get('originRunId', context.get('runId')),
        'sourceUrl': candidate['url'], 'candidate': copy.deepcopy(candidate),
        'language': language(candidate.get('language')), 'scopeId': scope,
        'status': 'pending-download', 'firstFailedAt': prior.get('firstFailedAt', now),
        'firstFailureContext': copy.deepcopy(prior.get('firstFailureContext', prior.get('foundIn', context))),
        'lastAttemptAt': now, 'attempts': prior.get('attempts', 0) + 1,
        'lastError': reason, 'foundIn': copy.deepcopy(context),
        'retryOnNextRun': True, 'originalText': None, 'evidenceAdmitted': False,
        'filters': filters([candidate.get('language')], [scope], [context.get('phase')],
                           'pending-download', candidate.get('publisher'), candidate['url'])}
    write(Path(outbox) / relative, record)
    return record


def record_success(outbox, candidate, snapshot, context, previous_root=None):
    relative, prior = previous_record(outbox, candidate, previous_root)
    if not prior or prior.get('status') != 'pending-download':
        return
    prior.update(status='downloaded-awaiting-evidence', cachedAt=utc(), lastAttemptAt=utc(),
        attempts=prior['attempts'] + 1, lastError=None, foundIn=copy.deepcopy(context),
        retryOnNextRun=False, byteDigest=snapshot['byteDigest'], textDigest=snapshot['textDigest'],
        evidenceAdmitted=False)
    prior['filters']['status'] = prior['status']
    write(Path(outbox) / relative, prior)


def retry_downloads(rag_root, work_dir, *, scope=None, limit=20, timeout_seconds=60,
                    run_id='manual-source-retry', controller_commit='local'):
    from ci.source_cache import SourceCache, managed_files
    require(1 <= limit <= 100 and 0 < timeout_seconds <= 300, 'Invalid retry limit or timeout')
    rag_root, work_dir = Path(rag_root), Path(work_dir)
    # Validate shared managed data before reading a queue or writing local results.
    manifest = managed_files(rag_root)
    pipeline = SimpleNamespace(root=work_dir, save=lambda: None, state={
        'runId': run_id, 'controllerCommit': controller_commit, 'ragMode': 'repo-web',
        'sourceRetryPhase': 'retry-downloads',
        'sourceCache': {'mode': 'refresh', 'maxAgeDays': 0}})
    cache = SourceCache(pipeline)
    cache.imported = rag_root
    context = {'runId': run_id, 'controllerCommit': controller_commit, 'phase': 'retry-downloads'}
    results, pending = [], []
    for relative in sorted(manifest):
        if not relative.startswith('download-retries/'):
            continue
        queued_path = cache.outbox / relative
        queued = read(queued_path if queued_path.exists() else rag_root / relative)
        if queued.get('status') != 'pending-download' or scope and queued.get('scopeId') != scope:
            continue
        pending.append((relative, queued))
    # Failed URLs move behind older pending work, so a permanent outage cannot
    # monopolize the daily batch limit.
    for relative, queued in sorted(pending, key=lambda row: (row[1].get('lastAttemptAt', ''), row[0])):
        queued_path = cache.outbox / relative
        candidate = queued['candidate']
        require(relative == 'download-retries/' + retry_key(candidate) + '.json', 'Retry queue identity changed')
        try:
            cache.fetch_snapshot(candidate, work_dir / 'retry-snapshots', time.monotonic() + timeout_seconds)
        except (OSError, TimeoutError, ValueError) as problem:
            # Fetch records transport failures itself; preserve extraction/URL failures too.
            if not queued_path.exists() or read(queued_path)['attempts'] <= queued['attempts']:
                record_failure(cache.outbox, candidate, str(problem), context, previous_root=rag_root)
            results.append({'id': queued['id'], 'url': candidate['url'], 'status': 'pending-download', 'reason': str(problem)})
        else:
            completed = read(queued_path)
            results.append({'id': queued['id'], 'url': candidate['url'], 'status': completed['status']})
        if len(results) >= limit:
            break
    report = {'recordType': 'source-download-retry-report', 'runId': run_id,
        'completedAt': utc(), 'results': results,
        'downloaded': sum(r['status'] == 'downloaded-awaiting-evidence' for r in results),
        'pending': sum(r['status'] == 'pending-download' for r in results), 'evidenceAdmitted': False}
    write(work_dir / 'source-retry-report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rag-root', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--scope', default='all', choices=['all', *'0123456789ABCDEF', 'legal'])
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--timeout-seconds', type=float, default=60)
    parser.add_argument('--publish', action='store_true', help='CI only: publish retry results to shared RAG main')
    args = parser.parse_args()
    if args.publish:
        require(bool(os.environ.get('SOURCE_CACHE_TOKEN')), 'RAG retry publication requires SOURCE_CACHE_TOKEN')
    run_id = os.environ.get('GITHUB_RUN_ID', 'manual-source-retry')
    report = retry_downloads(args.rag_root, args.work_dir, scope=None if args.scope == 'all' else args.scope,
        limit=args.limit, timeout_seconds=args.timeout_seconds,
        run_id=run_id, controller_commit=os.environ.get('GITHUB_SHA', 'local'))
    if args.publish:
        from ci.source_cache import CACHE_REPOSITORY, publish_files
        report['publication'] = publish_files(args.work_dir / 'source-cache-outbox', CACHE_REPOSITORY,
            args.work_dir, os.environ['SOURCE_CACHE_TOKEN'], run_id)
        write(args.work_dir / 'source-retry-report.json', report)
    summary = f"Source retries: {report['downloaded']} downloaded; {report['pending']} still pending. No evidence admission or catalog changes."
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(summary + '\n')


if __name__ == '__main__':
    main()
