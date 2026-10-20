"""Read caller-owned methodology and endpoint data without importing caller code."""
import copy
import os
from pathlib import Path
from urllib.parse import urlparse

from ci.contracts import read, require


def methodology_source(trusted, dry_run):
    selected = os.environ.get('METHODOLOGY_FILE', '')
    if selected:
        source = Path(selected)
        require(source.is_file() and not source.is_symlink(), 'Pinned caller methodology file is missing or a symlink')
        return source
    # The public repository deliberately does not contain the private manuscript.
    require(dry_run, 'Live initialization requires METHODOLOGY_FILE from the pinned caller definitions checkout')
    return Path(trusted) / 'agent-rules/tests/fixtures/methodology.tex'


def endpoint_config(config):
    """Only remote MCP URLs may come from the caller's configuration file."""
    root = os.environ.get('CONTROLLER_DEFINITIONS', '')
    if not root:
        return config
    source = Path(root) / 'opencode.json'
    require(source.is_file() and not source.is_symlink(), 'Pinned caller endpoint configuration is missing or a symlink')
    configured = read(source).get('mcp', {})
    require(isinstance(configured, dict) and set(configured) == set(config['mcp']),
            'Caller configuration must declare the six supported MCP names')
    result = copy.deepcopy(config)
    for name, entry in configured.items():
        url = entry.get('url') if isinstance(entry, dict) else None
        require(isinstance(url, str), 'Caller MCP URL is missing: ' + name)
        parsed = urlparse(url)
        require(parsed.scheme == 'https' or parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1'),
                'Caller MCP URLs require HTTPS or local HTTP')
        require(bool(parsed.hostname) and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                'Caller MCP URL must not contain credentials, query parameters or fragments')
        result['mcp'][name]['url'] = url
    return result
