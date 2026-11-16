"""M8 Exam engine (spec 6/M8, 8). Cross-module interfaces:

- ``service.ExamService.finalize`` (idempotent submit; M17 timeout sweeper calls it)
- ``service.attempt_remaining_seconds(attempt, now)``
- ``severity_rules.severity_for``
- ``hooks.on_submit`` registry (M9 registers scoring)
"""
