"""QSettings-backed session persistence.

This is the only module that reads/writes QSettings for auth state.
Key names match the existing LOC plugin exactly (see api_contract.md).
"""

from qgis.core import QgsSettings

from ..models.user import Organization, User

# QSettings key constants — must match the old plugin for compatibility.
_KEY_TOKEN = "LOC/auth_token"
_KEY_USER_ID = "LOC/user_id"
_KEY_USER_NAME = "LOC/user_name"
_KEY_USER_EMAIL = "LOC/user_email"
_KEY_USER_ROLE = "LOC/user_role"
_KEY_ORG_NAME = "LOC/org_name"
_KEY_ORG_ID = "LOC/org_id"
_KEY_2FA = "LOC/enabled_2fa"

_ALL_KEYS = (
    _KEY_TOKEN, _KEY_USER_ID, _KEY_USER_NAME, _KEY_USER_EMAIL,
    _KEY_USER_ROLE, _KEY_ORG_NAME, _KEY_ORG_ID, _KEY_2FA,
)


def save_session(token: str, user: User) -> None:
    """Persist token and user info to QSettings."""
    s = QgsSettings()
    s.setValue(_KEY_TOKEN, token)
    s.setValue(_KEY_USER_ID, user.user_id)
    s.setValue(_KEY_USER_NAME, user.full_name)
    s.setValue(_KEY_USER_EMAIL, user.email)
    s.setValue(_KEY_USER_ROLE, user.role)
    s.setValue(_KEY_ORG_NAME, user.organization.name)
    s.setValue(_KEY_ORG_ID, user.organization.org_id)
    s.setValue(_KEY_2FA, user.enabled_2fa)


def restore_session():
    """Read stored session from QSettings.

    Returns (token, User) if a token exists, or (None, None) if not.
    """
    s = QgsSettings()
    token = s.value(_KEY_TOKEN, None)
    if not token:
        return None, None

    org = Organization(
        org_id=s.value(_KEY_ORG_ID, ""),
        name=s.value(_KEY_ORG_NAME, ""),
    )
    user = User(
        user_id=s.value(_KEY_USER_ID, ""),
        full_name=s.value(_KEY_USER_NAME, ""),
        email=s.value(_KEY_USER_EMAIL, ""),
        role=s.value(_KEY_USER_ROLE, ""),
        enabled_2fa=bool(s.value(_KEY_2FA, False)),
        organization=org,
    )
    return token, user


def clear_session() -> None:
    """Remove all LOC session keys from QSettings."""
    s = QgsSettings()
    for key in _ALL_KEYS:
        s.remove(key)
