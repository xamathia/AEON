"""Provider-specific, memory-only Google OAuth for local desktop use."""
from .client import GoogleOAuth
from .errors import OAuthError
from .transport import TokenTransport

__all__ = ['GoogleOAuth', 'OAuthError', 'TokenTransport']
