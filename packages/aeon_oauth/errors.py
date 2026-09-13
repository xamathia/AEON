"""Public errors contain fixed messages, never upstream response details."""
_MESSAGES = {
    'invalid_configuration': 'Invalid local OAuth configuration.',
    'invalid_session': 'Invalid local session.',
    'session_limit': 'The local session limit has been reached. Forget a session before trying again.',
    'invalid_state': 'This connection attempt is invalid or has already been used.',
    'expired_state': 'This connection attempt has expired. Start connecting again.',
    'invalid_callback': 'The connection response is invalid. Start connecting again.',
    'authorization_denied': 'The Google connection was not authorized.',
    'invalid_response': 'The authorization service response is invalid.',
    'insufficient_scope': 'The requested access was not granted.',
    'transport_error': 'The authorization service cannot be reached. Try again.',
    'invalid_grant': 'The Google authorization is no longer valid. Reconnect this source.',
    'token_rejected': 'The Google service rejected the authorization request.',
    'not_connected': 'This Google source must be reconnected.',
    'oauth_error': 'The Google connection could not be completed.',
}


class OAuthError(Exception):
    """An allowlisted public code and a fixed, redacted English message."""

    def __init__(self, code):
        self.code = code if type(code) is str and code in _MESSAGES else 'oauth_error'
        super().__init__(_MESSAGES[self.code])
