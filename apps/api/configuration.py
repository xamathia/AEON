"""Strict local configuration data with no import-time environment loading."""

from collections.abc import Mapping
import os
import re


_ALLOWED_KEYS = (
    'AEON_GOOGLE_CLIENT_ID',
    'AEON_GOOGLE_CLIENT_SECRET',
    'AEON_GOOGLE_REDIRECT_URI',
    'AEON_GOOGLE_ROUTES_API_KEY',
    'AEON_ANTHROPIC_API_KEY',
    'AEON_ANTHROPIC_MODEL',
)
_MAX_FILE_BYTES = 65536
_KEY_PATTERN = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


class Configuration:
    """An allowlisted copy of configuration values with a redacted repr."""

    def __init__(self, values, valid=True):
        self.values = {}
        self.valid = valid is True
        if not self.valid:
            return
        try:
            if not isinstance(values, Mapping):
                raise ValueError()
            selected = {key: values[key] for key in _ALLOWED_KEYS if key in values}
            if any(type(value) is not str for value in selected.values()):
                raise ValueError()
            self.values = selected
        except Exception:
            self.valid = False

    def __repr__(self):
        return '<Configuration valid={}>'.format(self.valid)


def _parse_file(raw):
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError()
    text = raw.decode('utf-8').replace('\r\n', '\n')
    if any((ord(char) < 32 and char not in '\t\n') or ord(char) == 127 for char in text):
        raise ValueError()
    values = {}
    seen = set()
    for raw_line in text.split('\n'):
        line = raw_line.strip(' \t')
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        key = key.strip(' \t')
        if not separator or not _KEY_PATTERN.fullmatch(key) or key in seen:
            raise ValueError()
        seen.add(key)
        value = value.strip(' \t')
        if value.startswith(('"', "'")):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote or quote in value[1:-1]:
                raise ValueError()
            value = value[1:-1]
        elif '"' in value or "'" in value:
            raise ValueError()
        if key in _ALLOWED_KEYS:
            values[key] = value
    return values


def _file_values(env_path):
    try:
        stream = open(env_path, 'rb')
    except FileNotFoundError:
        return {}
    with stream:
        raw = stream.read(_MAX_FILE_BYTES + 1)
    return _parse_file(raw)


def load_configuration(*, environ=None, env_path=None) -> Configuration:
    """Load an optional data file, then overlay allowed runtime environment keys.

    Only whole-value matching quotes and full-line comments are syntax. Dollar
    signs, backticks, backslashes and inline hashes remain literal data. A bad
    file invalidates the entire configuration, even with environment overrides.
    """
    try:
        values = {} if env_path is None else _file_values(env_path)
        environment = Configuration(os.environ if environ is None else environ)
        if not environment.valid:
            return Configuration({}, valid=False)
        values.update(environment.values)
        return Configuration(values)
    except Exception:
        # Filesystem and decoder errors can contain credentials or raw contents.
        return Configuration({}, valid=False)
