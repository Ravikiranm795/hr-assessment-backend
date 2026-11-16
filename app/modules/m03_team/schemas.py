"""M3 request and response models (no secrets ever appear here except the one-time temp password)."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, computed_field

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Locale = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})?$", max_length=16)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]

_USER_EXAMPLE = {
    "id": "0b6f5c0e-5d3a-4c8e-9a64-2f1c3e8d7a10",
    "org_id": "7d1b8f1e-3c5a-4e0b-8a52-9d6f0c2b1e44",
    "name": "Hana Recruiter",
    "email": "hana@example.com",
    "role": "hr_user",
    "status": "active",
    "locale": "en",
    "must_change_password": True,
    "totp_enabled": False,
    "last_login_at": None,
    "locked_until": None,
    "is_locked": False,
    "created_at": "2026-11-16T09:30:00Z",
}


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, json_schema_extra={"examples": [_USER_EXAMPLE]})

    id: UUID
    org_id: UUID | None
    name: str
    email: str
    role: str
    status: str
    locale: str | None
    must_change_password: bool
    totp_enabled: bool
    last_login_at: datetime | None
    locked_until: datetime | None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > datetime.now(UTC)


class OwnedRoleOut(BaseModel):
    id: UUID
    title: str
    status: str
    owner_id: UUID
    owner_inactive: bool = Field(description="True while the owning user is deactivated and the role is not reassigned.")


class UserDetailOut(UserOut):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    **_USER_EXAMPLE,
                    "owned_roles": [
                        {
                            "id": "a3c9d2f0-1b7e-4f6a-8d35-0e4b9c7a2f11",
                            "title": "Backend Engineer",
                            "status": "open",
                            "owner_id": _USER_EXAMPLE["id"],
                            "owner_inactive": False,
                        }
                    ],
                }
            ]
        },
    )

    owned_roles: list[OwnedRoleOut] = []


class DeactivatedUserOut(UserDetailOut):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={"examples": [{**_USER_EXAMPLE, "status": "inactive", "owned_roles": [], "reassigned_roles": 2}]},
    )

    reassigned_roles: int = Field(0, description="Roles moved to reassign_roles_to during this deactivation.")


class UserCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"name": "Hana Recruiter", "email": "hana@example.com", "locale": "en"}]}
    )

    name: Name
    email: EmailStr
    locale: Locale | None = None
    role: str | None = Field(
        None,
        description="Optional and fixed to hr_user. Any admin role is refused with ADMIN_ONLY_ACTION.",
    )


class UserWithTempPasswordOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"user": _USER_EXAMPLE, "temporary_password": "q7Hn-Vb3x!Rt9LmZ"}]}
    )

    user: UserOut
    temporary_password: str = Field(description="Shown once. It is never stored in clear text nor returned again.")


class UserUpdate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "Hana R.", "locale": "en"}]})

    name: Name | None = None
    locale: Locale | None = None
    email: EmailStr | None = Field(None, description="Allowed only before the user's first login.")
    role: str | None = Field(None, description="Promotion is never possible here: any value is ADMIN_ONLY_ACTION.")


class DeactivateIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"reassign_roles_to": "5e2f0a1c-9d4b-4c7e-8f31-6a0b2d9c1e77", "reason": "Left the company"}]}
    )

    reassign_roles_to: UUID | None = Field(
        None, description="Active user of the same organization who takes over the roles this user owns."
    )
    reason: Reason | None = None


class TempPasswordOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"user_id": _USER_EXAMPLE["id"], "temporary_password": "q7Hn-Vb3x!Rt9LmZ", "must_change_password": True}
            ]
        }
    )

    user_id: UUID
    temporary_password: str = Field(description="Shown once. The user must change it at first login.")
    must_change_password: bool = True
