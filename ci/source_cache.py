"""Cross-run reference snapshots; only CI publishes the managed main-branch cache."""
import argparse
import base64
import copy
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import byte_digest, digest, read, refs, require, text, write
from ci.progress import log, stage_context
from ci.runtime import utc
from ci.source_research import select_sources, url_identity, validate_scope_snapshots
from ci.rag_store import REPOSITORY, BRANCH, DIRECTORY, document_record, filters, write_passages

CACHE_DIRECTORY = DIRECTORY
CACHE_REPOSITORY = 'https://github.com/' + REPOSITORY + '.git'
MANAGED = re.compile(r'(?:documents|scopes|passages|searches|download-retries)/[a-f0-9]{64}\.json|snapshots/source-[a-f0-9]{64}\.(?:bin|txt)')
SNAPSHOT_FIELDS = ('byteDigest', 'textDigest', 'finalUrl', 'retrievedAt', 'contentType', 'snapshotPath', 'textPath')
METADATA_FIELDS = ('id', 'type', 'title', 'publisher', 'url', 'language', 'jurisdiction',
                   'applicability', 'publicationDate', 'effectiveDate', 'redistribution', 'sourceStatus',
                   'passages', 'quotationMatchPolicy', 'typographyNormalizedPassages')


def configuration():
    mode = os.environ.get('SOURCE_CACHE_MODE', '') or 'reuse'
    require(mode in ('reuse', 'refresh', 'off'), 'SOURCE_CACHE_MODE must be reuse, refresh or off')
    age = float(os.environ.get('SOURCE_CACHE_MAX_AGE_DAYS', '') or '0')
    require(math.isfinite(age) and 0 <= age <= 3650,
            'SOURCE_CACHE_MAX_AGE_DAYS must be between 0 (no expiry) and 3650')
    return {'mode': mode, 'maxAgeDays': age, 'documentsReused': 0, 'documentsFetched': 0,
            'scopesReused': 0, 'publication': {'status': 'not-run'}}


def managed_files(root):
    root = Path(root)
    require(not root.is_symlink(), 'Source cache root must not be a symlink')
    if not root.exists():
        return {}
    files = {}
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'Source cache must not contain symlinks')
        relative = path.relative_to(root).as_posix()
        if path.is_file() and MANAGED.fullmatch(relative):
            require(path.stat().st_size <= 25 * 1024 * 1024, 'Source cache file exceeds 25 MB')
            files[relative] = byte_digest(path)
    return files


def copy_files(source, destination, manifest):
    for relative, checksum in manifest.items():
        require(bool(MANAGED.fullmatch(relative)), 'Unsafe source cache path: ' + relative)
        src, dst = Path(source) / relative, Path(destination) / relative
        require(not src.is_symlink() and src.is_file() and byte_digest(src) == checksum,
                'Source cache hash mismatch: ' + relative)
        require(src.resolve().is_relative_to(Path(source).resolve()), 'Source cache path escapes its root')
        # Refuse a symlink introduced on main rather than following it into code.
        require(not any(p.is_symlink() for p in (dst, *dst.parents)
                        if p.is_relative_to(Path(destination))), 'Source cache destination is a symlink')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def snapshot_names(source):
    require(bool(re.fullmatch(r'sha256:[a-f0-9]{64}', source['byteDigest'])), 'Invalid source digest')
    require(bool(re.fullmatch(r'sha256:[a-f0-9]{64}', source['textDigest'])), 'Invalid extracted text digest')
    require(source['snapshotPath'] == 'source-' + source['byteDigest'][7:] + '.bin' and
            source['textPath'] == 'source-' + source['textDigest'][7:] + '.txt',
            'Invalid content-addressed snapshot paths')


def export_snapshots(archive, destination, manifest):
    """Export an archive regardless of its directory name, retaining content hashes."""
    archive, destination = Path(archive), Path(destination)
    require(not archive.is_symlink(), 'Snapshot archive is a symlink')
    for name, checksum in manifest.items():
        require(name.startswith('snapshots/') and bool(MANAGED.fullmatch(name)), 'Invalid snapshot export path')
        source, target = archive / Path(name).name, destination / name
        require(not source.is_symlink() and byte_digest(source) == checksum, 'Snapshot export hash mismatch')
        require(not any(p.is_symlink() for p in (target, *target.parents)
                        if p.is_relative_to(destination)), 'Snapshot export destination is a symlink')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def hydrate(pipeline):
    config = pipeline.state.get('sourceCache', {})
    if (config.get('mode', 'off') == 'off' or pipeline.state.get('dryRun') or
            pipeline.state.get('ragMode') not in ('repo-web', 'repo-only')):
        return
    if config.get('importedCommit'):
        log('source_cache.hydrate', 'Keeping this run\'s pinned source cache ' + config['importedCommit'])
        return
    checkout = pipeline.workspace / 'uniq2me-rag'
    origin = git(['remote', 'get-url', 'origin'], checkout).stdout.strip()
    require(origin in (CACHE_REPOSITORY, CACHE_REPOSITORY.removesuffix('.git'),
                       'git@github.com:' + REPOSITORY + '.git'), 'Unexpected shared RAG origin')
    revision = git(['rev-parse', 'HEAD'], checkout).stdout.strip()
    source = checkout / CACHE_DIRECTORY
    manifest = managed_files(source)
    copy_files(source, pipeline.root / 'source-cache-import', manifest)
    config.update(repository=REPOSITORY, branch=BRANCH, path=DIRECTORY,
                  importedCommit=revision, importedManifest=manifest)
    pipeline.save()
    log('source_cache.hydrate', f'Pinned {len(manifest)} cache files from {REPOSITORY}/main at {revision}')


class SourceCache:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.config = pipeline.state.get('sourceCache', {'mode': 'off'})
        self.imported = pipeline.root / 'source-cache-import'
        self.outbox = pipeline.root / 'source-cache-outbox'
        self.scope_key = None

    @property
    def enabled(self):
        return self.config['mode'] != 'off'

    def increment(self, field):
        self.config[field] = self.config.get(field, 0) + 1
        self.pipeline.save()

    def current(self, timestamp):
        date = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        require(date.tzinfo is not None, 'Cache retrieval time must have a UTC offset')
        age = (datetime.now(timezone.utc) - date).total_seconds()
        return age >= -60 and (self.config.get('maxAgeDays', 0) == 0 or
                              age <= self.config.get('maxAgeDays', 0) * 86400)

    def scope_fingerprint(self, scope, area):
        trusted = self.pipeline.trusted
        policies = ('ci/source_research.py', 'ci/source_cache.py', 'ci/source_passages.py', 'ci/source_documents.py',
                    'ci/source_observations.py',
                    'agent-rules/agents/source-researcher.md',
                    'agent-rules/rules/02-mcp-source-discovery-and-rag.md')
        self.scope_key = digest({'schemaVersion': 1, 'scope': scope,
            'topic': {k: v for k, v in area.items() if k.startswith('key')},
            'catalogProfile': self.pipeline.state.get('catalogProfile'),
            **({'catalogBrief': self.pipeline.state['refinementPlan']['catalogBrief']}
               if self.pipeline.state.get('refinementPlan', {}).get('catalogBrief') else {}),
            'methodology': byte_digest(self.pipeline.work / 'inputs/main.tex'),
            'policies': {name: byte_digest(trusted / name) for name in policies},
            'modelDigest': self.pipeline.state['model'].get('digest'),
            'context': self.pipeline.state['model'].get('effectiveContext'),
            'sourceHostPolicy': 'any-public-https-host-v1'})[7:]
        return self.scope_key

    def record(self, relative):
        # Locally fetched data wins during a retry, including refresh runs.
        for root in (self.outbox, self.imported):
            path = root / relative
            if root == self.imported and self.config['mode'] != 'reuse':
                continue
            if path.exists():
                try:
                    if root == self.imported:
                        require(self.config.get('importedManifest', {}).get(relative) == byte_digest(path),
                                'Pinned cache record changed')
                    record = read(path)
                    require(record['schemaVersion'] == 1, 'Unsupported source cache schema')
                    require(self.current(record['cachedAt']), 'Source cache entry expired')
                    for name, checksum in record['files'].items():
                        require(name.startswith('snapshots/') and bool(MANAGED.fullmatch(name)),
                                'Cache records may reference snapshot files only')
                        require(not (root / name).is_symlink() and byte_digest(root / name) == checksum,
                                'Cached snapshot bytes changed: ' + name)
                        if root == self.imported:
                            require(self.config['importedManifest'].get(name) == checksum, 'Snapshot differs from pinned import')
                    return root, record
                except (ValueError, KeyError, TypeError, OSError) as problem:
                    log('source_cache.read', f'Ignore unusable {relative}: {problem}')
        return None, None

    def remember_document(self, source, archive):
        if not self.enabled:
            return
        origin = source.get('cacheReuse', {}).get('originRunId', self.pipeline.state['runId'])
        web_rechecked = source.get('cacheReuse', {}).get('webRecheckedThisRun', True)
        source = {key: copy.deepcopy(source[key]) for key in (*METADATA_FIELDS, *SNAPSHOT_FIELDS) if key in source}
        snapshot_names(source)
        raw, extracted = source['snapshotPath'], source['textPath']
        files = {'snapshots/' + name: byte_digest(Path(archive) / name) for name in (raw, extracted)}
        require(files['snapshots/' + raw] == source['byteDigest'], 'Source byte digest differs from snapshot')
        require(files['snapshots/' + extracted] == source['textDigest'], 'Extracted text digest differs from snapshot')
        export_snapshots(archive, self.outbox, files)
        key = digest(url_identity(source['url']))[7:]
        content = (Path(archive)/extracted).read_text(encoding='utf-8')
        if source.get('sourceStatus') == 'fetched-and-passage-checked':
            if source.get('passages'):
                from ci.pipeline import check_source_passages
                source = check_source_passages(source, content)
            else:
                source['sourceStatus'] = 'fetched-awaiting-passage-check'
        context = {'runId':self.pipeline.state['runId'], 'sourceOriginRunId':origin,
                   'controllerCommit':self.pipeline.state['controllerCommit'], 'recordedAt':utc(),
                   'webRecheckedThisRun':web_rechecked,
                   'phase':self.pipeline.state.get('sourceRetryPhase') or os.environ.get('STAGE'),
                   'frameworkId':self.pipeline.state.get('catalogProfile', {}).get('frameworkId'),
                   'task':self.pipeline.state.get('activeTask')}
        record = {'schemaVersion': 1,
            'cachedAt': source['retrievedAt'], 'originRunId': origin,
            'source': source, 'files': files}
        write(self.outbox / 'documents' / (key + '.json'), document_record(record, content, context))
        write_passages(self.outbox, source, content, context, utc())

    def fetch_snapshot(self, candidate, archive, deadline):
        from ci.pipeline import fetch_source_snapshot, official_url
        from ci.source_documents import ensure_source_available
        from ci.source_research import dora_core_source, full_dora_text
        official_url(candidate['url'])
        if self.enabled:
            root, record = self.record('documents/' + digest(url_identity(candidate['url']))[7:] + '.json')
            if record:
                saved = record['source']
                try:
                    snapshot_names(saved)
                    official_url(saved['finalUrl'])
                    require(url_identity(saved['url']) == url_identity(candidate['url']), 'Cached URL changed')
                    require(record['files'].get('snapshots/' + saved['snapshotPath']) == saved['byteDigest'],
                            'Cached byte digest does not match document')
                    require(record['files'].get('snapshots/' + saved['textPath']) == saved['textDigest'],
                            'Cached text digest does not match document')
                    # Cache manifests use snapshots/, while the caller may use
                    # another archive directory (for example the preflight pilot).
                    archive = Path(archive)
                    archive.mkdir(parents=True, exist_ok=True)
                    require(not archive.is_symlink(), 'Snapshot archive is a symlink')
                    for name, checksum in record['files'].items():
                        target = archive / Path(name).name
                        require(not target.is_symlink(), 'Snapshot destination is a symlink')
                        shutil.copyfile(root/name, target)
                        require(byte_digest(target) == checksum, 'Copied snapshot checksum changed')
                    value = copy.deepcopy(candidate)
                    value.pop('passages', None)
                    value.update({key: saved[key] for key in SNAPSHOT_FIELDS})
                    ensure_source_available((archive / saved['textPath']).read_text(encoding='utf-8'))
                    require(not dora_core_source(candidate) or (dora_core_source(value) and
                            full_dora_text((Path(archive) / saved['textPath']).read_text())),
                            'Cached DORA source is incomplete or an unavailable-document page')
                    value.update(sourceStatus='fetched-awaiting-passage-check', cacheReuse={
                        'originRunId': record['originRunId'],
                        'cacheCommit': self.config.get('importedCommit') if root == self.imported else None,
                        'reusedAt': utc(), 'webRecheckedThisRun': record['originRunId'] == self.pipeline.state['runId']})
                    self.increment('documentsReused')
                    log('source_cache.fetch_snapshot', 'Reused document without downloading: ' + candidate['url'])
                    return value
                except (ValueError, KeyError, TypeError, OSError) as problem:
                    log('source_cache.fetch_snapshot', f'Cached document unavailable: {problem}')
        try:
            value = fetch_source_snapshot(candidate, archive, deadline)
            if dora_core_source(candidate) and (not dora_core_source(value) or
                    not full_dora_text((Path(archive) / value['textPath']).read_text())):
                raise OSError('Primary DORA document is unavailable or incomplete; retry the full official text')
        except (OSError, TimeoutError) as problem:
            from ci.source_retry import record_failure
            record_failure(self.outbox, candidate, str(problem), {
                'runId': self.pipeline.state['runId'],
                'controllerCommit': self.pipeline.state['controllerCommit'],
                'phase': self.pipeline.state.get('sourceRetryPhase') or os.environ.get('STAGE', 'prepare')},
                previous_root=self.imported)
            raise
        if self.enabled:
            self.remember_document(value, archive)
            self.increment('documentsFetched')
        from ci.source_retry import record_success
        record_success(self.outbox, candidate, value, {
            'runId': self.pipeline.state['runId'], 'controllerCommit': self.pipeline.state['controllerCommit'],
            'phase': self.pipeline.state.get('sourceRetryPhase') or os.environ.get('STAGE', 'prepare')},
            previous_root=self.imported)
        return value

    def remember_scope(self, scope, fingerprint, admitted):
        if not self.enabled:
            return
        archive = self.pipeline.work / 'frozen/snapshots'
        for source in admitted['sources'].values():
            # This also retains fetched documents if passage extraction later fails.
            self.remember_document(source, archive)
        files = {'snapshots/' + source[field]: byte_digest(archive / source[field])
                 for source in admitted['sources'].values() for field in ('snapshotPath', 'textPath')}
        corpus = copy.deepcopy(admitted)
        # Retain provenance for included documents, not unrelated search URLs.
        corpus['observedResultUrls'] = sorted(set(admitted['observedResultUrls']) & {
            url_identity(source['url']) for source in admitted['sources'].values()})
        write(self.outbox / 'scopes' / (fingerprint + '.json'), {'schemaVersion': 1,
            'recordType':'source-scope',
            'cachedAt': utc(), 'scopeId': scope, 'fingerprint': fingerprint,
            'originRunId': self.pipeline.state['runId'], 'controllerCommit': self.pipeline.state['controllerCommit'],
            'filters':filters([s.get('language') for s in corpus['sources'].values()], [scope],
                              [os.environ.get('STAGE')], 'scope-admitted',
                              framework=self.pipeline.state.get('catalogProfile', {}).get('frameworkId')),
            'corpus': corpus, 'files': files})

    def reuse_scope(self, scope, fingerprint):
        from ci.source_documents import ensure_source_available
        if not self.enabled or self.pipeline.state.get('freshStart'):
            return None
        root, record = self.record('scopes/' + fingerprint + '.json')
        if not record:
            return None
        from ci.pipeline import check_source_passages, official_url
        try:
            require(record['scopeId'] == scope and record['fingerprint'] == fingerprint, 'Cache scope identity changed')
            corpus = copy.deepcopy(record['corpus'])
            require(not self.pipeline.state.get('catalogProfile') or corpus.get('frameworkId') ==
                    self.pipeline.state['catalogProfile']['frameworkId'], 'Cached scope belongs to another framework')
            sources = corpus['sources']
            require(corpus['admissionVersion'] == 2, 'Cache scope has no supported admission record')
            selected, excluded = select_sources(scope, list(sources.values()),
                set(corpus['observedResultUrls']), [], preserve_ids=True,
                framework=self.pipeline.state.get('catalogProfile', {}).get('frameworkId'))
            require(not excluded and {s['id'] for s in selected} == set(sources), 'Cache changed admitted source IDs')
            queries = corpus['queries']
            require(all(any(q['toolId'].startswith(group + '_') and q.get('outputDigest')
                    for q in queries) for group in ('searxng', 'websearch')), 'Missing original search provenance')
            for source in sources.values():
                snapshot_names(source)
                require(source['id'] in sources and sources[source['id']] == source, 'Cached source identity changed')
                official_url(source['url'])
                official_url(source['finalUrl'])
                require(self.current(source['retrievedAt']), 'Cached source expired')
                require(record['files'].get('snapshots/' + source['snapshotPath']) == source['byteDigest'],
                        'Cached source digest mismatch')
                require(record['files'].get('snapshots/' + source['textPath']) == source['textDigest'],
                        'Cached extracted text digest mismatch')
                _, latest = self.record('documents/' + digest(url_identity(source['url']))[7:] + '.json')
                require(not latest or latest['source']['byteDigest'] == source['byteDigest'],
                        'Document changed after this cached scope was admitted')
                content = (root / 'snapshots' / source['textPath']).read_text(encoding='utf-8')
                ensure_source_available(content)
                check_source_passages(source, content)
            validate_scope_snapshots(scope, sources, root / 'snapshots',
                self.pipeline.state.get('catalogProfile', {}).get('frameworkId'))
            if scope == 'legal' and self.pipeline.state.get('refinementPlan', {}).get('maskRequest'):
                from ci.source_research import validate_requested_authorities
                validate_requested_authorities(scope, sources, self.pipeline.source_area(scope))
            concerns = corpus['concerns']
            require(isinstance(concerns, list) and (bool(concerns) if scope != 'legal' else not concerns),
                    'Cached concern inventory is missing or has wrong scope')
            require(len({c['id'] for c in concerns}) == len(concerns), 'Duplicate cached concern ID')
            for concern in concerns:
                require(concern['sfsId'] == scope and concern['id'].startswith(scope + '-concern-'), 'Wrong cached concern scope')
                text(concern['description'], 'concern.description')
                refs(concern['sourceRefs'], sources)
            copy_files(root, self.pipeline.work / 'frozen', record['files'])
            reuse = {'originRunId': record['originRunId'],
                     'cacheCommit': self.config.get('importedCommit') if root == self.imported else None,
                     'reusedAt': utc(), 'executedThisRun': record['originRunId'] == self.pipeline.state['runId']}
            for source in sources.values():
                origin = source.get('cacheReuse', {}).get('originRunId', record['originRunId'])
                source['cacheReuse'] = {**reuse, 'originRunId': origin, 'scopeOriginRunId': record['originRunId'],
                                       'webRecheckedThisRun': origin == self.pipeline.state['runId']}
            for query in queries:
                query['cacheReuse'] = reuse
            corpus['limitations'].append({'scope': scope, 'description':
                'Reused previously fetched, passage-checked documents; original retrieval dates retained. '
                'No update search was performed for this cached scope in this run; use source_cache_mode=refresh to check updates.'})
            self.increment('scopesReused')
            for _ in sources:
                self.increment('documentsReused')
            log('source_cache.reuse_scope', f'Reused verified scope {scope}: {len(sources)} documents; searches skipped')
            return corpus
        except (ValueError, KeyError, TypeError, OSError) as problem:
            log('source_cache.reuse_scope', f'Scope {scope} needs fresh research: {problem}')
            return None


def redact(message, token):
    if token:
        for secret in (token, base64.b64encode(('x-access-token:' + token).encode()).decode()):
            message = message.replace(secret, '[redacted]')
    return message


def git(args, cwd, token='', check=True):
    from ci.git_auth import git_environment
    env = git_environment(token)
    result = subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=180)
    if check and result.returncode:
        raise RuntimeError(f'Source cache git {args[0]} failed: ' + redact(result.stderr.strip(), token))
    return result


def publish_files(outbox, repository, directory, token, run_id):
    """Reapply only managed files to latest main; normal pushes protect other edits."""
    manifest = managed_files(outbox)
    if not manifest:
        return {'status': 'no-new-documents', 'files': 0}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='source-cache-publisher-', dir=directory) as temporary:
        repo = Path(temporary)
        git(['init', '-q'], repo)
        git(['remote', 'add', 'origin', repository], repo)
        for attempt in range(1, 4):
            git(['fetch', '-q', '--depth=1', 'origin', 'refs/heads/main'], repo, token)
            git(['checkout', '-q', '--detach', '-f', 'FETCH_HEAD'], repo)
            target = repo / CACHE_DIRECTORY
            require(not target.is_symlink(), 'Source cache root on main is a symlink')
            # A racing run must not downgrade a document to an older retrieval.
            updates = dict(manifest)
            for name in manifest:
                if not name.endswith('.json') or not (target / name).exists():
                    continue
                require(not (target / name).is_symlink(), 'Source cache record on main is a symlink')
                existing, proposed = read(target / name), read(Path(outbox) / name)
                if existing.get('schemaVersion') == 1 and existing.get('cachedAt', '') > proposed['cachedAt']:
                    updates.pop(name)
                elif (name.startswith('documents/') and existing.get('source', {}).get('passages')
                      and not proposed.get('source', {}).get('passages')
                      and existing['source'].get('byteDigest') == proposed['source'].get('byteDigest')):
                    updates.pop(name)
            for name in list(updates):
                if not name.startswith('scopes/'):
                    continue
                proposed = read(Path(outbox) / name)
                for source in proposed['corpus']['sources'].values():
                    registry = 'documents/' + digest(url_identity(source['url']))[7:] + '.json'
                    latest = target / registry
                    if registry not in updates and latest.exists():
                        doc = read(latest)['source']
                        if doc['byteDigest'] != source['byteDigest']:
                            updates.pop(name)
                            break
            copy_files(outbox, target, updates)
            git(['add', '--', CACHE_DIRECTORY], repo)
            if git(['diff', '--cached', '--quiet'], repo, check=False).returncode == 0:
                return {'status': 'already-published', 'files': 0,
                        'commit': git(['rev-parse', 'HEAD'], repo).stdout.strip()}
            git(['-c', 'user.name=UNIQ2Me source cache', '-c',
                 'user.email=github-actions[bot]@users.noreply.github.com', 'commit', '-qm',
                 f'RAG: cache source documents for {run_id}'], repo)
            commit = git(['rev-parse', 'HEAD'], repo).stdout.strip()
            pushed = git(['push', 'origin', 'HEAD:refs/heads/main'], repo, token, check=False)
            if pushed.returncode == 0:
                return {'status': 'pushed', 'commit': commit, 'files': len(updates)}
            # The fetch/reapply loop safely handles a concurrent code/cache push.
            log('source_cache.publish', f'main push attempt {attempt}/3 did not succeed; retrying from latest main')
        raise RuntimeError('Could not publish shared evidence to uniq2me-rag/main after three attempts. '
                           'Check repository access and main branch protection; configure SOURCE_CACHE_TOKEN for uniq2me-rag. '
                           'Git rejection: ' + redact(pushed.stderr.strip(), token))


def publish(pipeline):
    config = pipeline.state.get('sourceCache', {})
    if (config.get('mode', 'off') == 'off' or pipeline.state.get('dryRun') or
            pipeline.state.get('ragMode') != 'repo-web'):
        return
    token = os.environ.get('SOURCE_CACHE_TOKEN', '')
    require(bool(token), 'Shared RAG publication needs a token with contents:write for uniq2me-rag')
    try:
        result = publish_files(pipeline.root / 'source-cache-outbox', CACHE_REPOSITORY,
                               pipeline.root, token, pipeline.state['runId'])
        config['publication'] = result
        pipeline.save()
        log('source_cache.publish', REPOSITORY + '/main: ' + str(result))
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as problem:
        config['publication'] = {'status': 'failed', 'reason': str(problem)}
        pipeline.save()
        raise


def main():
    from ci.pipeline import Pipeline
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('hydrate', 'publish'))
    args = parser.parse_args()
    with stage_context(os.environ.get('STAGE', 'prepare')):
        globals()[args.command](Pipeline())


if __name__ == '__main__':
    main()
