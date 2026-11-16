"""Role names (spec 4.3). ``users.role`` holds one of the three staff roles."""

SUPER_ADMIN = "super_admin"
ORG_ADMIN = "org_admin"
HR_USER = "hr_user"
CANDIDATE = "candidate"  # exam-audience tokens only; never stored in users.role
SYSTEM = "system"  # background jobs

STAFF_ROLES: tuple[str, ...] = (SUPER_ADMIN, ORG_ADMIN, HR_USER)
