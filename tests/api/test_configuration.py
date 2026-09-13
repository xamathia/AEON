"""Configuration fixtures never read the developer's environment or .env."""

import importlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


CLIENT_ID = 'AEON_GOOGLE_CLIENT_ID'
CLIENT_SECRET = 'AEON_GOOGLE_CLIENT_SECRET'
REDIRECT = 'AEON_GOOGLE_REDIRECT_URI'
ROUTES_KEY = 'AEON_GOOGLE_ROUTES_API_KEY'
SECRET = 'fixture-credential-never-report'


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('apps.api.configuration')

    def fixture(self, contents):
        directory = tempfile.TemporaryDirectory(prefix='aeon-configuration-test-')
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'fixture.env'
        path.write_bytes(contents if isinstance(contents, bytes) else contents.encode('utf-8'))
        return path

    def load(self, environ, **kwargs):
        return self.module.load_configuration(environ=environ, **kwargs)

    def test_constructor_copies_only_allowlisted_string_values(self):
        source = {CLIENT_ID: 'fixture-client', CLIENT_SECRET: SECRET,
                  REDIRECT: 'http://127.0.0.1:8787/', ROUTES_KEY: 'fixture-routes',
                  'UNRELATED_KEY': 'ignored'}
        configuration = self.module.Configuration(source)
        source[CLIENT_ID] = 'changed-after-construction'
        self.assertTrue(configuration.valid)
        self.assertEqual(configuration.values, {
            CLIENT_ID: 'fixture-client', CLIENT_SECRET: SECRET,
            REDIRECT: 'http://127.0.0.1:8787/', ROUTES_KEY: 'fixture-routes',
        })

    def test_constructor_rejects_nonstring_values_without_converting_them(self):
        class UnsafeValue:
            def __str__(self):
                raise AssertionError(SECRET)

        for value in (None, 3, True, b'bytes', UnsafeValue()):
            with self.subTest(kind=type(value).__name__):
                configuration = self.module.Configuration({CLIENT_SECRET: value})
                self.assertFalse(configuration.valid)
                self.assertEqual(configuration.values, {})

    def test_invalid_configuration_does_not_keep_partial_credentials(self):
        configuration = self.module.Configuration({CLIENT_SECRET: SECRET}, valid=False)
        self.assertFalse(configuration.valid)
        self.assertEqual(configuration.values, {})

    def test_repr_and_string_do_not_disclose_credentials(self):
        configuration = self.module.Configuration({CLIENT_SECRET: SECRET, ROUTES_KEY: SECRET})
        self.assertNotIn(SECRET, repr(configuration))
        self.assertNotIn(SECRET, str(configuration))

    def test_default_file_path_does_not_open_any_file(self):
        with patch('builtins.open', side_effect=AssertionError('Unexpected fixture file read')):
            configuration = self.load({CLIENT_ID: 'fixture-client'})
        self.assertTrue(configuration.valid)
        self.assertEqual(configuration.values, {CLIENT_ID: 'fixture-client'})

    def test_missing_fixture_file_is_valid_and_environment_is_still_used(self):
        existing = self.fixture('')
        configuration = self.load({CLIENT_ID: 'fixture-client'}, env_path=existing.parent / 'missing.env')
        self.assertTrue(configuration.valid)
        self.assertEqual(configuration.values, {CLIENT_ID: 'fixture-client'})

    def test_missing_fixture_and_empty_environment_produce_valid_empty_configuration(self):
        existing = self.fixture('')
        configuration = self.load({}, env_path=existing.parent / 'missing.env')
        self.assertTrue(configuration.valid)
        self.assertEqual(configuration.values, {})

    def test_environment_overrides_file_even_when_empty(self):
        path = self.fixture(CLIENT_ID + '=file-client\n' + CLIENT_SECRET + '=file-secret\n'
                            + ROUTES_KEY + '=file-routes\n')
        configuration = self.load({CLIENT_ID: 'environment-client', CLIENT_SECRET: '',
                                   'UNRELATED_KEY': 'ignored'}, env_path=path)
        self.assertTrue(configuration.valid)
        self.assertEqual(configuration.values, {CLIENT_ID: 'environment-client', CLIENT_SECRET: '',
                                                ROUTES_KEY: 'file-routes'})

    def test_matching_quotes_whitespace_comments_and_empty_values_are_data(self):
        path = self.fixture('  # Commentaire de fixture\r\n\r\n  ' + CLIENT_ID + ' = "fixture client"  \r\n'
                            + CLIENT_SECRET + "='literal # value'\r\n" + ROUTES_KEY + '=\r\n'
                            + 'OTHER_VALID_KEY=ignored\r\n')
        configuration = self.load({}, env_path=path)
        self.assertTrue(configuration.valid)
        self.assertEqual(configuration.values, {CLIENT_ID: 'fixture client',
                                                CLIENT_SECRET: 'literal # value', ROUTES_KEY: ''})

    def test_shell_like_text_is_preserved_without_execution_or_interpolation(self):
        path = self.fixture('')
        marker = path.parent / 'must-not-be-created'
        command = '$(touch ' + str(marker) + ')'
        literal = '${UNRELATED_KEY} `uname` \\n # literal'
        path.write_text(CLIENT_ID + '=' + command + '\n' + CLIENT_SECRET + "='" + literal + "'\n",
                        encoding='utf-8')
        configuration = self.load({'UNRELATED_KEY': 'must-not-interpolate'}, env_path=path)
        self.assertTrue(configuration.valid)
        self.assertEqual(configuration.values, {CLIENT_ID: command, CLIENT_SECRET: literal})
        self.assertFalse(marker.exists())

    def test_malformed_text_quotes_and_duplicate_keys_invalidate_whole_file(self):
        invalid = (
            'not-an-assignment', 'export ' + CLIENT_ID + '=fixture', 'BAD-KEY=fixture',
            '=fixture', CLIENT_ID + '=\"unterminated', CLIENT_ID + "='mismatched\"",
            CLIENT_ID + '=\"closed\" trailing', CLIENT_ID + '=\"one\"two\"',
            CLIENT_ID + '=one\n' + CLIENT_ID + '=two',
            'OTHER_VALID_KEY=one\nOTHER_VALID_KEY=two',
            CLIENT_ID + '=value\x00hidden', CLIENT_ID + '=value\x1bhidden',
            CLIENT_ID + '=value\vOTHER_VALID_KEY=another',
            '\ufeff' + CLIENT_ID + '=fixture',
        )
        for index, text in enumerate(invalid):
            with self.subTest(case=index):
                path = self.fixture(CLIENT_SECRET + '=' + SECRET + '\n' + text)
                configuration = self.load({CLIENT_ID: 'environment-client'}, env_path=path)
                self.assertFalse(configuration.valid)
                self.assertEqual(configuration.values, {})
                self.assertNotIn(SECRET, repr(configuration))

    def test_invalid_utf8_is_rejected_without_exposing_decode_errors(self):
        path = self.fixture(CLIENT_SECRET.encode('ascii') + b'=' + SECRET.encode('ascii') + b'\xff')
        configuration = self.load({}, env_path=path)
        self.assertFalse(configuration.valid)
        self.assertEqual(configuration.values, {})
        self.assertNotIn(SECRET, repr(configuration))

    def test_file_size_limit_is_65536_bytes(self):
        for size, valid in ((65536, True), (65537, False)):
            with self.subTest(size=size):
                path = self.fixture(b'#' + b'x' * (size - 2) + b'\n')
                configuration = self.load({}, env_path=path)
                self.assertEqual(configuration.valid, valid)
                self.assertEqual(configuration.values, {})

    def test_file_read_is_bounded_before_decoding(self):
        path = self.fixture(b'# fixture\n')
        class ObservedStream(io.BytesIO):
            def read(inner, size=-1):
                inner.read_size = size
                return super().read(size)
        stream = ObservedStream(b'# fixture\n')
        with patch('builtins.open', return_value=stream):
            configuration = self.load({}, env_path=path)
        self.assertTrue(configuration.valid)
        self.assertEqual(stream.read_size, 65537)
        self.assertTrue(stream.closed)

    def test_permission_and_io_failures_return_invalid_configuration(self):
        path = self.fixture(CLIENT_SECRET + '=' + SECRET)
        for error in (PermissionError(SECRET), OSError(SECRET)):
            with self.subTest(kind=type(error).__name__):
                with patch('builtins.open', side_effect=error):
                    configuration = self.load({CLIENT_ID: 'environment-client'}, env_path=path)
                self.assertFalse(configuration.valid)
                self.assertEqual(configuration.values, {})
                self.assertNotIn(SECRET, repr(configuration))

    def test_environment_requires_string_values_only_for_allowed_keys(self):
        configuration = self.load({CLIENT_SECRET: object()})
        self.assertFalse(configuration.valid)
        self.assertEqual(configuration.values, {})
        ignored = self.load({'UNRELATED_KEY': object()})
        self.assertTrue(ignored.valid)
        self.assertEqual(ignored.values, {})

    def test_default_environment_is_read_at_call_time_from_fake_mapping_only(self):
        with patch('os.environ', {CLIENT_ID: 'first-fake-environment'}):
            first = self.load(None)
        with patch('os.environ', {CLIENT_ID: 'second-fake-environment'}):
            second = self.load(None)
        self.assertEqual(first.values, {CLIENT_ID: 'first-fake-environment'})
        self.assertEqual(second.values, {CLIENT_ID: 'second-fake-environment'})

    def test_import_does_not_read_environment(self):
        class UnreadableEnvironment:
            def __iter__(self):
                raise AssertionError('Fixture environment was read at import time')
            def __getitem__(self, key):
                raise AssertionError('Fixture environment was read at import time')
            def get(self, key, default=None):
                raise AssertionError('Fixture environment was read at import time')
        spec = importlib.util.spec_from_file_location('configuration_import_fixture', self.module.__file__)
        isolated_module = importlib.util.module_from_spec(spec)
        with patch('os.environ', UnreadableEnvironment()):
            spec.loader.exec_module(isolated_module)


if __name__ == '__main__':
    unittest.main()
