"""Database models (spec section 5). Importing this package registers every table on ``Base.metadata``.

Modules import models from here (``from app.db import Invite``); the schema itself is
changed only through ``migrations/`` by the migration owner.
"""

from app.db.base import Base
from app.db.models_assessments import (
    Assessment,
    AssessmentVersion,
    OpenRole,
    Question,
    QuestionBankItem,
    QuestionOption,
    RoleAssessmentLink,
)
from app.db.models_exam import (
    Attempt,
    AttemptAnswer,
    AuditLog,
    Decision,
    GadgetCatalog,
    HelpRequest,
    Invite,
    Notification,
    ProctorEvent,
    ReportRequest,
    UserGadget,
)
from app.db.models_platform import (
    AiUsageLog,
    Asset,
    BrandingTheme,
    Document,
    DocumentVersion,
    FeatureFlag,
    MisuseFlag,
    OrgAiSettings,
    Organization,
    OrgEmailSettings,
    OrgFeatureFlag,
    PasswordHistory,
    RefreshToken,
    User,
)

__all__ = [
    "AiUsageLog",
    "Assessment",
    "AssessmentVersion",
    "Asset",
    "Attempt",
    "AttemptAnswer",
    "AuditLog",
    "Base",
    "BrandingTheme",
    "Decision",
    "Document",
    "DocumentVersion",
    "FeatureFlag",
    "GadgetCatalog",
    "HelpRequest",
    "Invite",
    "MisuseFlag",
    "Notification",
    "OpenRole",
    "OrgAiSettings",
    "OrgEmailSettings",
    "OrgFeatureFlag",
    "Organization",
    "PasswordHistory",
    "ProctorEvent",
    "Question",
    "QuestionBankItem",
    "QuestionOption",
    "RefreshToken",
    "ReportRequest",
    "RoleAssessmentLink",
    "User",
    "UserGadget",
]
