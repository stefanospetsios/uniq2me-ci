"""Deterministic website views of gated artifacts; no model calls or acceptance changes."""
import copy
import json
from pathlib import Path

from ci.contracts import HEX, byte_digest, read, require

STAGES = ['catalog', 'extended', 'masks', 'translate-fr', 'translate-el']
LOCALES = {'en': 'en-US', 'fr': 'fr-FR', 'el': 'el-GR', 'de': 'de-DE'}


def mdx(value):
    return str(value or '').replace('&', '&amp;').replace('<', '&lt;').replace(
        '>', '&gt;').replace('{', '&#123;').replace('}', '&#125;')


def page(title, description, body):
    return ('---\ntitle: ' + json.dumps(title, ensure_ascii=False) + '\ndescription: '
            + json.dumps(description, ensure_ascii=False) + '\n---\n\n# ' + mdx(title)
            + '\n\n' + body + '\n')


def configure_workflow(source):
    """Keep the output owner's build workflow, selecting catalogs from that repository."""
    import yaml
    parsed = yaml.load(source, Loader=yaml.BaseLoader)
    require(isinstance(parsed, dict) and 'jobs' in parsed, 'Missing website build workflow')
    env_line = '  UNIQ2ME_CATALOG_REPOSITORY: ${{ github.repository }}'
    if env_line not in source:
        require('env:\n' in source, 'Website workflow needs a global env mapping')
        source = source.replace('env:\n', 'env:\n' + env_line + '\n', 1)
    old = 'node uniq2me-pages/ci/checkout-catalogs.mjs --config uniq2me-pages/repositories/branches.json'
    if old in source:
        replacement = """node <<'NODE'
          const fs = require('node:fs')
          const config = JSON.parse(fs.readFileSync('uniq2me-pages/repositories/branches.json', 'utf8'))
          config.repository = process.env.GITHUB_REPOSITORY
          if (fs.existsSync('site-publication.json') && process.env.SOURCE_REF !== 'main') {
            const publication = JSON.parse(fs.readFileSync('site-publication.json', 'utf8'))
            const instance = process.env.SOURCE_REF.split('/')[1]
            config.branches = publication.languages.map(language => language + '/' + instance)
          }
          fs.writeFileSync('catalog-branches.json', JSON.stringify(config, null, 2) + '\\n')
          NODE
          node uniq2me-pages/ci/checkout-catalogs.mjs --config catalog-branches.json"""
        source = source.replace(old, replacement)
    require('--config catalog-branches.json' in source and
            'config.repository = process.env.GITHUB_REPOSITORY' in source,
            'Website workflow must fetch catalogs from the output repository')
    yaml.load(source, Loader=yaml.BaseLoader)
    return source


def site_view(work, state, stage, branch='en/qwen3'):
    """Return root-relative bytes, reading completed fields only from checked gates."""
    work = Path(work)
    require(stage in STAGES and stage in state['stages'], 'Website milestone has not passed its gate')
    stages = STAGES[:STAGES.index(stage) + 1]
    checked = {}
    for name in stages:
        if name in state['stages']:
            checked.update(state['stages'][name]['files'])
    for name, checksum in checked.items():
        require(byte_digest(work / name) == checksum, 'Website source changed after validation: ' + name)
    files = {}

    def put(name, value):
        files[name] = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()

    def raw(name, source):
        require(not source.is_symlink(), 'Website source cannot be a symlink')
        files[name] = source.read_bytes()

    languages = ['en'] + [lang for lang in ('fr', 'el') if 'translate-' + lang in stages
                         and 'translate-' + lang in state['stages']]
    pending = state.get('catalogPending', {})
    for language in languages:
        locale = LOCALES[language]
        area_links = []
        completed = 'extended' in stages and 'extended' in state['stages']
        if language != 'en':
            completed = True
        for sfs in HEX:
            area_name = f'json/{language}/{sfs}.json'
            area_source = work / area_name if area_name in checked else work / 'inputs' / area_name
            area = read(area_source)
            area_links.append(f'- [SFS {sfs}: ' + mdx(area['keyTitle']) + f'](/catalog/sets/{sfs})')
            require([entry['id'] for entry in area['set']] == list(HEX), 'Incomplete website area')
            raw(area_name, area_source)
            for entry in area['set']:
                key, sf_id = entry['repoId'], entry['id']
                q_name = f'catalog/questions/en/{key}.json'
                require(q_name in checked, 'Website question has no checked catalog binding: ' + key)
                question = read(work / q_name)
                record_name = f'repo-sf/{language}/{key}.json'
                solution_name = f'solutions/{language}/{key}.json'
                ready = completed and key not in pending and record_name in checked
                if completed and record_name in checked:
                    record = read(work / record_name)
                    raw(record_name, work / record_name)
                    status = ('Completed authoring draft; no operational assessment has been performed.'
                              if ready else 'Pending question: unreviewed placeholder. Do not use it in an assessment.')
                else:
                    record = copy.deepcopy(read(work / f'inputs/repo-sf/en/{key}.json'))
                    record['identity']['language'] = language
                    record['review'] = {'status': 'draft', 'approvals': []}
                    record['resultScore'] = None
                    record['isPlaceholder'] = key in pending
                    if key not in pending:
                        require(question.get('recordType') == 'question-candidate', 'Invalid website question')
                        contract = question['contract']
                        record['question']['shortBinaryQuestion'] = question['shortBinaryQuestion']
                        record['question']['plain'] = (
                            'Model-reviewed question draft. Completed explanations and a canonical solution '
                            'are pending. The proposed positive criterion is: ' + contract['positive'])
                        record['question']['technical-short'] = (
                            'Actor: ' + contract['actor'] + '. Scope: ' + contract['scope']
                            + '. Evidence period: ' + contract['evidencePeriod'] + '. Required evidence: '
                            + '; '.join(contract['requiredEvidence']) + '. Negative criterion: '
                            + contract['negative'] + '. Missing evidence: ' + contract['missingEvidence'])
                        record['question']['promptAiOptimization'] = (
                            'Preserve the model-reviewed question and contract; complete explanations and '
                            'the canonical solution through the remaining validation gates.')
                        record['meta']['title'] = 'SF ' + sf_id + ': question draft'
                    record['openSourceApproach'] = 'Canonical solution pending; no implementation was executed.'
                    record['editLink'] = f'https://github.com/stefanospetsios/uniq2me-ai/edit/{branch}/{record_name}'
                    record['jsonLink'] = f'https://github.com/stefanospetsios/uniq2me-ai/blob/{branch}/{record_name}'
                    put(record_name, record)
                    status = ('Pending question: unreviewed placeholder. Do not use it in an assessment.'
                              if key in pending else 'Question model review passed; solution and completed SF authoring pending.')
                require(record['identity'] == {'language': language, 'sfsId': sfs, 'sfId': sf_id,
                                               'repoId': key}, 'Website record identity changed')
                put(f'site-status/{language}/{key}.json', {
                    'identity': record['identity'], 'questionModelReviewPassed': key not in pending,
                    'sfAuthoringComplete': ready, 'assessmentPerformed': False, 'status': status})
                body = (f'[Back to SFS {sfs}](/catalog/sets/{sfs})\n\n**Status:** ' + status
                        + '\n\nThis is a generated authoring draft. Human approval and operational '
                        'assessment results have not been supplied.\n\n## Question\n\n'
                        + mdx(record['question']['shortBinaryQuestion'])
                        + '\n\n## Plain-language explanation\n\n' + mdx(record['question']['plain'])
                        + '\n\n## Assessment criterion and evidence\n\n'
                        + mdx(record['question']['technical-short']))
                if key in pending:
                    body += '\n\n## Pending review\n\n' + mdx(pending[key].get('reason', 'Review pending'))
                if ready and solution_name in checked:
                    solution = read(work / solution_name)
                    body += '\n\n## Proposed implementation\n\nNo infrastructure changes were executed.\n\n'
                    for field in ('desiredEffect', 'environment', 'resourceBasis', 'prerequisites',
                                  'steps', 'verification', 'alternatives', 'versions', 'rollback'):
                        value = solution[field]
                        body += '### ' + field + '\n\n'
                        body += ('\n'.join('- ' + mdx(item) for item in value) if isinstance(value, list)
                                 else mdx(value)) + '\n\n'
                body += (f'\n\n[View the JSON record](https://github.com/stefanospetsios/uniq2me-ai/'
                         f'blob/{branch}/{record_name}).\n\nAssessment result: unset.\n')
                title = f'SFS {sfs} / SF {sf_id}'
                files[f'pages/catalog/sets/{sfs}/{sf_id}.{locale}.mdx'] = page(
                    title, record['question']['shortBinaryQuestion'], body).encode()
        intro = (f'Run `{state["runId"]}` · latest published phase: `{stage}`.\n\n'
                 f'**{256 - len(pending)} model-reviewed questions; {len(pending)} pending positions.** '
                 'Full-roster review is ' + ('pending.' if state.get('catalogRosterPending') else 'complete.')
                 + '\n\nThis is a draft catalog, not an operational assessment or a human-approved release. '
                 + ('External source verification is disabled for this run.\n\n'
                    if state.get('ragMode') == 'disabled' else '\n\n')
                 + '[Browse all 16 Scoring Factor Sets](/catalog/sets).\n\n'
                 + 'Each set contains sixteen positions, labelled 0–F. Open an SF to read its '
                 'question, evidence contract and current completion status. Solutions appear only '
                 'after their publication gate passes.\n\n'
                 + '[Requirement masks](/catalog/masks) · [Methodology](/methodology)\n')
        files[f'pages/catalog/index.{locale}.mdx'] = page('Qwen3 question catalog', 'Generated draft catalog and publication status', intro).encode()
        files[f'pages/catalog/sets.{locale}.mdx'] = page('The 16 Scoring Factor Sets',
            'Fixed topic allocation; sixteen question positions in each set',
            '[Catalog status](/catalog)\n\n' + '\n'.join(area_links)).encode()
        files[f'pages/catalog/sfs.{locale}.mdx'] = page('Scoring Factor Sets',
            'The fixed areas of interest in this catalog',
            'An SFS groups sixteen independently assessed question positions in a fixed area of interest. '
            'Set identifiers run from 0 to F. A complete assessed set is encoded as four hexadecimal '
            'digits; its definition does not establish an assessment result.\n\n'
            '[Browse the fixed areas](/catalog/sets) · [Encoding](/methodology/encoding)').encode()
        files[f'pages/catalog/sf.{locale}.mdx'] = page('Scoring Factors',
            'One bounded yes/no question with an evidence contract',
            'An SF is one question within a Scoring Factor Set. Its contract identifies the actor, '
            'system boundary, evidence period and the evidence required to decide the criterion. '
            'Missing evidence remains pending. Model review of an authoring draft does not establish '
            'human approval or an operational pass/fail result.\n\n'
            '[Browse questions](/catalog/sets) · [Assessment](/methodology/assessment)').encode()
        mask_state = ('The mask publication phase has completed; inspect individual draft records '
                      'for coverage, deferred status and limitations.' if 'masks' in stages
                      else 'Requirement masks have not been published for this catalog phase.')
        files[f'pages/catalog/masks/index.{locale}.mdx'] = page('Requirement masks',
            'Catalog-bound selections and publication status',
            mask_state + '\n\nMasks select question positions and bind to a specific catalog digest. '
            'A draft selection does not establish compliance. No completed assessment profile '
            'has been supplied by this run.\n\n[Mask methodology](/methodology/masks) · '
            '[Catalog masks](/masks/catalogs) · [Set masks](/masks/sfs) · [Factor masks](/masks/factors)').encode()
        files[f'pages/questions/index.{locale}.mdx'] = page('Questions',
            'Open the current question catalog', '[Catalog status](/catalog)\n\n' + '\n'.join(area_links)).encode()
        files[f'pages/sfs.{locale}.mdx'] = page('Scoring Factor Sets',
            'Open the sixteen fixed areas', '[Browse all sets](/catalog/sets)').encode()
        put(f'pages/catalog/_meta.{locale}.json', {
            'index': 'Catalog status', 'sfs': 'What is an SFS?', 'sf': 'What is an SF?',
            'sets': 'The 16 sets', 'masks': 'Requirement masks'})
        put(f'pages/questions/_meta.{locale}.json', {'index': 'Questions', **{sfs: 'SFS ' + sfs for sfs in HEX}})
    for directory in ('schemas', 'repo-signer', 'repo-sfs-masks/templates'):
        for source in sorted((work / 'inputs' / directory).rglob('*')):
            if source.is_file():
                raw(source.relative_to(work / 'inputs').as_posix(), source)
    common = work / 'inputs/json/common.json'
    if common.exists():
        raw('json/common.json', common)
    ignore = work / 'inputs/.gitignore'
    if ignore.exists():
        raw('.gitignore', ignore)
    put('site-publication.json', {'recordType': 'catalog-site-view', 'runId': state['runId'],
        'stage': stage, 'languages': languages, 'branch': branch, 'ragMode': state.get('ragMode', 'repo-web'),
        'questionCount': 256 - len(pending), 'pendingQuestionCount': len(pending),
        'rosterReviewPassed': not bool(state.get('catalogRosterPending')),
        'humanApproved': False, 'assessmentPerformed': False, 'acceptedKnowledge': False,
        'sourceControllerCommit': state['controllerCommit'], 'inputCommit': state['inputCommit']})
    return files


def write_view(destination, files):
    destination = Path(destination)
    for name, data in files.items():
        target = destination / name
        require(not target.is_symlink() and destination.resolve() in target.resolve().parents,
                'Unsafe website destination')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def template_skeleton(repository, commit):
    """Recover optional shared data omitted by older checkpoints, at their exact input pin."""
    from ci.pipeline import command
    import posixpath
    files, entries = {}, {}
    listing = command(['git', 'ls-tree', '-r', commit], repository).stdout.splitlines()
    for row in listing:
        metadata, name = row.split('\t', 1)
        entries[name] = metadata.split()[0]

    def contents(name, seen=None):
        seen = set() if seen is None else seen
        require(name not in seen and name in entries, 'Invalid template data alias: ' + name)
        seen.add(name)
        value = command(['git', 'show', commit + ':' + name], repository).stdout
        if entries[name] == '120000':
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), value))
            require(not value.startswith('/') and target.startswith('repo-signer/'),
                    'Template signer alias escapes its data directory')
            return contents(target, seen)
        require(entries[name] in ('100644', '100755'), 'Template data must be a regular file')
        return value.encode()

    for name in entries:
        if name in ('.gitignore', 'json/common.json') or name.startswith((
                'repo-signer/', 'repo-sfs-masks/templates/', 'schemas/')):
            files[name] = contents(name)
    return files


def verify_checkpoint(root):
    from ci.contracts import file_manifest
    root = Path(root)
    manifest = read(root / 'checkpoint-manifest.json')
    require(manifest.get('recordType') == 'ci-checkpoint', 'Invalid checkpoint record')
    actual = file_manifest(root)
    actual.pop('checkpoint-manifest.json', None)
    require(actual == manifest['files'], 'Checkpoint file hashes do not match')
    return read(root / 'controller-state.json')
