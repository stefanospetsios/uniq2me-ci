"""Public aggregate metrics from observed call archives; never estimate missing usage."""
from datetime import datetime, timezone
from pathlib import Path

from ci.contracts import read

STAGES = ['preflight', 'prepare', 'catalog', 'extended', 'masks', 'translate-fr', 'translate-el']


def number(value):
    return type(value) in (int, float) and value >= 0


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def collect(root, state, observed_at=None):
    root = Path(root)
    work = root / 'work'
    observed_at = observed_at or datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    stage_names = [*STAGES, 'refine'] if state.get('refinementPlan') else STAGES
    stages = {stage: {'executionAttempts': 0, 'failedExecutionAttempts': 0, 'activeSeconds': None,
                     'agentCalls': 0, 'failedAgentCalls': 0, 'agentSessionSeconds': None,
                     'modelResponses': 0, 'responsesWithTotal': 0, 'knownTotalTokens': 0,
                     'inputTokens': None, 'outputTokens': None, 'reasoningTokens': None,
                     'cacheReadTokens': None, 'cacheWriteTokens': None} for stage in stage_names}
    seen_messages, seen_tools, usage_rows = set(), set(), []
    tool_calls = {}
    native_load, native_eval_seconds, native_eval_tokens = [], [], []
    for attempt in state.get('executionAttempts', []):
        stage = stages.get(attempt['stage'])
        if stage is None:
            continue
        stage['executionAttempts'] += 1
        stage['failedExecutionAttempts'] += attempt.get('status') == 'failed'
        if number(attempt.get('elapsedSeconds')):
            stage['activeSeconds'] = (stage['activeSeconds'] or 0) + attempt['elapsedSeconds']

    def add_usage(stage, usage):
        stage['modelResponses'] += 1
        stage['responsesWithTotal'] += number(usage.get('total'))
        if number(usage.get('total')):
            stage['knownTotalTokens'] += usage['total']
        for source, destination in [('input', 'inputTokens'), ('output', 'outputTokens'),
                                    ('reasoning', 'reasoningTokens'), ('cacheRead', 'cacheReadTokens'),
                                    ('cacheWrite', 'cacheWriteTokens')]:
            if number(usage.get(source)):
                stage[destination] = (stage[destination] or 0) + usage[source]
        usage_rows.append(usage)

    for path in sorted((root / 'runtime').glob('**/calls/*/envelope.json')):
        envelope = read(path)
        runtime_stage = path.relative_to(root / 'runtime').parts[0]
        if runtime_stage.startswith('refine-'):
            runtime_stage = 'refine'
        stage = stages.get('prepare' if runtime_stage == 'sources' else runtime_stage, stages['prepare'])
        stage['agentCalls'] += 1
        stage['failedAgentCalls'] += envelope.get('status') != 'completed'
        if number(envelope.get('elapsedSeconds')):
            stage['agentSessionSeconds'] = (stage['agentSessionSeconds'] or 0) + envelope['elapsedSeconds']
        message_path = path.with_name('messages.json')
        messages = read(message_path) if message_path.exists() else []
        assistants = [message for message in messages if message.get('info', {}).get('role') == 'assistant']
        if not assistants:
            assistants = [{'info': {'tokens': usage}} for usage in envelope.get('providerUsage', [])]
        # An interrupted call with no transcript/usage is unknown, never zero.
        if not assistants:
            assistants = [{'info': {}}]
        for index, message in enumerate(assistants):
            info = message['info']
            key = (envelope.get('sessionId', str(path)), info.get('id', str(path) + ':' + str(index)))
            if key in seen_messages:
                continue
            seen_messages.add(key)
            tokens = info.get('tokens') or {}
            cache = tokens.get('cache') or {}
            add_usage(stage, {**tokens, 'cacheRead': cache.get('read'), 'cacheWrite': cache.get('write')})
        for message_index, message in enumerate(messages):
            for part_index, part in enumerate(message.get('parts', [])):
                if part.get('type') != 'tool':
                    continue
                key = (envelope.get('sessionId', str(path)), part.get('id',
                    str(path) + ':' + str(message_index) + ':' + str(part_index)))
                if key in seen_tools:
                    continue
                seen_tools.add(key)
                name = part.get('tool', 'unknown')
                counts = tool_calls.setdefault(name, {'completed': 0, 'failed': 0, 'unfinished': 0})
                status = part.get('state', {}).get('status')
                counts['completed' if status == 'completed' else 'failed' if status == 'error' else 'unfinished'] += 1

    native_paths = sorted((root / 'ollama').glob('**/tool-probe-*/*-response.json'))
    native_stage = stages['preflight'] if any(a['stage'] == 'preflight' for a in state.get('executionAttempts', [])) \
        else stages['prepare']
    def probe_stage(path):
        journal, context = path.parent / 'attempts.json', path.parent / 'context.json'
        if journal.exists():
            owner = next((item.get('ciStage') for item in read(journal)
                          if item.get('responsePath', '').endswith(path.name)), None)
            return stages.get(owner, native_stage)
        if context.exists():
            return stages.get(read(context).get('ciStage'), native_stage)
        return native_stage
    if not native_paths and (root / 'ollama/tool-probe-response.json').exists():
        native_paths = [root / 'ollama/tool-probe-response.json']
    for path in native_paths:
        response = read(path)
        if not isinstance(response, dict):
            add_usage(probe_stage(path), {})
            continue
        inputs, outputs = response.get('prompt_eval_count'), response.get('eval_count')
        add_usage(probe_stage(path), {'input': inputs, 'output': outputs,
            'total': inputs + outputs if number(inputs) and number(outputs) else None,
            'cacheRead': response.get('prompt_eval_cached_count')})
        if number(response.get('load_duration')):
            native_load.append(response['load_duration'] / 1e9)
        if number(response.get('eval_duration')) and response['eval_duration'] > 0 and number(outputs):
            native_eval_seconds.append(response['eval_duration'] / 1e9)
            native_eval_tokens.append(outputs)
    unanswered_native = [path for path in (root / 'ollama').glob('**/tool-probe-*/*-request.json')
                         if not path.with_name(path.name.replace('-request.json', '-response.json')).exists()]
    for _ in unanswered_native:
        add_usage(native_stage, {})
    compatible_paths = sorted((root / 'ollama').glob('**/compatible-probe-*/response.json'))
    if not compatible_paths and (root / 'ollama/openai-compatible-probe-response.json').exists():
        compatible_paths = [root / 'ollama/openai-compatible-probe-response.json']
    for path in compatible_paths:
        response = read(path)
        usage = response.get('usage', {}) if isinstance(response, dict) else {}
        inputs, outputs = usage.get('prompt_tokens'), usage.get('completion_tokens')
        total = usage.get('total_tokens')
        add_usage(probe_stage(path), {'input': inputs, 'output': outputs,
            'total': total if number(total) else inputs + outputs if number(inputs) and number(outputs) else None,
            'reasoning': usage.get('completion_tokens_details', {}).get('reasoning_tokens'),
            'cacheRead': usage.get('prompt_tokens_details', {}).get('cached_tokens')})
    unanswered_compatible = [path for path in (root / 'ollama').glob('**/compatible-probe-*/request.json')
                            if not path.with_name('response.json').exists()]
    for _ in unanswered_compatible:
        add_usage(native_stage, {})

    dry = state.get('dryRun', False)
    for stage in stages.values():
        responses = stage['modelResponses']
        complete = stage['responsesWithTotal'] == responses and (responses > 0 or dry)
        stage['totalTokens'] = stage['knownTotalTokens'] if complete else None
        for field in ('activeSeconds', 'agentSessionSeconds'):
            if stage[field] is not None:
                stage[field] = round(stage[field], 3)
    def summed(field):
        values = [stage[field] for stage in stages.values() if stage[field] is not None]
        return round(sum(values), 3) if values else None
    responses = sum(stage['modelResponses'] for stage in stages.values())
    with_total = sum(stage['responsesWithTotal'] for stage in stages.values())
    known = sum(stage['knownTotalTokens'] for stage in stages.values())
    start, end = timestamp(state.get('startedAt')), timestamp(observed_at)
    sources_path = work / 'frozen/sources.json'
    sources = read(sources_path).get('sources', []) if sources_path.exists() else []
    capability_path = work / 'frozen/mcp-capabilities.json'
    capabilities = read(capability_path) if capability_path.exists() else {}
    servers = capabilities.get('servers', {})
    residency = state.get('model', {}).get('residency', {})
    direct_path = root / 'preflight-mcp-tool-execution.json'
    direct_probes = read(direct_path) if direct_path.exists() else {}
    return {'recordType': 'ci-run-metrics', 'schemaVersion': 1, 'runId': state.get('runId'),
        'observedAt': observed_at, 'dryRun': dry, 'ragMode': state.get('ragMode', 'repo-web'),
        'freshStart': state.get('freshStart', False),
        'outputStatus': 'synthetic-fixture' if dry else
            'source-grounded' if not state.get('refinementPlan') and state.get('ragMode', 'repo-web') == 'repo-web'
                and not (state.get('catalogPending') or state.get('catalogRosterPending')) else 'unverified-draft',
        'time': {'elapsedSinceStartSeconds': round(max(0, end - start), 3) if start is not None and end is not None else None,
                 'activeExecutionSeconds': summed('activeSeconds'),
                 'agentSessionSeconds': summed('agentSessionSeconds'),
                 'nativeModelLoadSeconds': round(sum(native_load), 3) if native_load else 0 if dry else None,
                 'nativeGenerationTokensPerSecond': round(sum(native_eval_tokens) / sum(native_eval_seconds), 3)
                    if native_eval_seconds else None},
        'tokens': {'reportedTotal': known if responses == with_total and (responses or dry) else None,
                   'knownTotal': known, 'responsesWithTotal': with_total, 'modelResponses': responses,
                   'responsesWithoutTotal': responses - with_total,
                   **{field: summed(field) for field in ('inputTokens', 'outputTokens', 'reasoningTokens',
                                                        'cacheReadTokens', 'cacheWriteTokens')}},
        'calls': {'agent': sum(stage['agentCalls'] for stage in stages.values()),
                  'failedAgent': sum(stage['failedAgentCalls'] for stage in stages.values()),
                  'nativeProbe': len(native_paths) + len(unanswered_native),
                  'compatibleProbe': len(compatible_paths) + len(unanswered_compatible),
                  'directMcpProbes': {name: {'status': probe['status'],
                                            'toolId': probe.get('execution', {}).get('toolId'),
                                            'elapsedSeconds': probe.get('execution', {}).get('elapsedSeconds')}
                                      for name, probe in direct_probes.items()},
                  'tools': tool_calls},
        'sourceCache': {key: value for key, value in state.get('sourceCache', {}).items()
                        if key != 'importedManifest'},
        'progress': {'checkedTaskCaches': len(list((work / '.task-cache').glob('*/*.json'))),
            'cacheHits': state.get('metricsCounters', {}).get('cacheHits', 0),
            'questions': len(list((work / 'catalog/questions/en').glob('*.json'))) - len(state.get('catalogPending', {})),
            'pendingQuestions': len(state.get('catalogPending', {})),
            'catalogRosterPending': bool(state.get('catalogRosterPending')),
            'catalogGraceSeconds': state.get('catalogGraceSeconds', 30),
            'sources': len(sources), 'authoritativeSources': 0 if dry or state.get('ragMode', 'repo-web') != 'repo-web' else len(sources),
            'repositorySources': len(sources) if not dry and state.get('ragMode') == 'repo-only' else 0,
            'validatedMilestones': len(state.get('stages', {})),
            'publicationsRecorded': len(state.get('publications', [])) + len(state.get('incremental', {}).get('events', [])),
            'languages': {lang: {'sfJsons': len(list((work / 'repo-sf' / lang).glob('*.json'))),
                                'solutions': len(list((work / 'solutions' / lang).glob('*.json'))),
                                'masks': len(list((work / 'repo-sfs-masks' / lang).glob('*.json')))}
                          for lang in ('en', 'fr', 'el')}},
        'model': {'tag': state.get('model', {}).get('tag'), 'effectiveContext': state.get('model', {}).get('effectiveContext'),
                  'reportedSizeBytes': residency.get('size'), 'reportedVramBytes': residency.get('size_vram'),
                  'mcpServersConnectedAtDiscovery': sum(server.get('status') == 'connected' for server in servers.values())
                    if isinstance(servers, dict) and servers else None},
        'stages': stages,
        'interpretation': ['Reported provider totals are used for OpenCode; raw input/output/reasoning/cache fields '
            'are shown separately and are not added together. Native totals use prompt_eval_count + eval_count; '
            'cached and reasoning tokens are not added again.',
            'Missing totals remain unknown; knownTotal is a lower bound when coverage is incomplete. '
            'Failed and truncated responses are included; duplicate transcript message IDs are counted once.',
            'Elapsed since start includes retry/queue gaps; active execution excludes those gaps and job setup. '
            'Agent session time includes tool waits. Native load time and generation rate cover native probes only.',
            'Dry runs have zero model inference. No financial cost, peak GPU use or energy is inferred.']}


def display(value, unit=''):
    return 'unknown' if value is None else f'{value:,}{unit}'


def markdown(metrics):
    times, tokens, progress = metrics['time'], metrics['tokens'], metrics['progress']
    rows = ['', '| Metric | Observed value |', '|---|---|']
    rows += [f'| RAG mode / output status | {metrics.get("ragMode", "repo-web")} / '
             f'{metrics.get("outputStatus", "unknown")} |',
             f'| Fresh start | {metrics.get("freshStart", False)} |']
    rows += [f'| {name} | {display(value, unit)} |' for name, value, unit in [
        ('Elapsed since run start (includes gaps)', times['elapsedSinceStartSeconds'], ' s'),
        ('Active execution (includes failed attempts)', times['activeExecutionSeconds'], ' s'),
        ('Agent session time (includes tool waits)', times['agentSessionSeconds'], ' s'),
        ('Native probe model load time', times['nativeModelLoadSeconds'], ' s'),
        ('Native probe generation rate', times['nativeGenerationTokensPerSecond'], ' tokens/s'),
        ('Reported token total', tokens['reportedTotal'], ''), ('Known token subtotal', tokens['knownTotal'], ''),
        ('Raw reported input / output', None, '')]]
    rows[-1] = f'| Raw reported input / output | {display(tokens["inputTokens"])} / {display(tokens["outputTokens"])} |'
    rows += [f'| Raw reported reasoning | {display(tokens["reasoningTokens"])} |',
             f'| Raw cache read / write | {display(tokens["cacheReadTokens"])} / {display(tokens["cacheWriteTokens"])} |',
             f'| Responses with a token total | {tokens["responsesWithTotal"]} / {tokens["modelResponses"]} |',
             f'| Agent calls / failed calls | {metrics["calls"]["agent"]} / {metrics["calls"]["failedAgent"]} |',
             f'| Checked task caches / cache hits | {progress["checkedTaskCaches"]} / {progress["cacheHits"]} |',
             f'| Questions / authoritative sources | {progress["questions"]} / {progress["authoritativeSources"]} |',
             f'| Pending questions / roster review pending | {progress.get("pendingQuestions", 0)} / '
             f'{progress.get("catalogRosterPending", False)} |',
             f'| Catalog finalization grace | {progress.get("catalogGraceSeconds", 30)} s |',
             f'| Model / effective context | {metrics["model"]["tag"]} / {display(metrics["model"]["effectiveContext"])} |',
             f'| Reported model size / VRAM | {display(metrics["model"]["reportedSizeBytes"], " bytes")} / '
             f'{display(metrics["model"]["reportedVramBytes"], " bytes")} |', '',
             '| Stage | Active seconds | Execution attempts / failed | Agent calls / failed | Known tokens | Total coverage |',
             '|---|---:|---:|---:|---:|---:|']
    for name, stage in metrics['stages'].items():
        rows.append(f'| {name} | {display(stage["activeSeconds"])} | {stage["executionAttempts"]} / '
                    f'{stage["failedExecutionAttempts"]} | {stage["agentCalls"]} / {stage["failedAgentCalls"]} | '
                    f'{stage["knownTotalTokens"]:,} | {stage["responsesWithTotal"]} / {stage["modelResponses"]} |')
    rows += ['', '| Language | SF JSONs | Solutions | Masks |', '|---|---:|---:|---:|']
    for lang, counts in progress['languages'].items():
        rows.append(f'| {lang} | {counts["sfJsons"]} | {counts["solutions"]} | {counts["masks"]} |')
    if metrics['calls']['tools']:
        rows += ['', '| Observed tool | Completed | Failed | Unfinished |', '|---|---:|---:|---:|']
        for name, counts in sorted(metrics['calls']['tools'].items()):
            rows.append(f'| `{name}` | {counts["completed"]} | {counts["failed"]} | {counts["unfinished"]} |')
    if metrics['calls'].get('directMcpProbes'):
        rows += ['', '| Direct MCP probe (controller) | Result | Tool | Seconds |', '|---|---|---|---:|']
        for name, probe in metrics['calls']['directMcpProbes'].items():
            rows.append(f'| {name} | {probe["status"]} | `{probe["toolId"]}` | {display(probe["elapsedSeconds"])} |')
    rows += ['', *metrics['interpretation']]
    return '\n'.join(rows) + '\n'


def commit_body(metrics, stage):
    current = metrics['stages'][stage]
    return '\n'.join([
        f'RAG mode: {metrics.get("ragMode", "repo-web")}; output status: {metrics.get("outputStatus", "unknown")}',
        f'Fresh start: {metrics.get("freshStart", False)}',
        f'Pending catalog questions: {metrics["progress"].get("pendingQuestions", 0)}; '
            f'roster review pending: {metrics["progress"].get("catalogRosterPending", False)}; '
            f'finalization grace: {metrics["progress"].get("catalogGraceSeconds", 30)} s',
        f'Stage active time: {display(current["activeSeconds"], " s")}',
        f'Run active time: {display(metrics["time"]["activeExecutionSeconds"], " s")}',
        f'Run elapsed since start (includes gaps): {display(metrics["time"]["elapsedSinceStartSeconds"], " s")}',
        f'Reported run tokens: {display(metrics["tokens"]["reportedTotal"])}; known subtotal: '
            f'{metrics["tokens"]["knownTotal"]:,}',
        f'Token-total coverage: {metrics["tokens"]["responsesWithTotal"]}/{metrics["tokens"]["modelResponses"]} responses',
        f'Native probe load time: {display(metrics["time"]["nativeModelLoadSeconds"], " s")}',
        f'Agent calls: {metrics["calls"]["agent"]}; failed: {metrics["calls"]["failedAgent"]}',
        f'Checked task caches: {metrics["progress"]["checkedTaskCaches"]}; cache hits: {metrics["progress"]["cacheHits"]}',
        f'Metrics: runs/{metrics["runId"]}/metrics/{stage}.json',
        'Metrics are captured before publication. Raw reasoning/cache counts are not added to reported totals.'])
