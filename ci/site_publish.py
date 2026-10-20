"""Publish the checked website tree to language/qwen3 branches with recovery journals."""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import byte_digest, read, require, write
from ci.progress import log
from ci.site_views import configure_workflow, site_view, template_skeleton, verify_checkpoint, write_view


def publish_tree(output, root, state, stage, source_commit, *, overlay=None, save=None):
    from ci.pipeline import command, remote_head
    root, output = Path(root), Path(output)
    branch = state.get('siteBranch', 'en/qwen3')
    require(branch.startswith('en/') and branch.count('/') == 1, 'Website branch must be en/<instance>')
    require(branch != state['branch'], 'Use a separate website branch for the run archive')
    command(['git', 'check-ref-format', '--branch', branch], output)
    journals = state.setdefault('sitePublications', {})
    journal = journals.get(stage)
    if journal and journal.get('completed'):
        log('site_publish', f'Already published {stage}: {journal["commit"]}')
        return journal
    bundle = root / 'site-publications' / (stage + '.bundle')
    origin = command(['git', 'remote', 'get-url', 'origin'], output).stdout.strip()
    with tempfile.TemporaryDirectory(prefix='uniq2me-site-', dir=output.parent) as directory:
        repo = Path(directory) / 'output'
        command(['git', 'clone', '--no-checkout', '--shared', str(output), str(repo)], output.parent)
        command(['git', 'remote', 'set-url', 'origin', origin], repo)
        if not journal:
            expected = remote_head(repo, branch)
            if 'siteExpectedRemote' in state:
                require(expected == state['siteExpectedRemote'],
                        'Website branch changed since the previous phase; refusing to overwrite: ' + branch)
            parent = expected
            if expected:
                command(['git', 'fetch', '--no-tags', 'origin', 'refs/heads/' + branch], repo, authenticated=True)
                require(command(['git', 'rev-parse', 'FETCH_HEAD'], repo).stdout.strip() == expected,
                        'Website parent changed during checkout')
            else:
                parent = remote_head(repo, 'main')
                require(parent, 'Output repository main branch is required for the website workflow')
                command(['git', 'fetch', '--no-tags', 'origin', 'refs/heads/main'], repo, authenticated=True)
                require(command(['git', 'rev-parse', 'FETCH_HEAD'], repo).stdout.strip() == parent,
                        'Website main parent changed during checkout')
            command(['git', 'checkout', '--detach', parent], repo)
            # The source is an already committed, gated archive tree. Keep both histories
            # as merge parents; never copy runtime transcripts or an unchecked candidate.
            command(['git', 'read-tree', '--reset', '-u', source_commit], repo)
            if overlay:
                write_view(repo, overlay)
            workflow = repo / '.github/workflows/build-deploy.yml'
            require(workflow.is_file(), 'Website build workflow is missing')
            require(configure_workflow(workflow.read_text()) == workflow.read_text(),
                    'Website workflow has not been configured for the output repository')
            view = read(repo / 'site-publication.json')
            refs = [branch] + [language + '/' + branch.split('/')[1]
                               for language in view['languages'] if language != 'en']
            expected_refs = {branch: expected}
            for ref in refs[1:]:
                previous = remote_head(repo, ref)
                if previous:
                    command(['git', 'fetch', '--no-tags', 'origin', 'refs/heads/' + ref], repo, authenticated=True)
                    require(command(['git', 'rev-parse', 'FETCH_HEAD'], repo).stdout.strip() == previous,
                            'Translated website parent changed during checkout')
                    require(command(['git', 'merge-base', '--is-ancestor', previous, parent], repo,
                                    check=False).returncode == 0,
                            'Translated website branch has independent changes: ' + ref)
                expected_refs[ref] = previous
            command(['git', 'add', '--all'], repo)
            tree = command(['git', 'write-tree'], repo).stdout.strip()
            args = ['git', '-c', 'user.name=UNIQ2Me CI', '-c',
                    'user.email=uniq2me-ci@users.noreply.github.com', 'commit-tree', tree, '-p', parent]
            if command(['git', 'merge-base', '--is-ancestor', source_commit, parent], repo,
                       check=False).returncode != 0:
                args += ['-p', source_commit]
            args += ['-m', f'feat(qwen3): publish {stage} website [{state["runId"]}]', '-m',
                     f'Archive commit: {source_commit}\n'
                     f'Model-reviewed questions: {view["questionCount"]}; pending: {view["pendingQuestionCount"]}.\n'
                     'Generated authoring draft; no human approval or operational assessment claimed.']
            commit = command(args, repo).stdout.strip()
            command(['git', 'update-ref', 'refs/heads/ci-site-publication', commit], repo)
            bundle.parent.mkdir(parents=True, exist_ok=True)
            command(['git', 'bundle', 'create', str(bundle), 'refs/heads/ci-site-publication'], repo)
            journal = {'stage': stage, 'sourceCommit': source_commit, 'commit': commit,
                       'expectedRefs': expected_refs, 'bundleDigest': byte_digest(bundle), 'completed': False}
            journals[stage] = journal
            if save:
                save()
        else:
            require(journal['sourceCommit'] == source_commit, 'Website retry has a different archive commit')
            require(byte_digest(bundle) == journal['bundleDigest'], 'Website recovery bundle changed')
            command(['git', 'fetch', str(bundle), 'refs/heads/ci-site-publication'], repo)
        commit = journal['commit']
        observed = {ref: remote_head(repo, ref) for ref in journal['expectedRefs']}
        for ref, expected in journal['expectedRefs'].items():
            require(observed[ref] in (expected, commit), 'Website branch changed; refusing to overwrite: ' + ref)
        updates = [commit + ':refs/heads/' + ref for ref in observed if observed[ref] != commit]
        if updates:
            command(['git', 'push', '--atomic', 'origin', *updates], repo, authenticated=True)
        require(all(remote_head(repo, ref) == commit for ref in observed), 'Website push could not be verified')
        journal['completed'] = True
        state['siteExpectedRemote'] = commit
        if save:
            save()
        log('site_publish', f'Published {stage} website: {commit} -> ' + ', '.join(observed))
        return journal


def publish(pipeline, stage):
    if pipeline.state.get('dryRun') or not pipeline.state['publish']:
        log('site_publish', 'Website push skipped for dry/non-publishing runs')
        return
    if not (pipeline.work / 'inputs/site-workflow.yml').exists():
        log('site_publish', 'Website push skipped: no workflow was pinned at initialization')
        return
    if pipeline.state['branch'] == pipeline.state.get('siteBranch', 'en/qwen3'):
        log('site_publish', 'Archive target already is the website branch; its checked view was published in the milestone')
        return
    publication = next((p for p in pipeline.state['publications'] if p['stage'] == stage), None)
    require(publication and publication['pushed'], 'Archive milestone must be published before its website')
    publish_tree(pipeline.output, pipeline.root, pipeline.state, stage, publication['commit'], save=pipeline.save)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['catalog', 'extended', 'masks', 'translate-fr', 'translate-el'])
    parser.add_argument('--checkpoint', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--journal-root', type=Path)
    parser.add_argument('--workflow', type=Path)
    parser.add_argument('--template-repository', type=Path)
    args = parser.parse_args()
    if args.checkpoint:
        from ci.pipeline import command
        require(args.output and args.journal_root and args.workflow, 'Checkpoint publication needs output, journal-root and workflow')
        state = verify_checkpoint(args.checkpoint)
        require(not state.get('dryRun'), 'Synthetic checkpoints cannot publish to the live website')
        publication = next((p for p in state['publications'] if p['stage'] == args.stage), None)
        require(publication and publication['pushed'], 'Checkpoint milestone was not published')
        command(['git', 'fetch', str(args.checkpoint / 'output.bundle'), 'HEAD'], args.output)
        view = site_view(args.checkpoint / 'work', state, args.stage)
        if args.template_repository:
            for name, data in template_skeleton(args.template_repository, state['inputCommit']).items():
                if name in view:
                    require(view[name] == data, 'Checkpoint and pinned template disagree: ' + name)
                view[name] = data
        view['.github/workflows/build-deploy.yml'] = configure_workflow(args.workflow.read_text()).encode()
        manifest = read(args.checkpoint / 'checkpoint-manifest.json')
        failure = read(args.checkpoint / 'last-failure.json') if (args.checkpoint / 'last-failure.json').exists() else {}
        view['checkpoint-source.json'] = (__import__('json').dumps({
            'recordType': 'verified-checkpoint-site-import', 'workflowRunId': manifest['workflowRunId'],
            'workflowAttempt': manifest['workflowAttempt'], 'checkpointManifestDigest': byte_digest(
                args.checkpoint / 'checkpoint-manifest.json'), 'sourceCommit': publication['commit'],
            'publishedStage': args.stage, 'failedStage': failure.get('stage'),
            'sourceArtifact': 'uniq2me-checkpoint-' + str(manifest['workflowRunId']) + '-'
                + (failure.get('stage') or args.stage) + '-' + str(manifest['workflowAttempt']),
            'runtimeFilesPublished': False}, indent=2) + '\n').encode()
        args.journal_root.mkdir(parents=True, exist_ok=True)
        journal_path = args.journal_root / 'site-state.json'
        if journal_path.exists():
            previous = read(journal_path)
            require(previous['runId'] == state['runId'], 'Website journal belongs to another run')
            state['sitePublications'] = previous.get('sitePublications', {})
        publish_tree(args.output, args.journal_root, state, args.stage, publication['commit'],
                     overlay=view, save=lambda: write(journal_path, state))
    else:
        from ci.pipeline import Pipeline
        publish(Pipeline(), args.stage)


if __name__ == '__main__':
    main()
