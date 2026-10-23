"""Scoped GitHub authentication compatible with Git 2.25 and newer runners."""
import base64
import os


def git_environment(token='', environment=None):
    source = os.environ if environment is None else environment
    env = {key: value for key, value in source.items()
           if not key.startswith('GIT_TRACE') and key != 'GIT_CURL_VERBOSE'}
    env['GIT_TERMINAL_PROMPT'] = '0'
    if token:
        authorization = base64.b64encode(('x-access-token:' + token).encode()).decode()
        # GIT_CONFIG_COUNT requires Git 2.31. This quoted key=value encoding
        # also works on older runners and is never written into a Git config.
        key = 'http.https://github.com/.extraheader'
        parameters = "'" + key + "=' '" + key + '=AUTHORIZATION: basic ' + authorization + "'"
        env['GIT_CONFIG_PARAMETERS'] = ' '.join(part for part in
            (env.get('GIT_CONFIG_PARAMETERS', ''), parameters) if part)
    return env
