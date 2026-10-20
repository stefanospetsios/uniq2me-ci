"""Explicit migration from legacy controllers and recorded current-main upgrades."""
import os

from ci.contracts import read, require
from ci.runtime import utc

REPOSITORY = 'stefanospetsios/uniq2me-ci'


def upgrade(pipeline, manifest, controller, command):
    automatic = os.environ.get('CONTROLLER_UPDATES') == 'main'
    if automatic:
        require(os.environ.get('CONTROLLER_REPOSITORY') == REPOSITORY, 'Unexpected shared controller repository')
        origin = command(['git', 'remote', 'get-url', 'origin'], pipeline.trusted).stdout.strip()
        require(origin in ('https://github.com/' + REPOSITORY, 'https://github.com/' + REPOSITORY + '.git',
                           'git@github.com:' + REPOSITORY + '.git'), 'Shared controller origin is not trusted')
        framework = pipeline.state.get('catalogProfile', {}).get('frameworkId', '')
        require(framework == ('dora' if os.environ.get('CATALOG_PROFILE') == 'dora' else ''),
                'Cannot migrate a checkpoint between frameworks')
        caller = os.environ.get('GITHUB_REPOSITORY', '')
        expected = 'stefanospetsios/uniq2me-dora' if framework == 'dora' else 'stefanospetsios/uniq2me-air'
        require(caller == expected, 'Checkpoint framework does not match its caller repository')
        require(not manifest.get('workflowRepository') or manifest['workflowRepository'] == caller,
                'Checkpoint belongs to another caller repository')
    source_repository = manifest.get('controllerRepository') or pipeline.state.get('controllerRepository')
    if controller == manifest['controllerCommit']:
        if automatic:
            require(source_repository == REPOSITORY, 'Shared checkpoint controller identity is missing')
        return
    require(pipeline.state.get('refinementPlan'), 'Only selective catalog checkpoints support explicit controller upgrades')
    explicit = os.environ.get('RESUME_RUN_ID') == manifest['workflowRunId']
    if automatic and source_repository != REPOSITORY:
        # Old private repositories have unrelated Git histories. Accept only the
        # reviewed, named commits recorded when the shared controller was created.
        require(explicit, 'Legacy checkpoint migration requires an explicit source run ID')
        compatibility = read(pipeline.trusted / 'controller-compatibility.json')
        caller = os.environ['GITHUB_REPOSITORY']
        require(source_repository in (None, '', caller), 'Unexpected legacy controller repository')
        require(manifest['controllerCommit'] in compatibility['legacyControllers'].get(caller, []),
                'Legacy controller commit is not in the reviewed migration registry')
        migration = 'reviewed-legacy-controller'
    else:
        require(automatic or explicit, 'Checkpoint controller commit differs; use a new dispatch with resume_run_id')
        require(command(['git', 'merge-base', '--is-ancestor', manifest['controllerCommit'], controller],
                        pipeline.trusted, check=False).returncode == 0,
                'Resume controller must descend from the checkpoint controller')
        framework = pipeline.state.get('catalogProfile', {}).get('frameworkId', '')
        require(framework == ('dora' if os.environ.get('CATALOG_PROFILE') == 'dora' else ''),
                'Cannot migrate a checkpoint between frameworks')
        migration = 'current-main-controller' if automatic else 'explicit-descendant-controller'
    pipeline.state.setdefault('resumeLineage', []).append({
        'sourceWorkflowRunId': manifest['workflowRunId'], 'sourceControllerCommit': manifest['controllerCommit'],
        'sourceControllerRepository': source_repository or os.environ.get('GITHUB_REPOSITORY', ''),
        'executionControllerCommit': controller, 'executionControllerRepository': REPOSITORY if automatic else source_repository,
        'policy': migration, 'resumedAt': utc(),
        'retainedInputAndRulePins': True})
    pipeline.state['controllerCommit'] = controller
    if automatic:
        pipeline.state['controllerRepository'] = REPOSITORY
        pipeline.state['workflowRepository'] = os.environ['GITHUB_REPOSITORY']
