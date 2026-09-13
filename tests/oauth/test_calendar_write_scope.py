"""The write consent must never expand either read-only provider."""
import unittest
from urllib.parse import parse_qs, urlsplit
from packages.aeon_oauth import GoogleOAuth

class WriteScopeTests(unittest.TestCase):
    def test_provider_scopes_are_separate(self):
        expected = {'google_calendar':'calendar.events.readonly', 'gmail':'gmail.readonly', 'google_calendar_write':'calendar.events'}
        for provider, suffix in expected.items():
            oauth = GoogleOAuth('fixture', 'http://127.0.0.1:8787/', provider=provider)
            query = parse_qs(urlsplit(oauth.begin('session')['authorization_url']).query)
            self.assertEqual(query['scope'], ['https://www.googleapis.com/auth/' + suffix])
            self.assertEqual(query['code_challenge_method'], ['S256'])

if __name__ == '__main__': unittest.main()
