"""Retain independently observed URLs across retries of one source scope."""
from pathlib import Path
import re

from ci.contracts import byte_digest, digest, read, require, write


def searches(transcript, groups):
    return [p for m in transcript for p in m.get('parts', [])
            if p.get('type') == 'tool' and p.get('state', {}).get('status') == 'completed'
            and any(p.get('tool') in group for group in groups.values())]


def queries(transcript, groups, scope, call_id):
    return [{'scope':scope,'toolId':p['tool'],'input':p['state'].get('input', {}),
             'outputDigest':digest(p['state'].get('output')),'callId':call_id}
            for p in searches(transcript, groups)]


def remember(server, work, scope, call_id, groups):
    require(isinstance(scope,str) and re.fullmatch(r'[0-9A-F]|legal',scope), 'Invalid source observation scope')
    require(re.fullmatch(r'sources-' + re.escape(scope) + r'-[1-9][0-9]*-discovery',call_id),
            'Source observation call belongs to another scope')
    path = Path(server.archive) / 'calls' / call_id / 'messages.json'
    if not path.exists():
        return
    root = Path(work).parent.resolve()
    archive_root = Path(server.archive).resolve()
    require(not path.is_symlink() and path.resolve().is_relative_to(archive_root), 'Unsafe search transcript path')
    transcript = read(path)
    parts = searches(transcript, groups)
    if not all(any(p['tool'] in group for p in parts) for group in groups.values()):
        return
    # Call paths can be reused after a retry. Keep immutable search-result bytes,
    # rather than making old provenance depend on an overwritten call transcript.
    # Keep completed reads too: truncated MCP results can be read from saved files,
    # and observed_document_urls applies the same provenance rules to those reads.
    retained = [{'parts':[p for m in transcript for p in m.get('parts', [])
        if p.get('type') == 'tool' and p.get('state', {}).get('status') == 'completed']}]
    snapshot = Path(work) / 'discovery-observations' / scope / 'transcripts' / (digest(retained)[7:] + '.json')
    require(not snapshot.is_symlink() and snapshot.resolve().is_relative_to(root), 'Unsafe retained search snapshot path')
    if snapshot.exists():
        require(not snapshot.is_symlink() and read(snapshot) == retained, 'Retained search snapshot changed')
    else:
        write(snapshot, retained)
    record = {'scopeId':scope,'callId':call_id,'transcriptRef':str(snapshot.resolve().relative_to(root)),
              'transcriptDigest':byte_digest(snapshot),
              'originalTranscriptRef':str(path.resolve().relative_to(archive_root))}
    write(Path(work) / 'discovery-observations' / scope / (digest(record)[7:] + '.json'), record)


def history(work, scope, groups):
    from ci.source_research import observed_document_urls
    root = Path(work).parent.resolve()
    require(isinstance(scope,str) and re.fullmatch(r'[0-9A-F]|legal',scope), 'Invalid source observation scope')
    observed, previous_queries = set(), []
    for path in sorted((Path(work) / 'discovery-observations' / scope).glob('*.json')):
        require(not path.is_symlink(), 'Unsafe search observation record')
        record = read(path)
        require(record['scopeId'] == scope and path.stem == digest(record)[7:], 'Source observation identity changed')
        require(re.fullmatch(r'sources-' + re.escape(scope) + r'-[1-9][0-9]*-discovery',record['callId']),
                'Source observation call belongs to another scope')
        relative = Path(record['transcriptRef'])
        require(not relative.is_absolute() and '..' not in relative.parts, 'Unsafe retained transcript reference')
        transcript_path = root / relative
        require(not transcript_path.is_symlink() and transcript_path.resolve().is_relative_to(root) and
                byte_digest(transcript_path) == record['transcriptDigest'], 'Retained search transcript changed')
        transcript = read(transcript_path)
        parts = searches(transcript, groups)
        require(all(any(p['tool'] in group for p in parts) for group in groups.values()),
                'Retained observation lacks both search MCPs')
        observed.update(observed_document_urls(transcript))
        previous_queries.extend({**q,'reusedScopeObservation':True} for q in
                                queries(transcript, groups, scope, record['callId']))
    return observed, previous_queries
