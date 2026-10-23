"""Requested, source-linked draft masks bound to final refinement snapshots."""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import (HEX, MASK_CHECKS, TRANSLATION_CHECKS, byte_digest, catalog_digest,
                          expand_mask, read, require, validate_masks, write)
from ci.pipeline import CandidateAttemptsExhausted, CatalogReviewExhausted, CatalogTimeout
from ci.progress import log
from ci.refinement import configured_pipeline
from ci.runtime import utc


def validate_translation(value, original):
    require({m['id'] for m in value['masks']} == {m['id'] for m in original['masks']}, 'Mask IDs changed in translation')
    for mask in value['masks']:
        source = next(m for m in original['masks'] if m['id'] == mask['id'])
        require(mask['selection'] == source['selection'], 'Translated mask bits changed')
        require(mask['references'] == source['references'], 'Translated mask references changed')
    for coverage in value['coverage']:
        source = next(c for c in original['coverage'] if c['maskId'] == coverage['maskId'])
        for key in ('jurisdiction', 'instrument'):
            require(coverage[key] == source[key], 'Translated mask ' + key + ' changed')
        require(len(coverage['mappings']) == len(source['mappings']), 'Translation dropped a mapping')
        for target, old in zip(coverage['mappings'], source['mappings']):
            for key in ('article', 'sourceRefs', 'positions'):
                require(target[key] == old[key], 'Translated mapping ' + key + ' changed')
        require(len(coverage['uncoveredObligations']) == len(source['uncoveredObligations']),
                'Translation dropped uncovered obligations')


def synthetic_package(pipeline, binding, count, language='en'):
    from ci.dry_run import masks
    value = masks(binding, language)
    result = {'masks': [], 'coverage': []}
    for index in range(count):
        mask = copy.deepcopy(value['masks'][index % len(value['masks'])])
        coverage = copy.deepcopy(value['coverage'][index % len(value['coverage'])])
        mask['id'] = coverage['maskId'] = 'synthetic-example-' + str(index + 1)
        result['masks'].append(mask)
        result['coverage'].append(coverage)
    return result


def generate_masks(pipeline):
    p = pipeline
    request = p.state['refinementPlan'].get('maskRequest')
    if not request:
        log('refine_masks', 'M not selected; no mask generation')
        return
    data = p.state.get('incremental', {})
    require({'primary', *p.state['refinementPlan'].get('retryModels', [])} <= set(data.get('completedPasses', [])),
            'Finish all SF task/model passes before creating masks')
    # Annotations change full-record digests; finish them BEFORE binding masks.
    p.annotate_non_validated()
    p.save()
    sources = p.frozen_sources()
    schema = read(p.work / 'inputs/schemas/mask.schema.json')
    prior_masks = [read(path) for path in sorted((p.work / 'base/repo-sfs-masks/en').glob('*.json'))]
    if not prior_masks:
        prior_masks = [read(path) for path in sorted((p.work / 'base/catalog/prior-masks/en').glob('*.json'))
                       if path.name != 'coverage.json']
    original_root = p.work / p.state['baseCatalog'].get('root', 'base')
    require(original_root.resolve().is_relative_to(p.work.resolve()), 'Prior catalog path escapes the pinned work')
    prior_questions = []
    selected_prior = set()
    for mask in prior_masks:
        fields = expand_mask(mask['selection'])
        selected_prior |= {(sfs, sf) for sfs in HEX for sf in HEX
                           if int(fields[sfs], 16) & (1 << (15 - int(sf, 16)))}
    for slot in p.state['slots']:
        if (slot['sfsId'], slot['sfId']) not in selected_prior:
            continue
        path = original_root / 'repo-sf/en' / (slot['repoId'] + '.json')
        if path.exists():
            prior_questions.append({'identity': slot, 'question': read(path)['question']['shortBinaryQuestion']})
    # Keep original stakeholder records and their old bindings as history.
    # Only newly checked/rebound records enter the current compatible library.
    for language in ('en', 'fr', 'el', 'de'):
        for path in sorted((p.work / 'base/repo-sfs-masks' / language).glob('*.json')):
            write(p.work / 'catalog/prior-masks' / language / path.name, read(path))
        coverage = p.work / 'base/mask-coverage' / (language + '.json')
        if coverage.exists():
            write(p.work / 'catalog/prior-masks' / language / 'coverage.json', read(coverage))
    state = p.state.setdefault('maskGeneration', {'requestedCount': request['count'], 'languages': {}})
    requested_languages = ['en', *[lang for lang in ('fr', 'el', 'de') if p.task_enabled('translate_' + lang)]]
    blocked = {key for key, pending in p.state.get('refinementPending', {}).items()
               if 'rework_questions' in pending.get('tasks', [])}
    blocked |= {slot['repoId'] for slot in p.state['slots'] if slot['sfsId'] in p.state.get('rosterPending', [])}
    questions = []
    for slot in p.state['slots']:
        sf = read(p.work / 'repo-sf/en' / (slot['repoId'] + '.json'))
        if sf.get('isPlaceholder'):
            blocked.add(slot['repoId'])
        path = p.work / 'catalog/questions/en' / (slot['repoId'] + '.json')
        # AI research prompts and solution prose can be very large. Masks bind
        # to the complete records, but select criteria using their contracts.
        questions.append({'identity': slot, 'question': sf['question']['shortBinaryQuestion'],
            'contract': read(path).get('contract') if path.exists() else None,
            'selectable': slot['repoId'] not in blocked})

    def save_status(language, status, reason='', files=None, binding=None):
        value = {'status': status, 'requestedCount': request['count'],
                 'generatedMaskCount': request['count'] if status == 'checked-draft' else 0,
                 'language': language, 'reason': reason, 'catalogDigest': binding,
                 'humanApproved': False, 'assessmentPerformed': False, 'recordedAt': utc()}
        write(p.work / 'mask-status' / (language + '.json'), value)
        state['languages'][language] = {**value, 'files': files or {}}
        p.save()

    def package(language):
        return {'masks': [read(path) for path in sorted((p.work / 'repo-sfs-masks' / language).glob('*.json'))],
                'coverage': read(p.work / 'mask-coverage' / (language + '.json'))}

    def model_labels(primary):
        labels, seen = [], set()
        for label in (primary, *p.state['refinementPlan'].get('retryModels', [])):
            tag = p.state['refinementPlan']['models'][label]
            if tag not in seen:
                labels.append(label)
                seen.add(tag)
        return labels

    # Finish the English mask package with M's model, then use T's model for
    # every selected metadata translation. Retry models follow each primary pass.
    primary = request['model']
    routes = [(label, 'en', 'create_masks', primary) for label in model_labels(primary)]
    primaries = [p.state['refinementPlan']['tasks']['translate_' + language]['model']
                 for language in requested_languages[1:]]
    for label in dict.fromkeys([*primaries, *p.state['refinementPlan'].get('retryModels', [])]):
        for language in requested_languages[1:]:
            task = 'translate_' + language
            primary = p.state['refinementPlan']['tasks'][task]['model']
            if label in model_labels(primary):
                routes.append((label, language, task, primary))
    for label, language, task, primary in routes:
        checked = state['languages'].get(language, {})
        if checked.get('status') == 'checked-draft':
            require(all(byte_digest(p.work / name) == checksum for name, checksum in checked['files'].items()),
                    'Checked mask bytes changed')
            require(catalog_digest(p.work, language) == checked['catalogDigest'], 'Checked mask catalog changed')
            continue
        if language != 'en' and state['languages'].get('en', {}).get('status') != 'checked-draft':
            save_status(language, 'pending-english-masks', 'English masks have not passed their checks')
            continue
        if p.state.get('pendingSourceScopes', {}).get('legal'):
            save_status(language, 'pending-research', 'The requested legal source scope remains pending')
            continue
        if len(blocked) == 256:
            save_status(language, 'pending-catalog', 'No checked question roster is available for selection')
            continue
        try:
            binding = catalog_digest(p.work, language)
        except FileNotFoundError:
            save_status(language, 'pending-translation', 'Complete the language catalog before binding translated masks')
            continue
        original = package('en') if language != 'en' else None
        prompt = ('Create exactly ' + str(request['count']) + ' distinct, useful draft requirement masks. '
            'Return exactly {"masks":[mask-schema records],"coverage":[article-linked mappings]}. '
            'Use status=draft and catalog.edition=null. Every selected bit needs a source-supported mapping. '
            'SF 0 is the most significant bit and SF F the least significant bit; preserve leading zeros. '
            'Keep instruments, versions, dates, jurisdictions, entity scope, sector assumptions, '
            'applicability limits and uncovered obligations explicit. Language does not determine jurisdiction. '
            'Do not treat draft legislation, proposals or working guidance as enacted law. '
            'Do not select a question marked selectable=false. Use only admitted source URLs/passages. '
                'When updating a prior mask, preserve its ID and stakeholder purpose, and rebind by the '
                'meaning of the current questions rather than copying old bit positions. Prior records are '
                'historical data; never carry forward old reviewed status as approval of this update. '
                'Do not invent signatures, human approval or operational compliance. '
            'A useful partial mapping must explicitly record its uncovered obligations.\n'
            'User instructions (subordinate to the immutable schema, sources and catalog): '
            + request['instructions'] + '\nData: ' + json.dumps({'language': language,
                'catalogDigest': binding, 'schema': schema, 'catalog': questions,
                'coverageExample': {'maskId': 'example', 'instrument': 'Exact instrument identifier/version',
                    'jurisdiction': 'EU|GR|FR', 'entityScope': 'Entity and sector',
                    'applicabilityAssumptions': 'Explicit applicability assumptions and status of national text',
                    'uncoveredObligations': ['Known scope gap'], 'unresolved': [],
                    'mappings': [{'article': 'Exact article identifier', 'rationale': 'Selection rationale',
                        'sourceRefs': [{'sourceId': 'admitted-source-id', 'passageId': 'admitted-passage-id'}],
                        'positions': [{'sfsId': '0', 'sfId': '0'}]}]},
                    'englishPackage': original, 'priorMasks': prior_masks if language == 'en' else None,
                    'priorQuestionDefinitions': prior_questions if language == 'en' else None}, ensure_ascii=False))
        if original:
            prompt += ('\nTranslate only metadata and coverage explanations into ' + language + '. '
                'Preserve every English mask ID, selection, reference, jurisdiction, instrument, article, '
                'mapping position, source reference and gap. Set both languages and use the supplied '
                'target-language catalog digest. Do not change selection based on translation.')

        def validate(value):
            validate_masks(value, schema, language, binding, sources, allow_unsourced=p.allow_unsourced())
            require(len(value['masks']) == request['count'], 'Return exactly the requested number of masks')
            for mask in value['masks']:
                selected = expand_mask(mask['selection'])
                require(not any(int(selected[slot['sfsId']], 16) & (1 << (15 - int(slot['sfId'], 16)))
                                for slot in p.state['slots'] if slot['repoId'] in blocked),
                        'A mask selected an unchecked/pending question')
            if original:
                validate_translation(value, original)

        p.state['refinementPassModel'] = 'primary' if label == primary else label
        p.activate(task)
        try:
            synthetic = synthetic_package(p, binding, request['count'], language)
            if original:
                synthetic = copy.deepcopy(original)
                for mask in synthetic['masks']:
                    mask['language'] = mask['catalog']['language'] = language
                    mask['catalog']['digest'] = binding
            with p.server('refine-masks-' + language) as server:
                value = p.task(server, task, 'masks-' + language, prompt, validate,
                    MASK_CHECKS + (TRANSLATION_CHECKS if original else []), synthetic)
        except (CandidateAttemptsExhausted, CatalogReviewExhausted, CatalogTimeout) as error:
            save_status(language, 'pending-validation', str(error), binding=binding)
            log('refine_masks', language + ': deferred after model ' + label)
            continue
        p.write_masks(value, language)
        names = ['repo-sfs-masks/' + language + '/' + mask['id'] + '.json' for mask in value['masks']]
        names.append('mask-coverage/' + language + '.json')
        files = {name: byte_digest(p.work / name) for name in names}
        save_status(language, 'checked-draft', files=files, binding=binding)
        log('refine_masks', language + ': ' + str(len(value['masks'])) + ' source-linked draft masks checked')
    state['completedAt'] = utc()
    p.save()


def main():
    pipeline = configured_pipeline()
    pipeline.pinned()
    generate_masks(pipeline)


if __name__ == '__main__':
    main()
