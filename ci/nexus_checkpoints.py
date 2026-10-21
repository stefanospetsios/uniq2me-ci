"""Verified Raw checkpoints. Credentials are supplied only to storage steps."""
import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import byte_digest, file_manifest, read, require


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Nexus:
    def __init__(self):
        raw = os.environ.get('CHECKPOINT_URL', '').strip()
        parsed = urllib.parse.urlsplit(raw)
        require(parsed.scheme == 'https' and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment,
                'CHECKPOINT_URL must be a canonical HTTPS Raw repository URL')
        match = re.fullmatch(r'(.*)/repository/([A-Za-z0-9_.-]+)/?', parsed.path)
        require(match is not None, 'CHECKPOINT_URL must end with /repository/<name>/')
        user, password = os.environ.get('CHECKPOINT_USER', 'checkpoint'), os.environ.get('CHECKPOINT_PASS', '')
        require(user and ':' not in user and password, 'Nexus requires CHECKPOINT_USER and CHECKPOINT_PASS')
        self.url = raw.rstrip('/') + '/'
        self.repository = match[2]
        self.api = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
                                           match[1] + '/service/rest/v1/', '', ''))
        self.headers = {'Authorization': 'Basic ' + base64.b64encode((user + ':' + password).encode()).decode()}
        self.opener = urllib.request.build_opener(NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, target, method='GET', data=None):
        # No redirects, exception bodies or URLs: neither auth nor private routing
        # information should reach logs. PUT needs a binary content type on Raw.
        headers = dict(self.headers)
        if data is not None:
            headers['Content-Type'] = 'application/octet-stream'
        for attempt in range(3):
            try:
                with self.opener.open(urllib.request.Request(target, method=method,
                        data=data, headers=headers), timeout=120) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code == 404 and method == 'DELETE':
                    return b''
                if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                    raise ValueError(f'Nexus {method} failed: HTTP {error.code}') from None
            except (OSError, urllib.error.URLError):
                if attempt == 2:
                    raise ValueError(f'Nexus {method} failed: connection unavailable') from None
            time.sleep(2 ** attempt)

    def get(self, path):
        return self.request(self.url + urllib.parse.quote(path, safe='/'))

    def put(self, path, data):
        return self.request(self.url + urllib.parse.quote(path, safe='/'), 'PUT', data)

    def assets(self):
        rows, token, seen = [], None, set()
        while True:
            query = {'repository': self.repository}
            if token:
                query['continuationToken'] = token
            page = json.loads(self.request(self.api + 'assets?' + urllib.parse.urlencode(query)))
            for row in page['items']:
                # Nexus versions differ in whether Raw paths begin with '/'.
                rows.append({**row, 'path': row['path'].lstrip('/')})
            token = page.get('continuationToken')
            if not token:
                return rows
            require(token not in seen, 'Nexus asset pagination repeated a token')
            seen.add(token)

    def delete(self, row):
        self.request(self.api + 'assets/' + urllib.parse.quote(row['id'], safe=''), 'DELETE')


def binding():
    repo, run = os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_RUN_ID']
    require(repo in ('stefanospetsios/uniq2me-air', 'stefanospetsios/uniq2me-dora'),
            'Nexus checkpoints are restricted to the owner caller repositories')
    require(bool(re.fullmatch(r'[1-9][0-9]*', run)), 'Invalid workflow run ID')
    return repo, run


def prefix(repo, run):
    require(bool(re.fullmatch(r'[1-9][0-9]*', str(run))), 'Invalid checkpoint source run ID')
    return f'{repo}/{run}/'


def records(store, repo, run, attempt=None):
    assets = {row['path']: row for row in store.assets()}
    result = []
    for path in sorted(assets):
        if not re.fullmatch(re.escape(prefix(repo, run)) + r'[0-9]{20}-[a-f0-9]{32}\.json', path):
            continue
        marker = json.loads(store.get(path))
        require(marker.get('recordType') == 'nexus-checkpoint'
                and marker.get('workflowRepository') == repo and marker.get('workflowRunId') == str(run)
                and marker.get('zipPath') == path[:-5] + '.zip'
                and re.fullmatch(r'[a-f0-9]{64}', marker.get('sha256', ''))
                and type(marker.get('size')) is int and marker['size'] > 0
                and re.fullmatch(r'sha256:[a-f0-9]{64}', marker.get('manifestDigest', ''))
                and re.fullmatch(r'[1-9][0-9]*', marker.get('workflowAttempt', ''))
                and re.fullmatch(r'uniq2me-checkpoint-' + re.escape(str(run)) + r'-[A-Za-z0-9_.-]+', marker.get('name', '')),
                'Invalid Nexus checkpoint commit marker')
        archive = assets.get(marker['zipPath'])
        require(archive is not None, 'Nexus committed checkpoint archive is missing')
        require(archive.get('fileSize') == marker['size'], 'Nexus checkpoint archive size changed')
        require(archive.get('checksum', {}).get('sha256') == marker['sha256'],
                'Nexus checkpoint archive checksum metadata changed')
        if attempt is None or marker.get('workflowAttempt') == str(attempt):
            result.append({**marker, 'markerPath': path, 'markerAsset': assets[path], 'zipAsset': archive})
    return sorted(result, key=lambda r: r['markerPath'], reverse=True)


def validate_tree(root, repo, run):
    manifest = read(root / 'checkpoint-manifest.json')
    require(manifest.get('recordType') == 'ci-checkpoint'
            and manifest.get('workflowRepository') == repo and manifest.get('workflowRunId') == str(run),
            'Checkpoint repository/run binding changed')
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'Checkpoint symlinks are forbidden')
    actual = file_manifest(root)
    actual.pop('checkpoint-manifest.json', None)
    require(actual == manifest['files'], 'Checkpoint file hashes do not match')
    return manifest


def upload(store, root, name):
    repo, run = binding()
    require(bool(re.fullmatch(r'uniq2me-checkpoint-' + run + r'-[A-Za-z0-9_.-]+', name)),
            'Checkpoint name must belong to this workflow run')
    manifest = validate_tree(root, repo, run)
    # Never archive a file inside its own source tree.
    with tempfile.TemporaryDirectory(prefix='checkpoint-package-', dir=root.parent) as temporary:
        archive = Path(temporary) / 'checkpoint.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zipfile_out:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    zipfile_out.write(path, path.relative_to(root).as_posix())
        with zipfile.ZipFile(archive) as saved:
            packed = {name: 'sha256:' + hashlib.sha256(saved.read(name)).hexdigest()
                      for name in saved.namelist()}
            manifest_digest = packed.pop('checkpoint-manifest.json')
            require(packed == manifest['files']
                    and json.loads(saved.read('checkpoint-manifest.json')) == manifest,
                    'Checkpoint changed while packaging; no upload was committed')
        body = archive.read_bytes()
    stem = prefix(repo, run) + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f') + '-' + uuid.uuid4().hex
    checksum = hashlib.sha256(body).hexdigest()
    store.put(stem + '.zip', body)
    require(hashlib.sha256(store.get(stem + '.zip')).hexdigest() == checksum,
            'Nexus uploaded checkpoint checksum does not match')
    # The immutable marker commits the independently verified archive. Interrupted
    # uploads have no marker and can never displace a restorable checkpoint.
    marker = {'recordType': 'nexus-checkpoint', 'workflowRepository': repo, 'workflowRunId': run,
        'workflowAttempt': manifest['workflowAttempt'], 'name': name, 'zipPath': stem + '.zip',
        'sha256': checksum, 'size': len(body), 'manifestDigest': manifest_digest}
    store.put(stem + '.json', json.dumps(marker, sort_keys=True).encode())
    require(json.loads(store.get(stem + '.json')) == marker, 'Nexus checkpoint marker verification failed')
    print(f'Nexus checkpoint saved and verified: {name}', flush=True)
    try:
        prune(store, repo, run, stem + '.json')
    except Exception:
        print('::warning::Nexus retention deferred; uploaded checkpoint remains available', flush=True)
    return stem + '.json'


def prune(store, repo, run, uploaded):
    snapshots = records(store, repo, run)
    require(any(row['markerPath'] == uploaded for row in snapshots), 'New checkpoint is absent from Nexus inventory')
    if uploaded not in {row['markerPath'] for row in snapshots[:2]}:
        return  # A newer uploader owns cleanup.
    # records() requires both retained asset sizes and Nexus SHA-256 metadata to
    # match their verified commit markers. Do not re-download large archives on
    # every SF; upload already verified the bytes before committing each marker.
    for row in snapshots[2:]:
        store.delete(row['markerAsset'])
        store.delete(row['zipAsset'])
    print(f'Nexus retention: {min(2, len(snapshots))} checkpoints retained for this run', flush=True)


def download(store, root, marker_path):
    repo, current = binding()
    allowed = [current, os.environ.get('RESUME_RUN_ID', '').strip()]
    selected = next((r for run in allowed if run for r in records(store, repo, run)
                     if r['markerPath'] == marker_path), None)
    require(selected is not None, 'Nexus checkpoint is outside the selected repository/run')
    body = store.get(selected['zipPath'])
    require(len(body) == selected['size'] and hashlib.sha256(body).hexdigest() == selected['sha256'],
            'Nexus downloaded checkpoint checksum does not match')
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='checkpoint-download-', dir=root.parent) as temporary:
        archive = Path(temporary) / 'checkpoint.zip'
        archive.write_bytes(body)
        staged = Path(temporary) / 'state'
        staged.mkdir()
        with zipfile.ZipFile(archive) as zipfile_in:
            seen = set()
            for entry in zipfile_in.infolist():
                name = entry.filename
                path = PurePosixPath(name)
                require(not path.is_absolute() and '..' not in path.parts and '\\' not in name
                        and name == path.as_posix() and name not in seen and not entry.is_dir()
                        and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG),
                        'Unsafe or duplicate checkpoint archive entry')
                seen.add(name)
                zipfile_in.extract(entry, staged)
        require(byte_digest(staged / 'checkpoint-manifest.json') == selected['manifestDigest'],
                'Nexus checkpoint manifest changed')
        manifest = validate_tree(staged, repo, selected['workflowRunId'])
        if os.environ.get('FRESH_START', 'false').lower() == 'true':
            require(manifest['workflowAttempt'] == os.environ['GITHUB_RUN_ATTEMPT'],
                    'Fresh start cannot restore another attempt')
        require(not root.exists() or not any(root.iterdir()), 'Checkpoint destination is not empty')
        if root.exists():
            root.rmdir()
        staged.replace(root)
    print('Nexus checkpoint downloaded; manifest and all file hashes verified', flush=True)


def probe(store):
    repo, run = binding()
    path = prefix(repo, run) + 'probe-' + uuid.uuid4().hex + '.txt'
    body = b'UNIQ2Me checkpoint storage probe\n'
    try:
        store.put(path, body)
        require(store.get(path) == body, 'Nexus preflight upload checksum failed')
        asset = next((row for row in store.assets() if row['path'] == path), None)
        require(asset is not None, 'Nexus preflight cannot discover uploaded files')
    finally:
        asset = next((row for row in store.assets() if row['path'] == path), None)
        if asset:
            store.delete(asset)
    print('Nexus preflight passed: upload, checksum, listing and deletion', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('upload', 'download', 'probe'))
    args = parser.parse_args()
    try:
        store = Nexus()
        root = Path(os.environ.get('RUNNER_TEMP', '/tmp')) / 'uniq2me-run'
        if args.command == 'upload':
            upload(store, root, os.environ['CHECKPOINT_NAME'])
        elif args.command == 'download':
            download(store, root, os.environ['CHECKPOINT_MARKER'])
        else:
            probe(store)
    except Exception as error:
        # A credential, private URL or provider response can occur in arbitrary
        # exception text. Only our deliberate ValueError messages are public.
        message = str(error) if type(error) is ValueError and not isinstance(error, json.JSONDecodeError) else type(error).__name__
        print('::error::Checkpoint storage failed: ' + message, flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
