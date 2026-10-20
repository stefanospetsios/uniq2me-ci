"""Filterable multilingual evidence records; no model calls or remote writes."""
import argparse
import copy
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import digest, read, write
from ci.source_passages import source_excerpt
from ci.source_research import observed_document_urls

REPOSITORY = 'stefanospetsios/uniq2me-rag'
BRANCH = 'main'
DIRECTORY = 'data'


def language(value):
    value = str(value or 'und').lower()
    return value if re.fullmatch(r'(?:[a-z]{2,3})(?:-[a-z0-9]{2,8})*', value) else 'und'


def context_scope(context, call_id=''):
    scope = context.get('scopeId')
    repo_id = context.get('repoId')
    match = re.search(r'-(00([0-9A-F])[0-9A-F])(?:-|$)', call_id)
    if match:
        repo_id, scope = match[1], match[2]
    if not scope:
        match = re.search(r'(?:sources-|roster-)(legal|[0-9A-F])(?:-|$)', call_id)
        if not match:
            match = re.match(r'refine-([0-9A-F])-', context.get('phase', ''))
        scope = match[1] if match else None
    return scope, repo_id


def filters(languages=(), scopes=(), phases=(), status='unverified', publisher=None, url=None, framework=None):
    return {'languages': sorted(set(language(v) for v in languages)) or ['und'],
            'scopeIds': sorted(set(v for v in scopes if v)),
            'phases': sorted(set(v for v in phases if v)), 'status': status,
            'publisher': publisher, 'host': urlsplit(url).hostname if url else None,
            **({'frameworkIds':[framework]} if framework else {})}


def document_record(record, source_text, context):
    value = copy.deepcopy(record)
    source = value['source']
    scope = source.get('id', '').split('-source-')[0] or context.get('scopeId')
    value.update(recordType='source-document', sourceUrl=source['url'],
        language=language(source.get('language')), retrievedAt=source['retrievedAt'],
        foundIn=copy.deepcopy(context), originalText=source_text[:800],
        excerptKind='fetched-document-excerpt', excerptTruncated=len(source_text) > 800)
    value['filters'] = filters([source.get('language')], [scope], [context.get('phase')],
        source.get('sourceStatus', 'fetched-awaiting-passage-check'), source.get('publisher'), source['url'],
        framework=context.get('frameworkId'))
    return value


def write_passages(outbox, source, source_text, context, cached_at):
    if source.get('sourceStatus') != 'fetched-and-passage-checked':
        return
    scope = source.get('id', '').split('-source-')[0] or context.get('scopeId')
    for passage in source.get('passages', []):
        quote = passage['text']
        exact = quote in source_text
        original = quote if exact else source_excerpt(quote, source_text)
        identity = digest([source['url'], source['textDigest'], scope, passage])[7:]
        write(Path(outbox)/'passages'/(identity+'.json'), {
            'schemaVersion':1, 'recordType':'source-passage', 'id':identity,
            'cachedAt':cached_at, 'originRunId':context.get('runId'),
            'sourceUrl':source['url'], 'finalUrl':source.get('finalUrl'),
            'language':language(source.get('language')), 'publisher':source.get('publisher'),
            'title':source.get('title'), 'retrievedAt':source['retrievedAt'],
            'byteDigest':source['byteDigest'], 'textDigest':source['textDigest'],
            'sourceId':source.get('id'), 'passageId':passage['id'], 'locator':passage['locator'],
            'quotation':quote, 'originalText':original,
            'excerptKind':'exact-quotation' if exact else 'original-source-context',
            'quotationMatchPolicy':source.get('quotationMatchPolicy'),
            'foundIn':copy.deepcopy(context),
            'filters':filters([source.get('language')], [scope], [context.get('phase')],
                              'passage-checked', source.get('publisher'), source['url'], framework=context.get('frameworkId'))})


def capture_searches(outbox, transcript, context, observed_at):
    """Retain observed results from every search-capable phase, including failures."""
    count = 0
    for message in transcript:
        for part in message.get('parts', []):
            tool = part.get('tool', '')
            state = part.get('state', {})
            web_tool = tool.startswith(('searxng_', 'websearch_')) or (
                tool.startswith('github_') and 'search' in tool[len('github_'):])
            if (part.get('type') != 'tool' or not web_tool
                    or state.get('status') not in ('completed', 'error')):
                continue
            count += 1
            arguments = state.get('input', {})
            arguments = arguments if isinstance(arguments, dict) else {}
            request = {k:v for k,v in arguments.items() if k in {
                'query','q','search_query','searchQuery','language','lang','engines','categories','time_range','url'}}
            output = state.get('output', '')
            output_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            url_error = None
            try:
                urls = sorted(observed_document_urls([{'parts':[part]}]))
            except ValueError as error:
                # Retain the observation even when an engine emits malformed URLs.
                # Admission still rejects unusable provenance independently.
                urls, url_error = [], str(error)
            scope, repo_id = context_scope(context, context.get('callId', ''))
            identity = digest([context, part.get('callID', part.get('id', count)), tool, digest(output)])[7:]
            meta = filters([arguments.get('language', arguments.get('lang'))], [scope],
                           [context.get('phase')], 'search-result-unverified', framework=context.get('frameworkId'))
            meta['hosts'] = sorted(set(urlsplit(u).hostname for u in urls[:200] if urlsplit(u).hostname))
            write(Path(outbox)/'searches'/(identity+'.json'), {
                'schemaVersion':1, 'recordType':'web-search', 'id':identity,
                'cachedAt':observed_at, 'observedAt':observed_at,
                'originRunId':context.get('runId'), 'foundIn':{**context,'scopeId':scope,'repoId':repo_id},
                'toolId':tool, 'request':request, 'executionStatus':state['status'],
                'toolTiming':copy.deepcopy(state.get('time')),
                'outputDigest':digest(output), 'resultUrls':urls[:200], 'resultUrlsTruncated':len(urls)>200,
                'resultUrlError':url_error,
                'originalText':output_text[:2000], 'excerptTruncated':len(output_text)>2000,
                'excerptKind':'search-result-excerpt',
                'error':str(state.get('error', ''))[:1000] or None,
                'filters':meta})
    return count


def query_records(root, *, language_filter=None, scope=None, phase=None, kind=None,
                  status=None, publisher=None, host=None, since=None, search=None, framework=None):
    root = Path(root)
    for collection in ('documents','scopes','passages','searches','download-retries'):
        if (root/collection).is_symlink():
            continue
        for path in sorted((root/collection).glob('*.json')):
            if path.is_symlink() or not re.fullmatch(r'[a-f0-9]{64}\.json',path.name):
                continue
            record = read(path)
            meta = record.get('filters', {})
            if framework and framework not in meta.get('frameworkIds', []):
                continue
            if language_filter and language(language_filter) not in meta.get('languages', []):
                continue
            if scope and scope.upper() not in [str(s).upper() for s in meta.get('scopeIds', [])]:
                continue
            if phase and not any(phase in p for p in meta.get('phases', [])):
                continue
            if kind and kind != record.get('recordType'):
                continue
            if status and status != meta.get('status'):
                continue
            if publisher and publisher.casefold() not in str(meta.get('publisher') or '').casefold():
                continue
            hosts = [str(h).casefold() for h in meta.get('hosts', [])] + [str(meta.get('host') or '').casefold()]
            if host and host.casefold() not in hosts:
                continue
            if since and str(record.get('retrievedAt') or record.get('observedAt') or record.get('cachedAt') or '') < since:
                continue
            if search and search.casefold() not in json.dumps(record,ensure_ascii=False).casefold():
                continue
            yield record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['query'])
    parser.add_argument('--root',type=Path,required=True)
    for name in ('language','scope','phase','kind','status','publisher','host','since','search','framework'):
        parser.add_argument('--'+name)
    args = parser.parse_args()
    for record in query_records(args.root,language_filter=args.language,scope=args.scope,phase=args.phase,
        kind=args.kind,status=args.status,publisher=args.publisher,host=args.host,since=args.since,search=args.search,
        framework=args.framework):
        print(json.dumps(record,ensure_ascii=False))


if __name__ == '__main__':
    main()
