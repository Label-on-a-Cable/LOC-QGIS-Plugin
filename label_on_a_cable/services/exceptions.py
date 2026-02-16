"""LOC API exception hierarchy.

LOCAPIException (base)
  +-- AuthenticationException
  |     +-- OTPRequiredException
  +-- NetworkException
  +-- ServerException
  +-- ValidationException
"""


class LOCAPIException(Exception):
    """Base exception for all LOC API errors."""

    def __init__(self, message="An API error occurred", status_code=None):
        self.status_code = status_code
        super().__init__(message)


class AuthenticationException(LOCAPIException):
    """Raised on 401/403 or invalid credentials."""

    def __init__(self, message="Authentication failed", status_code=None):
        super().__init__(message, status_code)


class OTPRequiredException(AuthenticationException):
    """Raised when the server requests a 2FA code."""

    requires_otp = True

    def __init__(self, message="Check your email for the verification code"):
        super().__init__(message)


class NetworkException(LOCAPIException):
    """Raised on connection/timeout errors (no HTTP response)."""

    def __init__(self, message="Network error — check your connection"):
        super().__init__(message)


class ServerException(LOCAPIException):
    """Raised on 5xx server errors.

    Carries diagnostic metadata so callers (tasks, UI) can surface
    actionable details without re-parsing the error string.
    """

    def __init__(
        self,
        message="Server error — try again later",
        status_code=None,
        request_url="",
        request_id="",
        elapsed_seconds=0.0,
        response_path="",
    ):
        super().__init__(message, status_code)
        self.request_url = request_url
        self.request_id = request_id
        self.elapsed_seconds = elapsed_seconds
        self.response_path = response_path


class ValidationException(LOCAPIException):
    """Raised on 400 validation errors."""

    def __init__(self, message="Validation error", status_code=400):
        super().__init__(message, status_code)
