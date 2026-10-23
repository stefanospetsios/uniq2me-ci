"""Explicit aN taxonomy drafting; original caller and reference bytes remain pinned."""
import copy
import json
import re
import shutil
from pathlib import Path

from ci.contracts import (HEX, digest, file_manifest, read, require, text, validate_review, write)
from ci.progress import log
from ci.runtime import utc
from ci.timeouts import budget_seconds, optional_seconds

CHECKS = ['sixteen-distinct-areas', 'domain-coverage', 'scope-boundaries', 'assessment-honesty']


def validate_taxonomy(value, framework=None):
    require(isinstance(value, dict) and set(value) == {'areas', 'rationale', 'limitations', 'unresolved'},
            'Return areas, rationale, limitations and unresolved')
    require(isinstance(value['areas'], list) and [area.get('id') for area in value['areas']] == list(HEX),
            'aN requires exactly sixteen ordered areas 0-F')
    titles = []
    for area in value['areas']:
        keys = {'id', 'title', 'summary', 'boundaries', 'keywords'}
        require(set(area) == keys | ({'articles', 'sourceIds'} if framework == 'dora' else set()), 'Wrong area proposal shape')
        if framework == 'dora':
            from ci.dora import load_profile
            source_ids = {s['id'] for s in load_profile()['sources']}
            require(isinstance(area['sourceIds'], list) and 'dora' in area['sourceIds']
                    and all(isinstance(s, str) and s in source_ids for s in area['sourceIds']),
                    'DORA areas must retain the primary regulation and registered source IDs')
            require(isinstance(area['articles'], list) and bool(area['articles']), 'DORA areas need article references')
            for article in area['articles']:
                require(isinstance(article, str) and bool(re.fullmatch(r'[1-9][0-9]?(?:-[1-9][0-9]?)?', article)),
                        'Use DORA article numbers or ranges')
                numbers = [int(n) for n in article.split('-')]
                require(all(1 <= n <= 64 for n in numbers) and numbers[0] <= numbers[-1], 'Invalid DORA article range')
        for field in ('title', 'summary', 'boundaries'):
            text(area[field], 'area.' + field)
        require(len(area['title']) <= 160 and '\n' not in area['title'], 'Area title must fit one concise line')
        titles.append(area['title'].strip().casefold())
        require(isinstance(area['keywords'], list) and area['keywords'], 'Area needs research keywords')
        for keyword in area['keywords']:
            text(keyword, 'area keyword')
    require(len(set(titles)) == 16, 'Taxonomy contains duplicate areas')
    text(value['rationale'], 'taxonomy rationale')
    require(isinstance(value['limitations'], list), 'Taxonomy limitations must be recorded')
    for limitation in value['limitations']:
        text(limitation, 'taxonomy limitation')
    require(value['unresolved'] == [], 'Taxonomy has unresolved drafting findings')


def synthetic_taxonomy():
    return {'areas': [{'id': sfs, 'title': 'SYNTHETIC proposed area ' + sfs,
        'summary': 'SYNTHETIC scope for area ' + sfs, 'boundaries': 'SYNTHETIC boundary ' + sfs,
        'keywords': ['synthetic topic ' + sfs]} for sfs in HEX],
        'rationale': 'SYNTHETIC taxonomy fixture; no real domain analysis',
        'limitations': ['SYNTHETIC fixture; no expert or model review'], 'unresolved': []}


def empty_signers(value):
    if isinstance(value, dict):
        return {key: empty_signers(child) if isinstance(child, (dict, list))
                else False if key == 'isBiased' else None for key, child in value.items()}
    return [empty_signers(child) for child in value]


def recover_seed(pipeline):
    p = pipeline
    intent = p.state.get('taxonomySeedPending')
    if not intent:
        return
    require('frozenManifest' not in p.state and not p.state.get('factorGates') and not p.state.get('publications'),
            'Cannot roll back a taxonomy after source admission or SF publication')
    for name, manifest in (('base', p.state['baseCatalogManifest']), ('agent-rules', p.state['rulesManifest'])):
        backup = p.root / 'taxonomy-backup' / name
        require(file_manifest(backup) == manifest, 'Taxonomy recovery backup changed: ' + name)
    record = read(p.root / 'taxonomy-reviewed.json')
    require(digest(record) == intent['recordDigest'], 'Reviewed taxonomy recovery record changed')
    for name in ('base', 'agent-rules'):
        shutil.rmtree(p.work / name)
        shutil.copytree(p.root / 'taxonomy-backup' / name, p.work / name)
    p.state.pop('taxonomySeedPending')
    p.save()
    log('taxonomy', 'Recovered the pinned baseline before resuming aN seeding')


def seed(pipeline, proposal, review, model_record=None):
    p = pipeline
    base = p.work / 'base'
    for language in ('en', 'fr', 'el', 'de'):
        for path in sorted((base / 'repo-sfs-masks' / language).glob('*.json')):
            for root in (base, p.work):
                write(root / 'catalog/prior-masks' / language / path.name, read(path))
    # Reference snapshots and inputs retain the original catalog. This derived
    # baseline is a new draft taxonomy and cannot inherit old question answers.
    for directory in ('solutions', 'catalog/questions', 'catalog/taxonomy', 'evidence-rules',
                      'translations', 'repo-sfs-masks', 'mask-coverage'):
        for root in (base, p.work):
            target = root / directory
            if target.exists():
                shutil.rmtree(target)
    for language in ('fr', 'el', 'de'):
        for directory in ('json', 'repo-sf'):
            for root in (base, p.work):
                target = root / directory / language
                if target.exists():
                    shutil.rmtree(target)
    for area in proposal['areas']:
        relative = Path('json/en') / (area['id'] + '.json')
        current = read(base / relative)
        current.update(keyTitle=area['title'], keyShortInfo=area['summary'],
            keyDescription=area['summary'] + ' ' + area['boundaries'], keyWords=', '.join(area['keywords']),
            promptAi='Develop sixteen bounded yes/no factors for ' + area['title'] + '. ' + area['boundaries'] +
                ' Define scope, evidence period and decision rules; preserve pending evidence.', resultScore=None)
        current['meta'].update(title=area['title'], menuTitle=area['title'])
        for entry in current['set']:
            entry['score'] = None
            entry['signer'] = empty_signers(entry['signer'])
        for root in (base, p.work):
            write(root / relative, current)
        for slot in (slot for slot in p.state['slots'] if slot['sfsId'] == area['id']):
            relative = Path('repo-sf/en') / (slot['repoId'] + '.json')
            sf = read(base / relative)
            sf['question'].update(shortBinaryQuestion='[Placeholder] Has condition ' + slot['sfId'] +
                ' in ' + area['title'] + ' been evidenced?', plain='New-taxonomy authoring placeholder.',
                **{'technical-short': 'Define this criterion using independently admitted topic sources.',
                   'keywords': current['keyWords'], 'promptAiOptimization': current['promptAi']})
            sf.update(isPlaceholder=True, resultScore=None, signatures=empty_signers(sf['signatures']),
                review={'status': 'draft', 'approvals': []}, clarificationQuestion=[], wikiLinks=[], openSourceApproach=None)
            sf['meta']['title'] = 'SFS ' + area['id'] + ' / SF ' + slot['sfId'] + ' (placeholder)'
            for field, operation in [('editLink', 'edit'), ('jsonLink', 'blob')]:
                sf[field] = p.link(operation, relative.as_posix())
            for field in ('glacierWiki', 'glacierLink', 'glacerQRcode', 'linkLatest'):
                sf[field] = '' if isinstance(sf[field], str) else None
            for root in (base, p.work):
                write(root / relative, sf)
    model_record = model_record or p.state.get('model', {'tag': p.state['refinementPlan']['models']['a']})
    model = {key: model_record.get(key) for key in ('tag', 'digest', 'effectiveContext')}
    marker = {'schemaVersion': 1, 'recordType': 'catalog-profile', 'frameworkId': 'air',
        'methodologyId': 'uniq2me', 'profileId': 'air-generated-taxonomy', 'profileVersion': 1,
        'taxonomyDigest': digest(proposal), 'sfsCount': 16, 'sfPerSfs': 16,
        'taxonomyIsOfficial': False, 'status': 'draft-authoring-taxonomy',
        'sourceCatalog': copy.deepcopy(p.state['baseCatalog']), 'generatorModel': model,
        'humanApproved': False, 'assessmentPerformed': False}
    if p.state.get('catalogProfile', {}).get('frameworkId') == 'dora':
        marker = p.seed_generated_taxonomy(proposal)
    for root in (base, p.work):
        write(root / 'catalog-profile.json', marker)
        write(root / 'catalog/taxonomy/areas.json', proposal)
        write(root / 'catalog/taxonomy/review.json', review)
    p.state['catalogProfile'] = marker
    p.state['taxonomyGeneration'] = {'status': 'checked-draft', 'proposalDigest': digest(proposal),
                                   'model': model, 'recordedAt': utc()}
    p.state['baseCatalog']['derivedTaxonomyDigest'] = digest(proposal)
    p.state['baseCatalogManifest'] = file_manifest(base)
    config = read(p.work / 'agent-rules/run-config.json')
    config['catalogProfile'] = marker
    config['catalog']['areasRef'] = 'base/json/en'
    config['inputs']['catalog']['digest'] = digest(p.state['baseCatalogManifest'])
    write(p.work / 'agent-rules/run-config.json', config)
    p.state['rulesManifest'] = file_manifest(p.work / 'agent-rules')
    p.state.pop('taxonomySeedPending', None)
    p.save()


def ensure_taxonomy(pipeline):
    p = pipeline
    plan = p.state['refinementPlan']
    require(plan.get('taxonomyMode') == 'regenerate' and plan['sfs'] == list(HEX), 'Only aN can replace the taxonomy')
    framework = p.state.get('catalogProfile', {}).get('frameworkId')
    if framework == 'dora':
        from ci.dora import DoraPipeline
        require(isinstance(p, DoraPipeline), 'AIR aN cannot replace a bound DORA taxonomy')
        p.source_profile()  # Verify the regulatory foundation before deriving areas.
    else:
        require(framework in (None, 'air'), 'aN cannot change the catalog framework')
    previous = p.state.get('taxonomyGeneration')
    if previous:
        proposal = read(p.work / 'catalog/taxonomy/areas.json')
        validate_taxonomy(proposal, framework)
        require(previous['proposalDigest'] == digest(proposal), 'Checked taxonomy proposal changed')
        return
    require('frozenManifest' not in p.state, 'Taxonomy must be fixed before source admission')
    context = digest({'base': p.state['baseCatalogManifest'], 'rules': p.state['rulesManifest'],
                      'focus': plan['catalogBrief'], 'model': plan['models'][plan['tasks']['rework_questions']['model']]})
    cached = p.root / 'taxonomy-reviewed.json'
    prompt = ('Propose a new classification for the user catalog focus using exactly sixteen areas, IDs 0-F. '
        'This is a draft authoring taxonomy, not an official classification or legal conclusion. '
        'Keep the UNIQ2Me sixteen-by-sixteen layout and current JSON schema. Each area will later receive sixteen '
        'separately source-checked questions. Choose distinct, comprehensive areas with explicit boundaries and '
        'research keywords. Return {areas:[{id,title,summary,boundaries,keywords:[strings]}],rationale,limitations:[strings],unresolved:[]}. '
        'Do not invent legal sources, approval or assessment results. Source discovery follows this drafting step. '
        'User catalog focus: ' + plan['catalogBrief'])
    if framework == 'dora':
        profile = p.source_profile()
        prompt += ('\nDORA is fixed: regenerate only the authored DORA area allocation, never the framework, '
            'regulatory foundation or applicability policy. Each area additionally requires '
            'articles:[article numbers or ranges within 1-64] and sourceIds:[registered IDs including dora]. '
            'These are provisional research mappings, not verified legal conclusions. Preserve entity roles '
            'and proportionality. Regulatory foundation and prior areas (data): ' + json.dumps(profile, ensure_ascii=False))
    if cached.exists():
        record = read(cached)
        require(record['contextDigest'] == context, 'Reviewed taxonomy inputs changed')
        proposal, review = record['proposal'], record['review']
    elif p.state.get('dryRun'):
        from ci.dry_run import synthetic_review
        proposal = synthetic_taxonomy()
        if framework == 'dora':
            for area, original in zip(proposal['areas'], p.source_profile()['areas']):
                area.update(articles=original['articles'], sourceIds=original['sourceIds'])
                area['title'] = 'SYNTHETIC DORA proposed area ' + area['id']
        review = synthetic_review(proposal, CHECKS)
        # The fixture has no real legal or model evidence.
        for row in review['checks']:
            row['sourceRefs'] = []
    else:
        require(p.state.get('runnerPreflight', {}).get('status') == 'passed', 'aN needs a passed runner preflight')
        p.state['activeModelLabel'] = plan['tasks']['rework_questions']['model']
        p.state['activeTask'] = 'taxonomy-draft'
        p.save()
        p.ensure_live_model(optional_seconds(budget_seconds(p.state['timeoutsMinutes']['SOURCE_MINUTES'])))
        feedback = ''
        with p.server('taxonomy') as server:
            for attempt in range(1, p.state['maximumAttempts'] + 1):
                try:
                    proposal = server.ask('catalog-coordinator', prompt + feedback,
                        optional_seconds(budget_seconds(p.state['timeoutsMinutes']['SOURCE_MINUTES'])), 'taxonomy-author-' + str(attempt))
                    validate_taxonomy(proposal, framework)
                    review = server.ask('catalog-validator', 'Review this proposed taxonomy against the user focus. '
                        'Return artifactDigest, decision accepted|rejected, checks:[{ruleId,passed,rationale,sourceRefs:[]}], '
                        'unresolved:[]. This is only an authoring classification review; no sources were admitted yet. '
                        + json.dumps({'artifact': proposal, 'artifactDigest': digest(proposal), 'requiredChecks': CHECKS,
                                      'catalogFocus': plan['catalogBrief']}, ensure_ascii=False),
                        optional_seconds(budget_seconds(p.state['timeoutsMinutes']['REVIEW_MINUTES'])), 'taxonomy-review-' + str(attempt))
                    validate_review(review, proposal, CHECKS, {}, allow_unsourced=True)
                    break
                except ValueError as error:
                    feedback = '\nRepair this observed structural/review finding: ' + str(error)
            else:
                raise ValueError('aN taxonomy drafting failed bounded review: ' + feedback)
    validate_taxonomy(proposal, framework)
    validate_review(review, proposal, CHECKS, {}, allow_unsourced=True)
    if not cached.exists():
        write(cached, {'contextDigest': context, 'proposal': proposal, 'review': review,
                       'model': {key: p.state.get('model', {}).get(key) for key in ('tag', 'digest', 'effectiveContext')}})
    for name, expected in (('base', p.state['baseCatalogManifest']), ('agent-rules', p.state['rulesManifest'])):
        backup = p.root / 'taxonomy-backup' / name
        if not backup.exists():
            shutil.copytree(p.work / name, backup)
        require(file_manifest(backup) == expected, 'Taxonomy recovery backup differs from its pinned baseline')
    p.state['taxonomySeedPending'] = {'recordDigest': digest(read(cached))}
    p.save()
    seed(p, proposal, review, read(cached)['model'])
    p.state.pop('activeTask', None)
    p.save()
    log('taxonomy', 'aN fixed sixteen new draft areas; source research and SF authoring follow')
