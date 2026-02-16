"""User and Organization domain models."""

from dataclasses import dataclass, field


@dataclass
class Organization:
    org_id: str = ""
    name: str = ""
    lite: bool = False


@dataclass
class User:
    user_id: str = ""
    full_name: str = ""
    email: str = ""
    role: str = ""
    enabled_2fa: bool = False
    organization: Organization = field(default_factory=Organization)

    @classmethod
    def from_api(cls, data: dict) -> "User":
        """Build a User from the login response ``user`` dict."""
        org_data = data.get("organization") or {}
        org = Organization(
            org_id=str(data.get("org_id", "")),
            name=org_data.get("name", ""),
            lite=bool(org_data.get("lite", False)),
        )
        return cls(
            user_id=str(data.get("user_id", "")),
            full_name=data.get("fullName", ""),
            email=data.get("email", ""),
            role=data.get("role", ""),
            enabled_2fa=bool(data.get("enabled2fa", False)),
            organization=org,
        )
