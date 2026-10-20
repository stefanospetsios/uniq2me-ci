"""Source transport and unavailable-document checks, separate from evidence admission."""
import html
import re
from urllib.parse import urlsplit, urlunsplit


def source_request_url(url):
    """Request official HTTP ELI identifiers over HTTPS without changing provenance."""
    parsed = urlsplit(url)
    if (parsed.scheme == 'http' and parsed.netloc.lower() == 'data.europa.eu' and
            re.fullmatch(r'/eli/(?:reg(?:_del|_impl)?|dir|dec)/[0-9]{4}/[0-9]+/'
                         r'(?:oj|[0-9]{4}-[0-9]{2}-[0-9]{2})(?:/[a-z]{2,3})?(?:/(?:pdf|html))?/?', parsed.path)):
        return urlunsplit(('https', 'data.europa.eu', parsed.path, parsed.query, parsed.fragment))
    return url


def ensure_source_available(content):
    """HTTP success is insufficient when the response is empty or a bot challenge."""
    normalized = ' '.join(html.unescape(content).lower().replace('’', "'").split())
    if not normalized:
        raise OSError('Source document is unavailable or incomplete: empty response text')
    # Match challenge-page combinations, not ordinary documentation mentioning JS.
    if len(normalized) <= 12000 and (
            ('javascript' in normalized and re.search(
                r'verify (?:that )?you(?: are|\'re) not a robot', normalized)) or
            ('enable javascript and cookies to continue' in normalized and
                ('just a moment' in normalized or 'checking your browser' in normalized)) or
            (normalized.startswith('checking your browser before accessing') and
                ('automatically' in normalized or 'redirect' in normalized))):
        raise OSError('Source document is unavailable or incomplete: JavaScript/bot challenge page; retry the document download')
