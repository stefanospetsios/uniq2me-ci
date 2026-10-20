"""SF publication and run-owned history consolidation, separate from inference.

Every push is preceded by a prepared commit and an uploaded checkpoint in the
workflow. Rewriting uses exact leases and only the recorded commits of this run.
"""
import argparse
import copy
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import HEX, byte_digest, digest, file_manifest, read, require, write
from ci.pipeline import (CandidateAttemptsExhausted, CatalogReviewExhausted,
                         CatalogTimeout, command, remote_head)
from ci.progress import log
from ci.refinement import DATA_DIRECTORIES, TASKS, configured_pipeline
from ci.runtime import utc


def factor_key(model, sfs, sf):
    return (model or 'primary') + '/' + sfs + '/' + sf


def factor_files(pipeline, sfs, sf):
    key = next(s['repoId'] for s in pipeline.state['slots'] if s['sfsId'] == sfs and s['sfId'] == sf)
    return {name: checksum for name, checksum in pipeline.sfs_files(sfs).items()
            if Path(name).stem == key and not name.startswith('repo-sfs-masks/')}


def seal_factor(pipeline, model, sfs, sf):
    pipeline.state.setdefault('factorGates', {})[factor_key(model, sfs, sf)] = {
        'files': factor_files(pipeline, sfs, sf), 'checkedAt': utc()}
    pipeline.save()


def verify_factor(pipeline, model, sfs, sf):
    gate = pipeline.state['factorGates'][factor_key(model, sfs, sf)]
    for name, checksum in gate['files'].items():
        require(byte_digest(pipeline.work / name) == checksum, 'Checked SF bytes changed: ' + name)


class Incremental:
    def __init__(self, pipeline):
        self.p = pipeline
        require(bool(pipeline.state.get('refinementPlan')), 'Incremental publication requires selective refinement')

    def head(self):
        return command(['git', 'rev-parse', 'HEAD'], self.p.output).stdout.strip()

    def data(self):
        p = self.p
        if 'incremental' not in p.state:
            require(not p.state.get('publicationIntent'), 'Finish the previous publication before enabling SF publication')
            for scope in p.state['sfsGates']:
                p.check_sfs_gate(scope)
            for queued in p.state.get('modelRetryQueue', {}).values():
                slot = next((s for s in p.state['slots'] if queued['key'].startswith(s['repoId'])), None)
                if slot and not (p.root / 'refinement-tasks' / queued['task'] / (slot['repoId'] + '.json')).exists():
                    p.state.setdefault('refinementPending', {}).setdefault(slot['repoId'], {
                        **slot, 'tasks': [queued['task']], 'status': 'pending-provider-retry',
                        'reason': 'An interrupted task remains in the checkpoint model retry queue'})
            # An interrupted SFS can have replayed only part of its immutable
            # task journals. Recover later completed positions before exporting.
            for task in TASKS:
                for marker in sorted((p.root / 'refinement-tasks' / task).glob('*.json')):
                    slot = next((s for s in p.state['slots'] if s['repoId'] == marker.stem), None)
                    if not slot or slot['sfsId'] in p.state['sfsGates']:
                        continue
                    for name, output in read(marker)['outputs'].items():
                        require(digest(output['value']) == output['digest'], 'Task journal changed')
                        require(name.startswith(tuple(d + '/' for d in DATA_DIRECTORIES))
                                and '..' not in Path(name).parts and not Path(name).is_absolute(), 'Unsafe task output')
                        write(p.work / name, output['value'])
            p.state['incremental'] = {'anchor': self.head(), 'history': [], 'batches': {},
                'visiblePositions': [s['repoId'] for s in p.state['slots']
                    if s['sfsId'] in p.state.get('primaryPassCompleted', [])],
                'completedPasses': [], 'events': [], 'refs': {p.state['branch']: p.state['expectedRemote']},
                'visibleFiles': {s['repoId']: factor_files(p, s['sfsId'], s['sfId'])
                    for s in p.state['slots'] if s['sfsId'] in p.state.get('primaryPassCompleted', [])}}
            p.save()
        return p.state['incremental']

    def begin(self, sfs, model='primary'):
        p, data = self.p, self.data()
        require(sfs in HEX and len(sfs) == 1 and model in ('primary', 'b', 'c', 'a'), 'Invalid SFS/model pass')
        plan = p.state['refinementPlan']
        if sfs not in plan['sfs']:
            return False
        batch_key = model + '/' + sfs
        existing = data['batches'].get(batch_key)
        if existing:
            data['activeBatch'] = batch_key
            p.save()
            return not existing.get('done')
        if model == 'primary':
            if sfs in p.state.get('primaryPassCompleted', []):
                return False
            factors = list(HEX)
        else:
            require(set(plan['sfs']) <= set(p.state.get('primaryPassCompleted', [])), 'Finish the primary pass first')
            if model not in plan.get('retryModels', []):
                return False
            required = plan['retryModels'][:plan['retryModels'].index(model)]
            require(set(required) <= set(data['completedPasses']), 'Finish the preceding model pass first')
            tag = plan['models'][model]
            queued = [v for v in p.state.get('modelRetryQueue', {}).values() if v['sfsId'] == sfs
                      and model in v['order'] and tag not in v['attemptedTags']]
            if not queued:
                return False
            factors = [sf for sf in HEX if any(v['key'].startswith('00' + sfs + sf) for v in queued)]
        if sfs in p.state['sfsGates']:
            p.check_sfs_gate(sfs)
            p.state['sfsGates'].pop(sfs)
        data['batches'][batch_key] = {'anchor': self.head(), 'factors': factors, 'commits': [], 'done': False}
        data['activeBatch'] = batch_key
        p.state['refinementPassModel'] = model
        if p.task_enabled('rework_questions'):
            p.state['rosterPending'] = sorted(set(p.state.get('rosterPending', [])) | {sfs})
        p.save()
        return True

    def batch(self):
        data = self.data()
        return data['activeBatch'], data['batches'][data['activeBatch']]

    def factor(self, sfs, sf):
        key, batch = self.batch()
        model, scope = key.split('/')
        require(scope == sfs, 'Another SFS batch is active')
        if sf in batch['factors']:
            self.p.refine(sfs, model_slot=None if model == 'primary' else model, sf_id=sf)

    def finish_sfs(self, sfs):
        p = self.p
        key, batch = self.batch()
        model, scope = key.split('/')
        require(scope == sfs, 'Another SFS batch is active')
        if batch.get('sealed'):
            p.check_sfs_gate(sfs)
            return
        for sf in batch['factors']:
            verify_factor(p, None if model == 'primary' else model, sfs, sf)
        slots = [s for s in p.state['slots'] if s['sfsId'] == sfs]
        for task in TASKS:
            if not p.task_enabled(task) or not (task == 'rework_questions' or task.startswith('translate_')):
                continue
            if task == 'rework_questions' and any(s['repoId'] in p.state.get('refinementPending', {}) for s in slots):
                continue
            p.activate(task)
            try:
                with p.server('finish-' + sfs + '-' + task) as server:
                    if task == 'rework_questions':
                        p.review_roster(server, sfs, slots)
                        p.state['rosterPending'] = [s for s in p.state.get('rosterPending', []) if s != sfs]
                    else:
                        p.translate_area(server, sfs, task.split('_')[1])
            except (CandidateAttemptsExhausted, CatalogReviewExhausted, CatalogTimeout) as error:
                if task.startswith('translate_'):
                    for slot in slots:
                        p.defer_refinement_task(task, slot, error)
                else:
                    p.state['rosterPending'] = sorted(set(p.state.get('rosterPending', [])) | {sfs})
                log('finish_sfs', sfs + ': review remains pending: ' + str(error))
        p.check_sfs_content(sfs)
        p.state['sfsGates'][sfs] = {'files': p.sfs_files(sfs), 'checkedAt': utc(),
            'tasks': [t for t in TASKS if p.task_enabled(t)], 'synthetic': p.state.get('dryRun', False),
            'pendingSlots': [s['repoId'] for s in slots if s['repoId'] in p.state.get('refinementPending', {})]}
        if model == 'primary':
            p.state['primaryPassCompleted'] = list(dict.fromkeys([*p.state.get('primaryPassCompleted', []), sfs]))
        batch['sealed'] = True
        p.save()

    def finish_pass(self, model):
        data = self.data()
        if model == 'primary':
            require(set(self.p.state['refinementPlan']['sfs']) <= set(self.p.state.get('primaryPassCompleted', [])),
                    'Primary pass is unfinished')
        for key, batch in data['batches'].items():
            if key.startswith(model + '/'):
                require(batch.get('done'), 'Publish the remaining SFS batch first')
        data['completedPasses'] = list(dict.fromkeys([*data['completedPasses'], model]))
        self.p.save()

    def snapshot(self):
        """Render only visible positions; all other positions retain pinned base bytes."""
        p, data = self.p, self.data()
        visible = set(data['visiblePositions'])
        for names in data.get('visibleFiles', {}).values():
            for name, checksum in names.items():
                require(byte_digest(p.work / name) == checksum, 'Earlier visible SF changed: ' + name)
        files = {}
        for directory in DATA_DIRECTORIES:
            if directory in ('repo-sfs-masks', 'mask-coverage'):
                continue
            for name in set(file_manifest(p.work / directory)) | set(file_manifest(p.work / 'base' / directory)):
                stem = Path(name).stem
                root = p.work if stem in visible or len(stem) != 4 else p.work / 'base'
                source = root / directory / name
                if source.is_file():
                    files[directory + '/' + name] = source.read_bytes()
        for directory in ('repo-signer', 'schemas', 'repo-sfs-masks/templates'):
            for name in file_manifest(p.work / 'inputs' / directory):
                files[directory + '/' + name] = (p.work / 'inputs' / directory / name).read_bytes()
        if p.state.get('catalogProfile'):
            write(p.work / 'catalog-profile.json', p.state['catalogProfile'])
            files['catalog-profile.json'] = (p.work / 'catalog-profile.json').read_bytes()
        import tempfile
        from ci.site_views import page
        with tempfile.TemporaryDirectory(prefix='sf-view-', dir=p.root) as temporary:
            view = Path(temporary)
            for name, content in files.items():
                target = view / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            original = p.state
            p.state = copy.deepcopy(original)
            try:
                for slot in p.state['slots']:
                    if slot['sfsId'] in p.state.get('rosterPending', []):
                        p.state.setdefault('refinementPending', {}).setdefault(slot['repoId'], {
                            **slot, 'reason': 'SFS roster review is pending', 'annotation': '[non-validated]'})
                for entry in p.state.get('refinementPending', {}).values():
                    entry['annotation'] = '[non-validated]'
                p.write_pages(catalog_root=view)
                for locale in ('en-US', 'fr-FR', 'el-GR'):
                    navigation = p.work / ('pages/catalog/_meta.' + locale + '.json')
                    if navigation.exists():
                        write(navigation, {**read(navigation), 'progress': 'Live progress'})
                        index = p.work / ('pages/catalog/index.' + locale + '.mdx')
                        index.write_text(index.read_text() + '\n[Live catalog progress](/catalog/progress).\n')
            finally:
                p.state = original
            for directory in ('pages/catalog', 'pages/questions'):
                for name in file_manifest(p.work / directory):
                    files[directory + '/' + name] = (p.work / directory / name).read_bytes()
            for path in p.work.glob('pages/sfs.*.mdx'):
                files[path.relative_to(p.work).as_posix()] = path.read_bytes()
            pending = list(p.state.get('refinementPending', {}).values())
            processed = [s for s in HEX if any(k[2] == s for k in visible)]
            q_count = sum(not read(view / 'repo-sf/en' / (k + '.json')).get('isPlaceholder')
                          and (view / 'catalog/questions/en' / (k + '.json')).exists() for k in visible)
            languages = ['en'] + [lang for lang in ('fr', 'el') if (view / 'json' / lang).exists()]
            status = {'recordType': 'catalog-site-view', 'runId': p.state['runId'],
                'stage': 'catalog-run-finished' if p.state.get('modelPassesComplete') else 'sf-progress',
                'languages': languages, 'branch': p.state['branch'], 'questionCount': q_count,
                'pendingQuestionCount': 256-q_count, 'humanApproved': False, 'assessmentPerformed': False,
                'acceptedKnowledge': False, 'catalogAccepted': False,
                'refinement': {'selectedSfs': p.state['refinementPlan']['sfs'], 'completedSfs': processed,
                    'tasks': [t for t in TASKS if p.task_enabled(t)],
                    'pendingPositions': [v for v in pending if v['sfsId'] in processed],
                    'processedSfCount': len(visible), 'completedSfsMeaning': 'SFSs with visible SF work; roster status is separate'},
                'rosterReviewsPending': sorted(set(p.state.get('rosterPending', []))),
                'publicationGranularity': 'sf', 'modelPassesComplete': p.state.get('modelPassesComplete', False)}
            status['currentModelPass'] = p.state.get('refinementPassModel', 'primary')
            status['lastPosition'] = data.get('lastPosition')
            if p.state.get('catalogProfile'):
                status['catalogProfile'] = p.state['catalogProfile']
            write(view / 'site-publication.json', status)
            write(view / 'refinement-status.json', {**status, 'recordType': 'selective-catalog-refinement',
                'baseCatalog': p.state['baseCatalog'], 'plan': p.state['refinementPlan'],
                'taskModels': p.state.get('taskModelOutcomes', {}),
                'pendingPositions': pending, 'completedSfs': sorted(p.state['sfsGates'])})
            for name in ('site-publication.json', 'refinement-status.json'):
                files[name] = (view / name).read_bytes()
            selected_pending = [v for v in pending if v.get('status') != 'pending-roster-review']
            body = (f'{len(visible)} SF snapshots are visible. {len(selected_pending)} attempted positions have pending selected tasks. '
                    f'{len(set(p.state.get("rosterPending", [])))} SFS roster reviews remain pending.\n\n'
                    'Individual task model checks are authoring checks. No human approval or operational assessment is claimed.\n\n'
                    'Positions not processed in this run retain their base content.\n\n'
                    '[Browse the catalog](/catalog/sets).\n\n')
            if data.get('lastPosition'):
                last = data['lastPosition']
                body += f'Last processed: SFS #{last["sfsId"]}, SF #{last["sfId"]}; model pass `{last["modelPass"]}`.\n\n'
            pending_keys = {v['repoId'] for v in selected_pending}
            for scope in processed:
                body += f'## SFS {scope}\n\n| SF | Published work |\n| --- | --- |\n'
                for slot in (s for s in p.state['slots'] if s['sfsId'] == scope):
                    key, sf = slot['repoId'], slot['sfId']
                    state = ('Base retained; not processed' if key not in visible else
                        'Selected work pending' if key in pending_keys else
                        'Task checks passed; roster review pending' if scope in p.state.get('rosterPending', [])
                        else 'Selected task checks passed')
                    body += f'| [SF {sf}](/catalog/sets/{scope}/{sf}) | {state} |\n'
            files['pages/catalog/progress.en-US.mdx'] = page('Catalog progress', 'Live SF publication and pending work', body).encode()
        return files

    def prepare(self, kind, sfs=None, sf=None):
        p, data = self.p, self.data()
        pending = p.state.get('publicationIntent')
        event = kind + ':' + (data.get('activeBatch', '') if kind != 'catalog' else 'final') + (':' + sf if sf else '')
        if pending:
            require(pending.get('incremental') and pending['event'] == event, 'Another publication is awaiting recovery')
            if pending.get('commit') and self.head() == pending['commit']:
                return True
            files = self.snapshot()
            require({n: 'sha256:' + __import__('hashlib').sha256(v).hexdigest() for n, v in files.items()}
                    == pending['artifactFiles'], 'Prepared snapshot changed during recovery')
            self.commit_prepared(pending, files)
            return True
        if any(e['event'] == event for e in data['events']):
            return False
        require(not command(['git', 'status', '--porcelain'], p.output).stdout.strip(), 'Output checkout has unrelated changes')
        old_head = self.head()
        parent = old_head
        if kind == 'sf':
            key, batch = self.batch()
            model, scope = key.split('/')
            require(sfs == scope, 'SFS batch changed')
            if sf not in batch['factors']:
                return False
            verify_factor(p, None if model == 'primary' else model, sfs, sf)
            data.setdefault('visibleFiles', {})['00' + sfs + sf] = factor_files(p, sfs, sf)
            data['visiblePositions'] = sorted(set(data['visiblePositions']) | {'00' + sfs + sf})
            data['lastPosition'] = {'sfsId': sfs, 'sfId': sf, 'modelPass': model}
        elif kind == 'sfs':
            _, batch = self.batch()
            require(batch.get('sealed'), 'SFS must finish its checks before consolidation')
            p.check_sfs_gate(sfs)
            parent = batch['anchor']
            expected = batch['commits']
            actual = command(['git', 'rev-list', '--reverse', parent + '..' + old_head], p.output).stdout.splitlines()
            require(actual == expected, 'Refusing to squash commits not owned by this SFS batch')
        elif kind == 'catalog':
            plan = p.state['refinementPlan']
            require({'primary', *plan.get('retryModels', [])} <= set(data['completedPasses']), 'Complete all model passes first')
            p.annotate_non_validated()
            p.state['modelPassesComplete'] = True
            # Annotation is a controlled presentation change, after verifying
            # all SFS gates; retain the updated bytes for export/recovery.
            data['visibleFiles'] = {s['repoId']: factor_files(p, s['sfsId'], s['sfId'])
                for s in p.state['slots'] if s['repoId'] in data['visiblePositions']}
            p.save()
            parent = data['anchor']
            actual = command(['git', 'rev-list', '--reverse', parent + '..' + old_head], p.output).stdout.splitlines()
            require(actual == data['history'], 'Refusing to squash commits not owned by this catalog run')
        else:
            raise ValueError('Unknown incremental publication kind')
        files = self.snapshot()
        import json
        languages = json.loads(files['site-publication.json'])['languages']
        if p.state['publish']:
            for ref in [p.state['branch'], *[lang + '/' + p.state['branch'].split('/', 1)[1]
                    for lang in languages if lang != 'en' and p.state['branch'].startswith('en/')]]:
                observed = remote_head(p.output, ref)
                if ref not in data['refs']:
                    if observed:
                        command(['git', 'fetch', '--no-tags', 'origin', 'refs/heads/' + ref], p.output, authenticated=True)
                        require(command(['git', 'merge-base', '--is-ancestor', observed, old_head], p.output, check=False).returncode == 0,
                                'Translated branch has independent changes')
                    data['refs'][ref] = observed
                require(observed == data['refs'][ref], 'Destination changed; refusing to overwrite it')
        tracked = command(['git', 'ls-files'], p.output).stdout.splitlines()
        generated = (*DATA_DIRECTORIES, 'pages/catalog', 'pages/questions', 'pages/sfs', 'site-status',
                     'completion-status', 'area-status', 'mask-status', 'catalog/pending')
        removed = [n for n in tracked if (n.startswith(tuple(d + '/' for d in generated))
                   or n in ('next-base.json', 'pipeline-status.json', 'catalog-feedback.json',
                            'release-manifest.json', 'catalog/en.json', 'catalog/fr.json', 'catalog/el.json')
                   or n.startswith('pages/sfs.')) and n not in files]
        message = f'catalog: {kind} ' + (f'{sfs}/{sf}' if kind == 'sf' else sfs or 'complete') + f' [{p.state["runId"]}]'
        pending = {'incremental': True, 'event': event, 'kind': kind, 'sfs': sfs,
            'sf': sf,
            'stage': 'incremental-' + kind, 'parent': parent, 'oldHead': old_head,
            'message': message, 'commit': None,
            'artifactFiles': {n: 'sha256:' + __import__('hashlib').sha256(v).hexdigest() for n, v in files.items()},
            'oldFiles': {n: byte_digest(p.output / n) if (p.output / n).is_file() else None for n in set(files) | set(removed)},
            'removedRootFiles': removed, 'expectedRefs': copy.deepcopy(data['refs'])}
        p.state['publicationIntent'] = pending
        p.save()
        self.commit_prepared(pending, files)
        return True

    def commit_prepared(self, pending, files):
        p = self.p
        require(self.head() == pending['oldHead'], 'Prepared output history changed')
        changed = command(['git', 'diff', 'HEAD', '--name-only'], p.output).stdout.splitlines()
        changed += command(['git', 'ls-files', '--others', '--exclude-standard'], p.output).stdout.splitlines()
        require(set(changed) <= set(pending['oldFiles']), 'Output has unrelated changes during recovery')
        for name, old in pending['oldFiles'].items():
            path = p.output / name
            require(not any(v.is_symlink() for v in (path, *path.parents)
                            if v == p.output or p.output in v.parents), 'Symlink in publication output')
            actual = byte_digest(path) if path.is_file() else None
            require(actual in (old, pending['artifactFiles'].get(name)), 'Output file changed independently: ' + name)
        for name in pending['removedRootFiles']:
            (p.output / name).unlink(missing_ok=True)
        for name, content in files.items():
            target = p.output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        command(['git', 'add', '-A', '--', *sorted(pending['oldFiles'])], p.output)
        tree = command(['git', 'write-tree'], p.output).stdout.strip()
        commit = command(['git', '-c', 'user.name=UNIQ2Me CI', '-c', 'user.email=ci@users.noreply.github.com',
                          'commit-tree', tree, '-p', pending['parent'], '-m', pending['message']], p.output).stdout.strip()
        pending['commit'] = commit
        p.save()
        command(['git', 'update-ref', 'HEAD', commit, pending['oldHead']], p.output)

    def publish(self):
        p, data = self.p, self.data()
        pending = p.state.get('publicationIntent')
        if not pending:
            return
        require(pending.get('incremental'), 'Another publication mechanism owns this intent')
        commit = pending['commit']
        require(self.head() == commit, 'Prepared publication HEAD changed')
        require(command(['git', 'rev-parse', 'HEAD^'], p.output).stdout.strip() == pending['parent'], 'Prepared parent changed')
        for name, expected in pending['artifactFiles'].items():
            require(byte_digest(p.output / name) == expected, 'Prepared publication bytes changed: ' + name)
        require(all(not (p.output / n).exists() for n in pending['removedRootFiles']), 'Stale output reappeared')
        if p.state['publish']:
            observed = {ref: remote_head(p.output, ref) for ref in pending['expectedRefs']}
            require(all(value in (pending['expectedRefs'][ref], commit) for ref, value in observed.items()),
                    'Destination changed; refusing publication')
            updates = [commit + ':refs/heads/' + ref for ref, value in observed.items() if value != commit]
            if updates:
                leases = ['--force-with-lease=refs/heads/' + ref + ':' + (pending['expectedRefs'][ref] or '')
                          for ref, value in observed.items() if value != commit]
                command(['git', 'push', '--atomic', *leases, 'origin', *updates], p.output, authenticated=True)
            require(all(remote_head(p.output, ref) == commit for ref in pending['expectedRefs']), 'Published refs did not verify')
            data['refs'] = {ref: commit for ref in pending['expectedRefs']}
            p.state['expectedRemote'] = commit
        if pending['kind'] == 'sf':
            _, batch = self.batch()
            batch['commits'].append(commit)
            data['history'].append(commit)
        elif pending['kind'] == 'sfs':
            _, batch = self.batch()
            if batch['commits']:
                data['history'] = data['history'][:-len(batch['commits'])]
            data['history'].append(commit)
            batch.update(done=True, commit=commit)
        else:
            data['history'] = [commit]
            data['catalogCommit'] = commit
        data['events'].append({'event': pending['event'], 'commit': commit, 'oldHead': pending['oldHead'],
            'parent': pending['parent'], 'pushed': p.state['publish'], 'recordedAt': utc()})
        p.state['localHead'] = commit
        p.state.pop('publicationIntent')
        p.save()
        log('incremental.publish', ('Pushed ' if p.state['publish'] else 'Local only: ') + pending['event'] + ': ' + commit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['begin', 'factor', 'prepare-factor', 'finish-sfs',
        'prepare-sfs', 'publish', 'finish-pass', 'prepare-catalog', 'recover', 'select-pass'])
    parser.add_argument('sfs', nargs='?', choices=list(HEX))
    parser.add_argument('sf', nargs='?', choices=list(HEX))
    parser.add_argument('--pass', dest='model', choices=['primary', 'a', 'b', 'c'], default='primary')
    args = parser.parse_args()
    p = configured_pipeline()
    p.pinned()
    runner = Incremental(p)
    active = True
    selected_model = None
    if args.command == 'select-pass':
        selected = os.environ.get('PUBLICATION_PASS', 'primary')
        require(selected in ('primary', 'final', 'retry-1', 'retry-2', 'retry-3'), 'Invalid publication pass')
        if selected in ('primary', 'final'):
            selected_model = 'primary'
        else:
            index = int(selected[-1]) - 1
            models = p.state['refinementPlan'].get('retryModels', [])
            active = index < len(models)
            selected_model = models[index] if active else ''
    elif args.command == 'recover':
        pending = p.state.get('publicationIntent')
        active = bool(pending and pending.get('incremental'))
        if active:
            runner.prepare(pending['kind'], pending.get('sfs'), pending.get('sf'))
    elif args.command == 'begin':
        active = runner.begin(args.sfs, args.model)
    elif args.command == 'factor':
        runner.factor(args.sfs, args.sf)
    elif args.command == 'prepare-factor':
        active = runner.prepare('sf', args.sfs, args.sf)
    elif args.command == 'finish-sfs':
        runner.finish_sfs(args.sfs)
    elif args.command == 'prepare-sfs':
        active = runner.prepare('sfs', args.sfs)
    elif args.command == 'prepare-catalog':
        active = runner.prepare('catalog')
    elif args.command == 'finish-pass':
        runner.finish_pass(args.model)
    else:
        runner.publish()
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write('active=' + str(active).lower() + '\n')
            if selected_model is not None:
                stream.write('model=' + selected_model + '\n')


if __name__ == '__main__':
    main()
