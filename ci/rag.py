"""Explicit retrieval modes; non-web outputs are drafts, never accepted bases."""
import copy

from ci.contracts import HEX, byte_digest, digest, read, require, write
from ci.progress import log
from ci.runtime import utc

MODES = ('disabled', 'repo-only', 'repo-web')


def validate_mode(mode):
    require(mode in MODES, 'RAG_MODE must be disabled, repo-only or repo-web')
    return mode


def mode_of(state):
    return validate_mode(state.get('ragMode', 'repo-web'))


def policy(mode):
    validate_mode(mode)
    return {'mode': mode, 'enabled': mode != 'disabled', 'webResearch': mode == 'repo-web',
            'repositoryRetrieval': mode != 'disabled',
            'outputPolicy': 'source-grounded-acceptance-required' if mode == 'repo-web' else 'unverified-draft-only'}


def runtime_config(config, mode):
    """Disable remote MCPs explicitly, including matching runner-user configuration."""
    config = copy.deepcopy(config)
    if validate_mode(mode) != 'repo-web':
        for server in config['mcp'].values():
            server['enabled'] = False
    return config


def instructions(mode):
    if mode == 'repo-web':
        return ('\nRAG_MODE=repo-web: use the pinned repositories and admitted frozen web document snapshots. '
                'Respect original retrieval dates and cacheReuse markers in frozen/sources.json. '
                'Reused documents and prior queries do not establish current web contents or a new update search. '
                'Qualify version/currency claims using those actual observations and limitations.\n')
    if mode == 'repo-only':
        return ('\nRAG_MODE=repo-only: retrieval is limited to pinned repository text in '
            'frozen/sources.json and inputs/, including saved research from uniq2me-rag/main. '
            'No remote MCPs, web search, web fetch or external document access. '
            'Repository templates/catalogs are curated inputs, not verified statutory or '
            'implementation evidence. Saved research preserves original URLs, retrieval dates '
            'and quotations checked against pinned snapshots; it is not a fresh search, '
            'current web verification or a newly admitted complete scope. Produce unverified '
            'drafts; use sourceRefs only for actual frozen passages. Do not claim legal '
            'currency, legal verification or source acceptance. Legal MASKS remain '
            'unverified proposals with that limitation in descriptions and coverage. '
            'Review checks concern draft consistency and truthful provenance, not current '
            'external verification. Do not reject a draft solely because web verification '
            'was disabled, and never describe that verification as performed.\n')
    validate_mode(mode)
    return ('\nRAG_MODE=disabled: no RAG corpus, retrieval tools, MCPs or web access. '
        'Use model knowledge plus the mandatory repository templates/topic allocation '
        'provided for this task. Built-in read may inspect mandatory templates and '
        'current-run artifacts for their shape, identities and consistency; it must '
        'not retrieve an additional knowledge corpus. Explicit prior-draft timeout '
        'feedback is allowed only as unverified repair context, never accepted evidence. '
        'Explicit user-selected commit snapshots under inputs/reference-inputs/ are also '
        'permitted as unverified reference data when configured, never as source verification. '
        'These are unverified drafts. Set all sourceRefs, '
        'wikiLinks and mask references to []; never invent a citation. The source-support '
        'and legal provenance checks in this mode mean checking that no source '
        'verification is falsely asserted, not verifying claims. Structural, consistency '
        'and fixture checks still apply. Unresolved generation defects still block a '
        'draft; lack of external verification is separately recorded by CI. Label legal '
        'MASK descriptions and coverage as unverified model proposals. This explicit '
        'mode overrides source-grounded task examples asking for populated citations.\n')


def saved_repository_research(pipeline):
    """Read checked historical quotations from pinned RAG; never fetch or admit a scope."""
    from ci.source_cache import SourceCache, copy_files, snapshot_names
    from ci.source_documents import ensure_source_available
    from ci.source_passages import check_source_passages
    cache = SourceCache(pipeline)
    if not cache.enabled or cache.config.get('mode') != 'reuse' or not cache.config.get('importedCommit'):
        return {}, []
    framework = pipeline.state.get('catalogProfile', {}).get('frameworkId')
    selected = set(pipeline.state.get('sourceScopes', HEX))
    sources, limitations, seen = {}, [], set()
    for collection in ('documents', 'scopes'):
        for path in sorted((cache.imported / collection).glob('*.json')):
            relative = path.relative_to(cache.imported).as_posix()
            if relative not in cache.config.get('importedManifest', {}):
                continue
            root, record = cache.record(relative)
            if not record:
                continue
            meta = record.get('filters', {})
            frameworks = meta.get('frameworkIds', [])
            if (framework and framework not in frameworks) or (not framework and frameworks):
                continue
            scopes = selected.intersection(meta.get('scopeIds', []))
            candidates = ([record['source']] if collection == 'documents' else
                          list(record.get('corpus', {}).get('sources', {}).values()))
            for original in candidates:
                scope = original.get('id', '').split('-source-')[0]
                if scope not in scopes or original.get('sourceStatus') != 'fetched-and-passage-checked':
                    continue
                identity = (scope, original.get('url'), original.get('textDigest'))
                if identity in seen:
                    continue
                try:
                    snapshot_names(original)
                    for field, checksum in (('snapshotPath', 'byteDigest'), ('textPath', 'textDigest')):
                        require(record['files'].get('snapshots/' + original[field]) == original[checksum],
                                'Shared RAG source digest differs from its snapshot')
                    content = (root / 'snapshots' / original['textPath']).read_text(encoding='utf-8')
                    ensure_source_available(content)
                    source = check_source_passages(original, content)
                    files = {'snapshots/' + source[field]: source[checksum]
                             for field, checksum in (('snapshotPath', 'byteDigest'), ('textPath', 'textDigest'))}
                    copy_files(root, pipeline.work / 'frozen', files)
                except (ValueError, KeyError, TypeError, OSError) as problem:
                    limitations.append({'scope': scope, 'description':
                        'Excluded saved RAG source ' + str(original.get('url')) + ': ' + str(problem)})
                    continue
                sid = scope + '-shared-rag-' + digest(identity)[7:]
                source.update(id=sid, sourceStatus='pinned-shared-rag-passages-not-currently-verified',
                    storedSourceId=original['id'],
                    cacheReuse={'originRunId': record.get('originRunId'),
                        'cacheCommit': cache.config['importedCommit'], 'cacheRecord': relative,
                        'reusedAt': utc(), 'executedThisRun': False, 'webRecheckedThisRun': False})
                sources[sid] = source
                seen.add(identity)
    cache.config['documentsReused'] = cache.config.get('documentsReused', 0) + len(sources)
    pipeline.save()
    limitations.append({'scope': 'all', 'description':
        f'Reused {len(sources)} saved RAG document/scope records from pinned uniq2me-rag/main '
        f"at {cache.config['importedCommit']}. Original retrieval dates retained; no web search, "
        'download, current verification or new scope admission was performed. '
        'Only matching framework/scope records with locally checked quotations were included.'})
    log('rag.saved_repository_research', f'Loaded {len(sources)} saved research sources; web access disabled')
    return sources, limitations


def prepare_repository_sources(pipeline):
    """Index committed repository prose only; never dereference its cited web URLs."""
    mode = mode_of(pipeline.state)
    require(mode != 'repo-web', 'Repository preparation cannot replace live discovery')
    sources, concerns = {}, []
    for scope in pipeline.state.get('sourceScopes', HEX):
        area_path = pipeline.work / 'inputs/json/en' / (scope + '.json')
        area = read(area_path)
        source_refs = []
        if mode == 'repo-only':
            source_id = scope + '-repository-area'
            passages = [{'id': key, 'locator': 'json/en/' + scope + '.json#' + key,
                         'text': area[key]} for key in ('keyTitle', 'keyShortInfo', 'keyDescription', 'keyWords')
                        if isinstance(area.get(key), str) and area[key].strip()]
            require(passages, 'Pinned area has no usable repository text: ' + scope)
            source = {'id': source_id, 'type': 'repository-template',
                'title': area['keyTitle'], 'publisher': 'stefanospetsios/uniq2me',
                'url': 'https://github.com/stefanospetsios/uniq2me/blob/' +
                    pipeline.state['inputCommit'] + '/json/en/' + scope + '.json',
                'language': 'en', 'jurisdiction': 'repository topic allocation; no legal verification',
                'applicability': 'Pinned topic scope ' + scope, 'publicationDate': None,
                'effectiveDate': None, 'redistribution': 'repository-prose',
                'byteDigest': byte_digest(area_path), 'inputPath': str(area_path.relative_to(pipeline.work)),
                'retrievedAt': utc(), 'sourceStatus': 'pinned-repository-text-not-externally-verified',
                'passages': passages}
            sources[source_id] = source
            source_refs = [{'sourceId': source_id, 'passageId': p['id']} for p in passages]
            # Existing SFs and a prior accepted release are committed input
            # collections. They are never relabeled as statutory evidence.
            for collection, directory, repository, commit in (
                ('template', pipeline.work / 'inputs/repo-sf/en', 'uniq2me', pipeline.state['inputCommit']),
                ('prior', pipeline.work / 'inputs/prior-release/catalog/questions/en',
                 'uniq2me-ai', pipeline.state['outputBaseCommit'])):
                for slot in (s for s in pipeline.state['slots'] if s['sfsId'] == scope):
                    path = directory / (slot['repoId'] + '.json')
                    if not path.exists():
                        continue
                    value = read(path)
                    prose = value.get('shortBinaryQuestion') or value.get('question', {}).get('shortBinaryQuestion')
                    if not isinstance(prose, str) or not prose.strip():
                        continue
                    sid = f'{scope}-repository-{collection}-{slot["repoId"]}'
                    relative = path.relative_to(pipeline.work / 'inputs').as_posix()
                    # Prior-release files are copied from the output repository;
                    # map their paths back to the immutable release namespace.
                    if collection == 'prior':
                        relative = (pipeline.state.get('priorRelease') or {})['releasePath'] + '/' + relative[len('prior-release/'):]
                    sources[sid] = {**source, 'id': sid, 'type': 'repository-catalog',
                        'title': f'Pinned {collection} question {slot["repoId"]}',
                        'publisher': 'stefanospetsios/' + repository,
                        'url': f'https://github.com/stefanospetsios/{repository}/blob/{commit}/{relative}',
                        'byteDigest': byte_digest(path), 'inputPath': str(path.relative_to(pipeline.work)),
                        'passages': [{'id': 'question', 'locator': 'shortBinaryQuestion', 'text': prose}]}
            if pipeline.state.get('baseCatalog'):
                base = pipeline.state['baseCatalog']
                for slot in (s for s in pipeline.state['slots'] if s['sfsId'] == scope):
                    relative = 'repo-sf/en/' + slot['repoId'] + '.json'
                    path = pipeline.work / 'base' / relative
                    record = read(path)
                    passages = [{'id': key, 'locator': 'question.' + key, 'text': value}
                        for key,value in record['question'].items() if isinstance(value,str) and value.strip()]
                    sid = scope + '-repository-base-' + slot['repoId']
                    sources[sid] = {**source, 'id': sid, 'type': 'repository-catalog',
                        'title': 'Pinned base SF ' + slot['repoId'], 'publisher': base['repository'],
                        'url': 'https://github.com/' + base['repository'] + '/blob/' + base['commit'] + '/' + relative,
                        'byteDigest': byte_digest(path), 'inputPath': path.relative_to(pipeline.work).as_posix(),
                        'passages': passages}
        # This inventory records mandatory topic boundaries, not model claims or
        # a claimed independent legal/technical coverage audit.
        concerns.append({'id': scope + '-concern-topic-boundaries', 'sfsId': scope,
            'description': area.get('keyDescription') or area['keyTitle'], 'sourceRefs': source_refs,
            'inventoryBasis': 'pinned-topic-boundaries; not an independent completeness assessment'})
    limitations = [{'scope': 'all', 'description': 'RAG ' + mode +
        ': no current external source admission or legal verification; outputs are unverified drafts.'}]
    if mode == 'repo-only':
        saved, saved_limitations = saved_repository_research(pipeline)
        sources.update(saved)
        limitations.extend(saved_limitations)
    write(pipeline.work / 'frozen/mcp-capabilities.json', {
        'ragMode': mode, 'servers': {}, 'allowedTools': [], 'realConnectionsProbed': False,
        'reason': 'Remote retrieval disabled by the selected RAG mode'})
    pipeline.state['allowedTools'] = []
    return sources, concerns, [], limitations
