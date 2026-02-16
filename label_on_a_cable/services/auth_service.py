"""Authentication orchestration.

Ties together ApiClient (network), session (QSettings persistence),
and User model.  Exposes login / logout / restore for the plugin.
"""

from typing import Optional

from ..models.user import User
from .api_client import ApiClient
from .session import clear_session, restore_session, save_session


class AuthService:
    """High-level auth operations for the plugin."""

    def __init__(self, api_client: ApiClient):
        self.api = api_client
        self.current_user: Optional[User] = None
        self.token: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self, email: str, password: str, otp: Optional[str] = None):
        """Authenticate against LOC.

        On success: stores token on api_client, persists to QSettings,
        sets self.current_user / self.token.

        Raises OTPRequiredException if 2FA code is needed.
        Raises AuthenticationException on bad credentials.
        Raises NetworkException / ServerException on infra errors.
        """
        data = self.api.login(email, password, otp)

        token = data.get("token", "")
        user = User.from_api(data.get("user", {}))

        # Apply token to the HTTP session for subsequent calls
        self.api.set_token(token)

        # Persist to QSettings so the session survives QGIS restart
        save_session(token, user)

        self.token = token
        self.current_user = user

    def logout(self):
        """Client-side logout (no server endpoint).

        Clears the Bearer header, wipes QSettings, resets local state.
        """
        self.api.clear_token()
        clear_session()
        self.token = None
        self.current_user = None

    def try_restore(self) -> bool:
        """Attempt to restore a previous session from QSettings.

        Returns True if a token was found and re-applied, False otherwise.
        Does NOT validate the token against the server.
        """
        token, user = restore_session()
        if token and user:
            self.api.set_token(token)
            self.token = token
            self.current_user = user
            return True
        return False

    @property
    def is_logged_in(self) -> bool:
        return self.token is not None
