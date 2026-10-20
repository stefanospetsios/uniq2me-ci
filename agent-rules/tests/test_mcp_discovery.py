"""Probe actual MCP protocol schemas without model or tool execution."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import read, write
from ci.mcp_schemas import auth_headers, discover_mcp_tools, probe_search_tools
from ci.runtime import OpenCode


@contextmanager
def mcp_endpoint(empty=False, repeat_cursor=False, search=False, call_error=False, extra_required=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            method = request['method']
            if 'id' not in request:
                self.send_response(202)
                self.end_headers()
                return
            if method == 'initialize':
                result = {'protocolVersion': request['params']['protocolVersion'],
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'CI fixture', 'version': '1'}}
            elif method == 'tools/list':
                cursor = request.get('params', {}).get('cursor')
                name = 'browser.click' if cursor else 'browser_snapshot'
                result = {'tools': [] if empty else [{'name': name, 'description': 'Actual fixture tool schema',
                    'inputSchema': {'type': 'object', 'properties': {'fixture': {'type': 'string'}}}}]}
                if search:
                    required = ['query', 'tenant_id'] if extra_required else ['query']
                    result['tools'] = [{'name': 'web.search' if cursor else 'write_file',
                        'description': 'Synthetic tool execution fixture', 'inputSchema': {
                            'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': required}}]
                if not empty and (not cursor or repeat_cursor):
                    result['nextCursor'] = 'page-two'
            elif method == 'tools/call' and search:
                result = {'isError': call_error, 'content': [
                    {'type': 'text', 'text': 'SYNTHETIC search execution result; no live research'}]}
            else:
                raise AssertionError('Probe attempted to execute an MCP tool: ' + method)
            data = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'type': 'remote', 'url': f'http://127.0.0.1:{server.server_port}/mcp', 'oauth': False}, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class MCPDiscovery(unittest.TestCase):
    def test_direct_probe_executes_the_schema_valid_allowed_search_tool(self):
        with mcp_endpoint(search=True) as (config, requests):
            result = probe_search_tools({'searxng': config}, ['searxng_web_search'], [], seconds=5)['searxng']
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['execution']['toolId'], 'searxng_web_search')
        calls = [request for request in requests if request['method'] == 'tools/call']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['params']['name'], 'web.search')
        self.assertIn('query', calls[0]['params']['arguments'])
        self.assertFalse(result['execution']['isError'])

    def test_direct_probe_error_result_fails_without_executing_again(self):
        with mcp_endpoint(search=True, call_error=True) as (config, requests):
            result = probe_search_tools({'websearch': config}, ['websearch_web_search'], [], seconds=5)['websearch']
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['execution']['isError'])
        self.assertEqual(sum(request['method'] == 'tools/call' for request in requests), 1)

    def test_direct_probe_does_not_guess_missing_required_arguments(self):
        with mcp_endpoint(search=True, extra_required=True) as (config, requests):
            result = probe_search_tools({'websearch': config}, ['websearch_web_search'], [], seconds=5)['websearch']
        self.assertEqual(result['status'], 'failed')
        self.assertIn('no allowed query tool', result['failures'][0]['reason'])
        self.assertFalse(any(request['method'] == 'tools/call' for request in requests))

    def test_real_protocol_paginates_and_never_executes_tools(self):
        with mcp_endpoint() as (config, requests):
            result = discover_mcp_tools({'playwright': config}, [])['playwright']
        self.assertEqual(result['status'], 'discovered')
        self.assertEqual(result['transport'], 'streamable-http')
        self.assertEqual([t['id'] for t in result['toolSchemas']],
                         ['playwright_browser_snapshot', 'playwright_browser_click'])
        self.assertEqual(result['toolSchemas'][0]['parameters']['properties']['fixture'], {'type': 'string'})
        self.assertEqual([r['method'] for r in requests],
                         ['initialize', 'notifications/initialized', 'tools/list', 'tools/list'])

    def test_opencode_builtin_registry_does_not_hide_connected_mcp_schemas(self):
        with tempfile.TemporaryDirectory() as directory, mcp_endpoint() as (config, _), \
             redirect_stdout(io.StringIO()):
            root = Path(directory)
            server = OpenCode(root, ROOT, {'mcp': {'playwright': config}}, root, {})
            server.version, server.runtime_mcp = 'fixture', {'playwright': config}
            server.start, server.stop = Mock(), Mock()
            def api(path, **_):
                if path == '/doc':
                    return {'paths': {}}
                if path.startswith('/experimental/tool?'):
                    return [{'id': 'read', 'description': 'Built-in', 'parameters': {'type': 'object'}}]
                if path == '/mcp':
                    return {'playwright': {'status': 'connected'}}
                raise AssertionError('Unexpected API call: ' + path)
            server.api = Mock(side_effect=api)
            result = server.discover()
            self.assertIn('playwright_browser_snapshot', result['allowedTools'])
            self.assertNotIn('playwright_browser_click', result['allowedTools'])
            self.assertIn('playwright_browser_click', result['deniedTools'])
            self.assertEqual(read(root / 'mcp-capabilities.json')['mcpSchemaDiscovery']['playwright']['status'],
                             'discovered')
            server.start.assert_called_once()

    def test_connected_server_with_no_tools_still_fails_and_saves_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory, mcp_endpoint(empty=True) as (config, _):
            root = Path(directory)
            server = OpenCode(root, ROOT, {'mcp': {'playwright': config}}, root, {})
            server.version, server.runtime_mcp = 'fixture', {'playwright': config}
            server.api = Mock(side_effect=[{'paths': {}}, [], {'playwright': {'status': 'connected'}}])
            with self.assertRaisesRegex(ValueError, 'including direct MCP tools/list'):
                server.discover()
            self.assertEqual(read(root / 'mcp-capabilities.json')['toolSchemas'], [])

    def test_repeated_cursor_is_rejected(self):
        with mcp_endpoint(repeat_cursor=True) as (config, _):
            result = discover_mcp_tools({'playwright': config}, [])['playwright']
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['toolSchemas'], [])
        self.assertIn('repeated a tools/list cursor', result['failures'][0]['reason'])

    def test_oauth_tokens_are_scoped_to_the_matching_endpoint(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_DATA_HOME': directory}):
            url = 'https://fixture.invalid/mcp'
            path = Path(directory) / 'opencode/mcp-auth.json'
            write(path, {'playwright': {'serverUrl': url, 'tokens': {
                'accessToken': 'synthetic-access-token', 'expiresAt': time.time() + 100}}})
            sensitive = []
            self.assertEqual(auth_headers('playwright', {'url': url}, sensitive),
                             {'Authorization': 'Bearer synthetic-access-token'})
            self.assertIn('synthetic-access-token', sensitive)
            self.assertEqual(auth_headers('playwright', {'url': 'https://other.invalid/mcp'}, []), {})
            self.assertEqual(auth_headers('playwright', {'url': url, 'oauth': False}, []), {})
            self.assertEqual(auth_headers('playwright', {'url': url, 'headers': {'Authorization': 'Bearer configured'}}, []),
                             {'Authorization': 'Bearer configured'})
            write(path, {'playwright': {'serverUrl': url, 'tokens': {
                'accessToken': 'synthetic-expired-token', 'expiresAt': time.time() - 1}}})
            with self.assertRaisesRegex(ValueError, 'reauthenticate'):
                auth_headers('playwright', {'url': url}, [])


if __name__ == '__main__':
    unittest.main()
