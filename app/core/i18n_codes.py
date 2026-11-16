"""Message keys the frontend translates. The backend never returns display text.

Error message keys follow ``errors.<area>.<name>`` (spec Appendix A). Validation field codes
are the ``code`` values inside ``VALIDATION_ERROR`` ``details.fields``.
"""

# code -> (message key, default HTTP status). Exactly the Appendix A catalog.
ERROR_CATALOG: dict[str, tuple[str, int]] = {
    "UNAUTHENTICATED": ("errors.common.unauthenticated", 401),
    "FORBIDDEN": ("errors.common.forbidden", 403),
    "NOT_FOUND": ("errors.common.not_found", 404),
    "VALIDATION_ERROR": ("errors.common.validation", 422),
    "CONFLICT": ("errors.common.conflict", 409),
    "RATE_LIMITED": ("errors.common.rate_limited", 429),
    "INVALID_CREDENTIALS": ("errors.auth.invalid_credentials", 401),
    "ACCOUNT_LOCKED": ("errors.auth.account_locked", 423),
    "MUST_CHANGE_PASSWORD": ("errors.auth.must_change_password", 403),
    "PASSWORD_POLICY_FAILED": ("errors.auth.password_policy_failed", 422),
    "ORG_DISABLED": ("errors.org.disabled", 403),
    "FEATURE_DISABLED": ("errors.common.feature_disabled", 403),
    "ADMIN_ONLY_ACTION": ("errors.common.admin_only_action", 403),
    "LAST_ADMIN_PROTECTED": ("errors.team.last_admin_protected", 409),
    "PROTECTED_ACCOUNT": ("errors.team.protected_account", 403),
    "ASSESSMENT_IMMUTABLE": ("errors.assessment.immutable", 409),
    "ASSESSMENT_NOT_PUBLISHED": ("errors.assessment.not_published", 409),
    "VERSION_LIMIT_REACHED_ALL_IN_USE": ("errors.assessment.version_limit_all_in_use", 409),
    "DRAFT_ALREADY_EXISTS": ("errors.assessment.draft_already_exists", 409),
    "QUESTION_INVALID": ("errors.question.invalid", 422),
    "MODEL_ANSWER_REQUIRED": ("errors.question.model_answer_required", 422),
    "ROLE_NOT_OPEN": ("errors.role.not_open", 409),
    "ROLE_HAS_NO_ASSESSMENT": ("errors.role.has_no_assessment", 409),
    "ROLE_HAS_INVITES": ("errors.role.has_invites", 409),
    "DUPLICATE_INVITE": ("errors.invite.duplicate", 409),
    "INVITE_EXPIRED": ("errors.invite.expired", 410),
    "INVITE_REVOKED": ("errors.invite.revoked", 410),
    "LINK_BOUND_TO_OTHER_SESSION": ("errors.exam.link_bound_to_other_session", 403),
    "DEVICE_NOT_SUPPORTED": ("errors.exam.device_not_supported", 422),
    "CONSENT_REQUIRED": ("errors.exam.consent_required", 409),
    "CAMERA_REQUIRED": ("errors.exam.camera_required", 409),
    "HELP_REQUEST_PENDING": ("errors.exam.help_request_pending", 409),
    "EXAM_NOT_STARTED": ("errors.exam.not_started", 409),
    "EXAM_ALREADY_SUBMITTED": ("errors.exam.already_submitted", 409),
    "EXAM_TIME_OVER": ("errors.exam.time_over", 409),
    "DECISION_ALREADY_MADE": ("errors.review.decision_already_made", 409),
    "AI_NOT_CONFIGURED": ("errors.ai.not_configured", 409),
    "AI_LIMIT_REACHED": ("errors.ai.limit_reached", 429),
    "AI_PROVIDER_ERROR": ("errors.ai.provider_error", 502),
    "AI_OUTPUT_INVALID": ("errors.ai.output_invalid", 502),
    "REPORT_FILTER_INVALID": ("errors.report.filter_invalid", 422),
    "BRANDING_CONTRAST_TOO_LOW": ("errors.branding.contrast_too_low", 422),
    "SMTP_NOT_CONFIGURED": ("errors.email.smtp_not_configured", 409),
    "UPLOAD_INVALID": ("errors.upload.invalid", 422),
}

# Returned for unhandled exceptions only. Appendix A has no 500 code (see core/README.md).
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
INTERNAL_ERROR_MESSAGE_KEY = "errors.common.internal"

# Pydantic error type -> field code in VALIDATION_ERROR details.fields. Unlisted types pass through.
VALIDATION_FIELD_CODES: dict[str, str] = {
    "missing": "required",
    "string_too_short": "too_short",
    "string_too_long": "too_long",
    "too_short": "too_short",
    "too_long": "too_long",
    "greater_than": "too_small",
    "greater_than_equal": "too_small",
    "less_than": "too_large",
    "less_than_equal": "too_large",
    "string_pattern_mismatch": "invalid_format",
    "value_error": "invalid",
    "enum": "invalid_choice",
    "literal_error": "invalid_choice",
    "int_parsing": "invalid_type",
    "int_type": "invalid_type",
    "float_parsing": "invalid_type",
    "bool_parsing": "invalid_type",
    "string_type": "invalid_type",
    "uuid_parsing": "invalid_type",
    "datetime_parsing": "invalid_type",
    "date_parsing": "invalid_type",
    "json_invalid": "invalid_json",
    "extra_forbidden": "unknown_field",
}
