"""Deterministic gates. These check records and bindings, not independent truth."""
import hashlib
import json
import re
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

HEX = '0123456789ABCDEF'
LANGUAGES = ('en', 'fr', 'el')
QUESTION_CHECKS = ['source-support', 'single-condition', 'nontechnical-wording',
                   'scope-and-evidence', 'concern-coverage', 'fixture-decisions']
SOLUTION_CHECKS = ['source-support', 'unchanged-contract', 'implementable-route',
                   'alternatives', 'rollback-and-verification', 'execution-honesty']
FIELD_CHECKS = ['source-support', 'unchanged-contract', 'complete-fields',
                'cross-field-consistency', 'fixture-decisions', 'assessment-honesty']
TRANSLATION_CHECKS = ['source-target-equivalence', 'actors-and-negation',
                      'thresholds-periods-exceptions', 'protected-literals',
                      'fixture-decisions', 'assessment-honesty']
# These checks use the bound candidate, pinned task inputs and fixtures. Any
# citations they do supply must still resolve to real frozen passages.
CANDIDATE_REVIEW_CHECKS = frozenset((
    'single-condition', 'nontechnical-wording', 'fixture-decisions',
    'unchanged-contract', 'execution-honesty', 'complete-fields',
    'cross-field-consistency', 'assessment-honesty',
    'source-target-equivalence', 'actors-and-negation', 'thresholds-periods-exceptions',
    'protected-literals', 'catalog-binding', 'bit-order', 'draft-status',
    'fixed-area-boundaries', 'no-duplicate-criteria', 'no-contradictions'))

MASK_CHECKS = ['legal-source-support', 'entity-and-jurisdiction', 'article-mapping',
               'catalog-binding', 'bit-order', 'uncovered-obligations', 'draft-status']


class ReviewRejected(ValueError):
    """A bound model review reported semantic defects; preserve its actual feedback."""
    def __init__(self, review):
        self.details = {'decision': review.get('decision'), 'unresolved': review.get('unresolved'),
            'failedChecks': [{k: row.get(k) for k in ('ruleId', 'rationale', 'sourceRefs')}
                for row in review.get('checks', []) if isinstance(row, dict) and row.get('passed') is not True]}
        super().__init__('Review rejected or left unresolved findings: ' +
                         json.dumps(self.details, ensure_ascii=False))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    # Matches JSON.stringify(canonical(...)) used by uniq2me-pages for catalogs
    # containing strings, booleans, nulls and integers (no floating-point fields).
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical(value)).hexdigest()


def byte_digest(path):
    return 'sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
                         + '\n', encoding='utf-8')
    temporary.replace(path)


def text(value, label):
    require(isinstance(value, str) and bool(value.strip()), f'{label} must be filled')
    require(not re.search(r'\[placeholder\]|\bTODO\b|\bTBD\b', value, re.I),
            f'{label} contains placeholder text')


def identity(value, expected, language='en'):
    require(value == {**expected, 'language': language}, 'Identity or language changed')


def refs(values, sources, *, allow_empty=False):
    require(isinstance(values, list) and (values or allow_empty), 'At least one frozen passage is required')
    for ref in values:
        require(isinstance(ref, dict) and set(ref) == {'sourceId', 'passageId'},
                'Use {sourceId, passageId} references')
        source = sources.get(ref['sourceId'])
        require(source is not None, f'Unknown frozen source: {ref["sourceId"]}')
        require(ref['passageId'] in {p['id'] for p in source['passages']},
                f'Unknown frozen passage: {ref["passageId"]}')


def validate_question(value, expected, sources, concerns, *, allow_unsourced=False, lineage=None):
    require(isinstance(value, dict), 'Question candidate must be an object')
    errors = []
    def check(condition, message):
        if not condition:
            errors.append(message)
    def inspect(function, *args, path='', **kwargs):
        try:
            function(*args, **kwargs)
        except (ValueError, KeyError, TypeError) as problem:
            errors.append((path + ': ' if path else '') + str(problem))
    check(value.get('recordType') == 'question-candidate',
          'recordType must equal "question-candidate"; received '
          + (json.dumps(value['recordType']) if 'recordType' in value else 'missing'))
    inspect(identity, value.get('identity'), expected)
    question = value.get('shortBinaryQuestion')
    inspect(text, question, 'shortBinaryQuestion')
    if isinstance(question, str):
        check(len(question) <= 512, 'Question exceeds 512 characters')
    contract = value.get('contract')
    check(isinstance(contract, dict), 'contract must be an object')
    contract = contract if isinstance(contract, dict) else {}
    for key in ('actor', 'scope', 'evidencePeriod', 'positive', 'negative', 'missingEvidence'):
        inspect(text, contract.get(key), 'contract.' + key)
    for key in ('requiredEvidence', 'alternatives'):
        items = contract.get(key)
        check(isinstance(items, list) and bool(items), key + ' must be filled: contract.' + key
              + ' requires a nonempty array of substantive strings')
        if isinstance(items, list):
            for index, item in enumerate(items):
                inspect(text, item, key, path=f'contract.{key}[{index}]')
    inspect(refs, value.get('sourceRefs'), sources, allow_empty=allow_unsourced, path='sourceRefs')
    ids = value.get('concernIds')
    valid_ids = isinstance(ids, list) and bool(ids) and all(isinstance(item, str) for item in ids)
    check(valid_ids and len(ids) == len(set(ids)), 'Invalid concern IDs')
    if valid_ids:
        check(set(ids) <= concerns, 'Question maps to an unknown concern: '
                + json.dumps(sorted(set(ids) - concerns)) + '; allowed concern IDs: '
                + json.dumps(sorted(concerns)))
    check(value.get('unresolved') == [], 'Question has unresolved blockers')
    fixtures = value.get('fixtures', [])
    check(isinstance(fixtures, list) and len(fixtures) >= 3, 'Provide three evidence fixtures')
    fixtures = fixtures if isinstance(fixtures, list) else []
    fixture_ids, kinds = [], set()
    outcomes = {'satisfied': 'satisfied', 'failed': 'failed', 'missing': 'pending'}
    for index, fixture in enumerate(fixtures):
        path = f'fixtures[{index}]'
        check(isinstance(fixture, dict), path + ' must be an object')
        if not isinstance(fixture, dict):
            continue
        inspect(text, fixture.get('id'), 'fixture.id', path=path)
        inspect(text, fixture.get('evidence'), 'fixture.evidence', path=path)
        if isinstance(fixture.get('id'), str):
            fixture_ids.append(fixture['id'])
        kind = fixture.get('kind')
        if isinstance(kind, str):
            kinds.add(kind)
        check(isinstance(kind, str) and kind in outcomes, path + ': Invalid fixture kind')
        check(fixture.get('synthetic') is True, path + ': Fixtures must be labeled synthetic')
        check(isinstance(kind, str) and fixture.get('expected') == outcomes.get(kind),
                path + ': Fixture must distinguish pending evidence from binary outcomes')
    check(len(fixture_ids) == len(set(fixture_ids)), 'Duplicate fixture ID')
    check(set(outcomes) <= kinds, 'Missing fixture kinds')
    if lineage is not None:
        parent_digest, semantic_relations = lineage['parentQuestionDigest'], lineage['semanticRelations']
        check('parentQuestionDigest' in value and value['parentQuestionDigest'] == parent_digest,
              'Question parent binding is incorrect: parentQuestionDigest must be explicitly '
              + json.dumps(parent_digest) + '; received '
              + (json.dumps(value['parentQuestionDigest']) if 'parentQuestionDigest' in value else 'missing'))
        check(value.get('semanticRelation') in semantic_relations,
              'Question needs explicit semantic lineage: semanticRelation must be one of '
              + json.dumps(semantic_relations) + '; received '
              + (json.dumps(value['semanticRelation']) if 'semanticRelation' in value else 'missing')
              + '. Compare with the prior accepted position, not a rejected attempt.')
        inspect(text, value.get('changeReason'), 'changeReason')
    require(not errors, 'Question validation failed: ' + '; '.join(errors))


def validate_solution(value, expected, sources, question, language='en', *, allow_unsourced=False):
    require(value.get('recordType') == 'canonical-solution', 'Wrong solution record type')
    identity(value.get('identity'), expected, language)
    require(value.get('questionDigest') == digest(question), 'Solution has a stale question')
    for key in ('desiredEffect', 'environment', 'resourceBasis', 'rollback'):
        text(value.get(key), key)
    for key in ('prerequisites', 'steps', 'verification', 'alternatives', 'versions'):
        require(isinstance(value.get(key), list) and value[key], key + ' is required')
        for item in value[key]:
            text(item, key)
    refs(value.get('sourceRefs'), sources, allow_empty=allow_unsourced)
    require(value.get('executionValidation') == 'not-executed',
            'This authoring job does not execute proposed infrastructure changes')
    require(value.get('unresolved') == [], 'Solution has unresolved blockers')
    require(isinstance(value.get('protectedLiterals'), list), 'Declare protected command/version literals')
    prose = canonical({k: v for k, v in value.items() if k != 'protectedLiterals'}).decode('utf-8')
    for literal in value['protectedLiterals']:
        text(literal, 'protected literal')
        require(json.dumps(literal, ensure_ascii=False)[1:-1] in prose,
                'Protected literal was lost from the actual solution')


def same_shape(value, template, path='record'):
    """Preserve every legacy key/type, including nested metadata and signatures."""
    if isinstance(template, dict):
        require(isinstance(value, dict),
                path + ' must preserve the pinned template keys exactly')
        require(set(value) == set(template),
                path + ' must preserve the pinned template keys exactly; missing: '
                + ', '.join(sorted(set(template) - set(value))) + '; unexpected: '
                + ', '.join(sorted(set(value) - set(template))))
        for key in template:
            same_shape(value[key], template[key], path + '.' + key)
    elif isinstance(template, list):
        require(isinstance(value, list), path + ' must be an array')
    elif template is not None:
        require(type(value) is type(template), path + ' has the wrong type')


def validate_sf(value, template, expected, sources, question, solution, language='en', *, allow_unsourced=False):
    same_shape(value, template)
    identity(value.get('identity'), expected, language)
    require(value.get('schemaVersion') == template['schemaVersion'], 'Schema version changed')
    require(value.get('recordType') == template['recordType'], 'Record type changed')
    require(value.get('isPlaceholder') is False, 'Placeholder flag must be false')
    for key in template['question']:
        text(value['question'].get(key), 'question.' + key)
    require(len(value['question']['shortBinaryQuestion']) <= 512, 'Question too long')
    if language == 'en':
        require(value['question']['shortBinaryQuestion'] == question['shortBinaryQuestion'],
                'Completion changed the accepted question; repair it upstream')
    text(value['meta'].get('title'), 'meta.title')
    require(value['meta']['theme'] == template['meta']['theme'], 'Presentation keys changed')
    for key in ('clarificationQuestion', 'wikiLinks'):
        require(isinstance(value.get(key), list) and (value[key] or (allow_unsourced and key == 'wikiLinks')),
                key + ' is required')
        for pair in value[key]:
            require(isinstance(pair, dict) and set(pair) == {'key1', 'key2'},
                    'Preserve legacy key1/key2 pairs')
            text(pair['key1'], key)
            text(pair['key2'], key)
    allowed_urls = {s['url'] for s in sources.values()}
    require(all(item['key2'] in allowed_urls for item in value['wikiLinks']),
            'wikiLinks must point to fetched frozen sources')
    text(value.get('openSourceApproach'), 'openSourceApproach')
    require(f'solutions/{language}/{expected["repoId"]}.json' in value['openSourceApproach'],
            'The SF must link its actual language-specific canonical solution')
    require(value.get('authorGithubId') == 'stefanospetsios', 'Do not invent the author')
    for key in ('editLink', 'jsonLink'):
        text(value.get(key), key)
        require(value[key].startswith('https://github.com/stefanospetsios/uniq2me-ai/'),
                key + ' must point to the declared output repository')
    # These are real-world observations, identities, signatures and archive assets.
    # Null is complete and truthful when no assessment/publication asset exists.
    for key in ('glacierWiki', 'glacierLink', 'glacerQRcode', 'linkLatest', 'resultScore'):
        require(value.get(key) is None, key + ' must remain null until real evidence exists')
    require(value.get('review') == {'status': 'draft', 'approvals': []},
            'Model checks do not create human content approval')
    require(value.get('signatures') == template['signatures'], 'Do not fill operational signatures')
    for signer in value['signatures'].values():
        require(all(signer.get(k) is None for k in ('vote', 'fullName', 'timestamp', 'text')),
                'Template signatures must be empty; legacy demo signatures are not evidence')


def validate_review(value, artifact, checks, sources, fixtures=None, *, allow_unsourced=False):
    require(value.get('artifactDigest') == digest(artifact), 'Review targets different bytes')
    require(value.get('decision') in ('accepted', 'rejected'), 'Invalid review decision')
    require(isinstance(value.get('unresolved'), list), 'Invalid review unresolved findings')
    require(isinstance(value.get('checks'), list), 'Invalid review checks')
    if value['decision'] == 'rejected' or value['unresolved']:
        raise ReviewRejected(value)
    rows = value.get('checks', [])
    require(isinstance(rows, list) and len(rows) == len(checks), 'Review omitted required checks')
    require({r.get('ruleId') for r in rows} == set(checks), 'Review check IDs differ')
    for row in rows:
        if row.get('passed') is not True:
            raise ReviewRejected(value)
        text(row.get('rationale'), 'review rationale')
        try:
            refs(row.get('sourceRefs'), sources,
                 allow_empty=allow_unsourced or row['ruleId'] in CANDIDATE_REVIEW_CHECKS)
        except (ValueError, KeyError, TypeError) as problem:
            raise ValueError('Review check ' + str(row['ruleId']) + ' sourceRefs: ' + str(problem)) from problem
    if fixtures is not None:
        outcomes = value.get('fixtureOutcomes', [])
        require(len(outcomes) == len(fixtures), 'Review omitted fixture decisions')
        require({r['id']: r['outcome'] for r in outcomes}
                == {f['id']: f['expected'] for f in fixtures},
                'Shared evidence fixtures produced different judgments')


def catalog_digest(root, language):
    root = Path(root)
    sets = []
    for sfs in HEX:
        area = read(root / 'json' / language / (sfs + '.json'))
        require([entry['id'] for entry in area['set']] == list(HEX), 'Incomplete SFS')
        records = []
        for entry in area['set']:
            record = read(root / 'repo-sf' / language / (entry['repoId'] + '.json'))
            identity(record['identity'], {'sfsId': sfs, 'sfId': entry['id'],
                                        'repoId': entry['repoId']}, language)
            records.append(record)
        sets.append({'sfsId': sfs, 'set': area, 'records': records})
    return digest(sets)


def expand_mask(selection):
    fields = {sfs: 0 for sfs in HEX}
    if selection['type'] == 'catalog':
        fields = {sfs: int(selection['fields'][sfs], 16) for sfs in HEX}
    elif selection['type'] == 'sfs':
        fields[selection['sfsId']] = int(selection['mask'], 16)
    elif selection['type'] == 'sf':
        for position in selection['positions']:
            fields[position['sfsId']] |= 1 << (15 - int(position['sfId'], 16))
    else:
        raise ValueError('Unknown mask selection')
    return {sfs: f'{value:04X}' for sfs, value in fields.items()}


def validate_masks(value, schema, language, expected_digest, sources, *, allow_unsourced=False):
    require(isinstance(value, dict) and set(value) == {'masks', 'coverage'},
            'Return masks and separate coverage records')
    masks, coverage = value['masks'], value['coverage']
    require(isinstance(masks, list) and masks, 'Requested MASKS are missing')
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    require(len({mask['id'] for mask in masks}) == len(masks), 'Duplicate mask ID')
    require(isinstance(coverage, list) and len(coverage) == len(masks), 'Missing mask coverage')
    require({c['maskId'] for c in coverage} == {m['id'] for m in masks}, 'Wrong coverage IDs')
    for mask in masks:
        validator.validate(mask)
        require(mask['language'] == language and mask['catalog']['language'] == language,
                'Wrong mask language')
        require(mask['catalog']['digest'] == expected_digest, 'Stale mask catalog digest')
        require(mask['status'] == 'draft' and mask['catalog']['edition'] is None,
                'Generated legal masks must remain unreviewed drafts')
        review = mask.get('provenance', {}).get('review')
        require(review is None or (review['status'] == 'pending' and review['reviewers'] == []
                                  and review['reviewedAt'] is None),
                'Do not manufacture a legal reviewer')
        require(mask['references'] or allow_unsourced, 'Legal mask needs source references')
        require(all(r.get('url') in {s['url'] for s in sources.values()}
                    for r in mask['references']), 'Mask cites an unfrozen source')
        fields = expand_mask(mask['selection'])
        require(any(int(x, 16) for x in fields.values()), 'Empty legal mask is not useful coverage')
        mapping = next(c for c in coverage if c['maskId'] == mask['id'])
        for key in ('instrument', 'jurisdiction', 'entityScope', 'applicabilityAssumptions'):
            text(mapping.get(key), 'coverage.' + key)
        require(mapping.get('unresolved') == [], 'Mask mapping has unresolved blockers')
        require(isinstance(mapping.get('uncoveredObligations'), list), 'Record coverage gaps')
        require(isinstance(mapping.get('mappings'), list) and mapping['mappings'], 'No mappings')
        selected = {(sfs, sf) for sfs in HEX for sf in HEX
                    if int(fields[sfs], 16) & (1 << (15 - int(sf, 16)))}
        mapped = set()
        for item in mapping['mappings']:
            text(item.get('article'), 'mapped article')
            text(item.get('rationale'), 'mapping rationale')
            refs(item.get('sourceRefs'), sources, allow_empty=allow_unsourced)
            positions = item.get('positions', [])
            require(isinstance(positions, list) and positions, 'Mapping has no positions')
            for p in positions:
                require((p['sfsId'], p['sfId']) in selected, 'Mapping references an excluded bit')
                mapped.add((p['sfsId'], p['sfId']))
        require(mapped == selected, 'Every selected bit needs an article-linked rationale')


def file_manifest(root):
    root = Path(root)
    result = {}
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'Symlinks are not permitted in run artifacts')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = byte_digest(path)
    return result
