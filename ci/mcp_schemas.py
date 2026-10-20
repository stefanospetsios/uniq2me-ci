"""Read MCP schemas and optionally execute explicit read-only search probes."""
import asyncio
import json
import os
import re
import time
from datetime import timedelta
from pathlib import Path

from ci.contracts import require


def auth_headers(name, config, sensitive):
    headers = dict(config.get('headers', {}))
    sensitive.extend(headers.values())
    for key, value in headers.items():
        if key.lower() == 'authorization':
            credential = re.fullmatch(r'(?i)(?:bearer|basic)\s+(.+)', value)
            if credential:
                sensitive.append(credential[1])
    if any(key.lower() == 'authorization' for key in headers) or config.get('oauth') is False:
        return headers
    # OpenCode refreshes its OAuth credentials when establishing /mcp connections.
    # Reuse only the resulting access token for this exact configured endpoint.
    data_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    path = data_home / 'opencode/mcp-auth.json'
    if not path.exists():
        return headers
    entry = json.loads(path.read_text(encoding='utf-8')).get(name, {})
    if entry.get('serverUrl') != config['url']:
        return headers
    tokens = entry.get('tokens', {})
    token = tokens.get('accessToken')
    if not token:
        return headers
    require(not tokens.get('expiresAt') or tokens['expiresAt'] > time.time(),
            f'MCP {name} OAuth access token expired; reauthenticate it on the runner')
    sensitive.append(token)
    headers['Authorization'] = 'Bearer ' + token
    return headers


class ToolProbeError(ValueError):
    pass


async def _list_session(streams, name, search_query=None, allowed_tools=(), call_seconds=120):
    from mcp import ClientSession, types

    async with ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=30)) as session:
        initialized = await session.initialize()
        tools, cursor, cursors, ids = [], None, set(), set()
        while True:
            page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
            for tool in page.tools:
                # Use the same namespace sanitization as OpenCode's MCP registry.
                tool_id = re.sub(r'[^a-zA-Z0-9_-]', '_', name) + '_' + re.sub(
                    r'[^a-zA-Z0-9_-]', '_', tool.name)
                require(tool_id not in ids, f'MCP {name} exposes duplicate normalized tool IDs')
                ids.add(tool_id)
                tools.append({'id': tool_id, 'description': tool.description or '',
                    'parameters': tool.inputSchema, 'mcpTool': tool.model_dump(mode='json', exclude_none=True),
                    'schemaSource': 'mcp-tools/list'})
            cursor = page.nextCursor
            if not cursor:
                break
            require(cursor not in cursors, f'MCP {name} repeated a tools/list cursor')
            cursors.add(cursor)
        observed = {'toolSchemas': tools, 'protocolVersion': initialized.protocolVersion,
                    'serverInfo': initialized.serverInfo.model_dump(mode='json')}
        if search_query is not None:
            from jsonschema import Draft202012Validator
            candidates = []
            for tool in tools:
                schema = tool['parameters']
                if tool['id'] not in allowed_tools or 'query' not in schema.get('properties', {}):
                    continue
                arguments = {'query': search_query}
                for key in schema.get('required', []):
                    if key not in arguments and 'default' in schema.get('properties', {}).get(key, {}):
                        arguments[key] = schema['properties'][key]['default']
                if Draft202012Validator(schema).is_valid(arguments):
                    candidates.append((tool, arguments))
            if not candidates:
                raise ToolProbeError(f'MCP {name} has no allowed query tool whose schema accepts the probe arguments')
            tool, arguments = sorted(candidates, key=lambda item: item[0]['id'])[0]
            started = time.monotonic()
            result = await session.call_tool(tool['mcpTool']['name'], arguments=arguments,
                                            read_timeout_seconds=timedelta(seconds=call_seconds))
            observed['execution'] = {'toolId': tool['id'], 'nativeToolName': tool['mcpTool']['name'],
                'arguments': arguments, 'elapsedSeconds': round(time.monotonic() - started, 3),
                'isError': result.isError, 'result': result.model_dump(mode='json', exclude_none=True)}
        return observed


async def _attempt(name, url, headers, transport, search_query=None, allowed_tools=(), call_seconds=120):
    import httpx
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    if transport == 'streamable-http':
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30), follow_redirects=False) as client:
            async with streamable_http_client(url, http_client=client) as streams:
                return await _list_session(streams, name, search_query, allowed_tools, call_seconds)
    async with sse_client(url, headers=headers, timeout=30, sse_read_timeout=30) as streams:
        return await _list_session(streams, name, search_query, allowed_tools, call_seconds)


def _error_text(error):
    nested = getattr(error, 'exceptions', None)
    return '; '.join(_error_text(item) for item in nested) if nested else str(error)


def _contains_probe_error(error):
    return isinstance(error, ToolProbeError) or any(
        _contains_probe_error(item) for item in getattr(error, 'exceptions', []))


async def _discover(name, config, sensitive):
    failures = []
    try:
        require(config.get('type') == 'remote', f'MCP {name} must use a configured remote endpoint')
        headers = auth_headers(name, config, sensitive)
        for transport in ('streamable-http', 'sse'):
            try:
                result = await asyncio.wait_for(_attempt(name, config['url'], headers, transport), timeout=45)
                result.update(status='discovered', transport=transport)
                return name, result
            except Exception as error:
                failures.append({'transport': transport, 'reason': _error_text(error)})
    except Exception as error:
        failures.append({'reason': _error_text(error)})
    return name, {'status': 'failed', 'failures': failures, 'toolSchemas': []}


def discover_mcp_tools(configured, sensitive):
    async def collect():
        return dict(await asyncio.gather(*(
            _discover(name, config, sensitive) for name, config in sorted(configured.items()))))
    return asyncio.run(collect())


async def _probe_search(name, config, allowed_tools, sensitive, seconds):
    failures = []
    started = time.monotonic()
    try:
        require(name in ('searxng', 'websearch'), 'Direct preflight execution is limited to search MCPs')
        headers = auth_headers(name, config, sensitive)
        query = 'Ollama keep_alive official documentation'
        for transport in ('streamable-http', 'sse'):
            remaining = seconds - (time.monotonic() - started)
            require(remaining > 0, f'MCP {name} direct tool probe exhausted its allowance')
            try:
                result = await asyncio.wait_for(_attempt(name, config['url'], headers, transport,
                    query, allowed_tools, remaining), timeout=remaining)
                # isError is an application failure, not a transport failure. Do
                # not invoke the same tool again through another transport.
                result.update(status='failed' if result['execution']['isError'] else 'passed', transport=transport)
                return name, result
            except Exception as error:
                failures.append({'transport': transport, 'reason': _error_text(error)})
                if _contains_probe_error(error):
                    break
    except Exception as error:
        failures.append({'reason': _error_text(error)})
    return name, {'status': 'failed', 'failures': failures}


def probe_search_tools(configured, allowed_tools, sensitive, seconds=120):
    async def collect():
        return dict(await asyncio.gather(*(
            _probe_search(name, config, allowed_tools, sensitive, seconds)
            for name, config in sorted(configured.items()))))
    return asyncio.run(collect())
