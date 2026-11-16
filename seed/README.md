# Demo data

Commands (inside the api container, with `JWT_*`, `FIELD_ENCRYPTION_KEYS` and `TOKEN_PEPPER` set, as for any run):

    docker compose -f docker/docker-compose.yml run --rm api python -m app.cli seed-demo    # create (no-op if present)
    docker compose -f docker/docker-compose.yml run --rm api python -m app.cli reset-demo   # wipe demo data, then reseed

The seed drives the service layer (create org, team, assessments, roles, invites, the exam flow, review and
decisions), so audit entries, notifications and scores are real. Refused when `APP_ENV=prod`.

## What reset-demo deletes

Only organizations flagged `is_demo = true` with everything inside them, and the demo platform admin.
Real data is never touched. The audit log is append-only (database trigger), so audit rows written for
earlier demo runs stay behind. The break-glass default super admin (`DEFAULT_SA_*`) is created when those
variables are set and is never removed.

## Demo accounts

All accounts share the demo-only password `DemoPass#2026` (no forced change). Emails are on the reserved
domain `demo.example.com`.

| Account | Email | Role |
|---|---|---|
| Daily super admin | superadmin.daily@platform.demo.example.com | super_admin |
| Org 1 admin | admin@one.demo.example.com | org_admin |
| Org 1 HR | hana@, ravi@, meera@, tom@ one.demo.example.com | hr_user |
| Org 2 admin | admin@acme.demo.example.com | org_admin |
| Org 2 HR | lena@acme.demo.example.com | hr_user |

Org 1 is named "<vendor name> (demo)" (taken from `APP_VENDOR_NAME`), has branding, a consent and a privacy
document, and every feature flag on. "Acme Demo Corp" (org 2) has camera proctoring, CSV bulk invite, reports
chat, AI question builder, AI scoring and the question bank switched off.

## Content

- 4 assessments (Java developer with 2 published versions, QA engineer, frontend developer, general aptitude
  in org 2) covering all five question types.
- 6 roles (open, closed, draft), each linked to one assessment.
- 20 invites: 13 submitted (scores from 20% to 100%, some with proctoring flags and generated placeholder
  snapshots), 1 in progress (with extra time), 1 ready with an open help request, 1 camera-exempt, 1 with
  accommodations, 1 with an approved help request, plus invited, expired and revoked ones.
- 9 decisions (approved, rejected, one admin override) and 5 attempts waiting for a decision.
- Dashboard layouts saved for every staff account. Activity is back-dated over the last two weeks.

Candidate exam links are not listed: only a hash is stored. Use the invite pages as a demo HR user to
regenerate a link for the invited candidates.
