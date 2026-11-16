"""Seed the feature flag catalog (spec Appendix E) and the gadget catalog (Appendix D).

Grouped rows in Appendix E are expanded into one key each (22 flags). Every flag defaults
on; only ``watermark`` is hidden from organizations (SA only). Gadget ``available_for``
uses the user role values (``hr_user``, ``org_admin``, ``super_admin``); ``default_order``
follows the catalog order; ``title_key`` is the frontend message key.

Re-running is safe: inserts use ON CONFLICT DO NOTHING so SA-edited defaults survive.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (key, description, default_enabled, visible_to_org)
FEATURE_FLAGS = [
    ("dashboard", "Dashboard", True, True),
    ("dashboard_gadgets", "Customizable dashboard gadgets", True, True),
    ("assessments", "Assessments tab and builder", True, True),
    ("ai_question_builder", "AI question suggestions", True, True),
    ("ai_scoring", "AI scoring of short answers and code gaps", True, True),
    ("question_bank", "Question bank (stretch)", True, True),
    ("roles", "Open roles", True, True),
    ("invites", "Invites", True, True),
    ("csv_bulk_invite", "CSV bulk invite", True, True),
    ("email_sending", "Send links by email (needs SMTP)", True, True),
    ("help_requests", "Candidate help requests", True, True),
    ("camera_proctoring", "Camera checks and snapshots", True, True),
    ("review", "Candidate review and decisions", True, True),
    ("reports_chat", "AI report chat", True, True),
    ("audit_view", "Audit screen", True, True),
    ("audit_export", "Audit exports", True, True),
    ("team", "Team management", True, True),
    ("settings", "Settings section: organization settings", True, True),
    ("org_branding", "Settings section: organization branding", True, True),
    ("policy_documents", "Settings section: policy documents", True, True),
    ("ai_provider_setup", "Settings section: AI provider setup", True, True),
    ("watermark", "Vendor watermark on candidate pages (SA only)", True, False),
]

HR, OA, SA = "hr_user", "org_admin", "super_admin"
# (key, available_for) in Appendix D order; default_order is the 1-based position.
GADGETS = [
    ("summary_counts", [HR, OA, SA]),
    ("needs_attention", [HR, OA]),
    ("open_roles", [HR, OA]),
    ("recent_activity", [HR, OA]),
    ("candidates_per_week", [HR, OA, SA]),
    ("decision_split", [HR, OA, SA]),
    ("quick_actions", [HR, OA]),
    ("expiring_invites", [HR, OA]),
    ("avg_score_by_role", [HR, OA]),
    ("avg_completion_time", [HR, OA]),
    ("my_assessments", [HR]),
    ("proctoring_flags", [HR, OA]),
    ("roles_by_recruiter", [OA]),
    ("security_events", [OA]),
    ("active_flag_notice", [OA]),
    ("orgs_overview", [SA]),
    ("active_misuse_flags", [SA]),
    ("platform_usage", [SA]),
]


def upgrade() -> None:
    conn = op.get_bind()
    insert_flag = sa.text(
        "INSERT INTO feature_flags (key, description, default_enabled, visible_to_org) "
        "VALUES (:key, :description, :default_enabled, :visible_to_org) ON CONFLICT (key) DO NOTHING"
    )
    for key, description, default_enabled, visible in FEATURE_FLAGS:
        conn.execute(
            insert_flag,
            {"key": key, "description": description, "default_enabled": default_enabled, "visible_to_org": visible},
        )
    insert_gadget = sa.text(
        "INSERT INTO gadget_catalog (key, title_key, available_for, default_enabled, default_order) "
        "VALUES (:key, :title_key, :available_for, true, :default_order) ON CONFLICT (key) DO NOTHING"
    )
    for order, (key, roles) in enumerate(GADGETS, start=1):
        conn.execute(
            insert_gadget,
            {"key": key, "title_key": f"gadgets.{key}.title", "available_for": roles, "default_order": order},
        )


def downgrade() -> None:
    op.execute("DELETE FROM gadget_catalog WHERE key IN (" + ", ".join(f"'{k}'" for k, _ in GADGETS) + ")")
    op.execute("DELETE FROM feature_flags WHERE key IN (" + ", ".join(f"'{k}'" for k, *_ in FEATURE_FLAGS) + ")")
