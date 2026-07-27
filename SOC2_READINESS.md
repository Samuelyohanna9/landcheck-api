LandCheck API SOC 2 Readiness Notes

Scope

- This document covers the security-readiness controls implemented in the LandCheck API before any formal SOC 2 audit.
- It is a readiness checklist, not an audit opinion or certification.

Implemented controls

- Signed bearer sessions for Green, Work, sponsor, and environment-admin authentication.
- Central session storage with expiry, idle timeout, revocation, and actor metadata.
- Super-admin enforcement for `/green/admin/*` routes, except explicitly allowed public callbacks.
- Request activity logging enriched with validated actor/session context.
- Password-reset and password-change timestamps on Green users and sponsor accounts.
- Self-service MFA foundation for partner and sponsor accounts:
  - status
  - setup
  - verify
  - enable
  - disable
- Flutterwave webhook signature enforcement controls for production deployments.
- Production guardrail to disable destructive activity-log reset unless explicitly enabled.
- Security maintenance endpoints for posture reporting and retention cleanup.
- Compliance pack for audit operations, including:
  - policy templates
  - risk and vendor registers
  - access review and evidence logs
  - backup/restore runbooks
  - ISMS scope and SoA drafts
  - monthly review working files

Environment variables

- `LANDCHECK_ENV`
- `LANDCHECK_AUTH_SESSION_TTL_HOURS`
- `LANDCHECK_AUTH_SESSION_IDLE_MINUTES`
- `LANDCHECK_ACTIVITY_LOG_RETENTION_DAYS`
- `LANDCHECK_SECURITY_SESSION_RETENTION_DAYS`
- `LANDCHECK_ALLOW_LOG_RESET`
- `LANDCHECK_REQUIRE_FLW_WEBHOOK_SIGNATURE`
- `LANDCHECK_ENV_ADMIN_MFA_ENABLED`
- `WORK_USERNAME`
- `WORK_PASSWORD`
- `FLW_SECRET_HASH`

Public repository hygiene

- Keep real environment values only in private `.env` files on the server, in CI secrets, or in a secret manager.
- Do not commit populated evidence registers, access reviews, incident notes, backup artifacts, customer exports, or database dumps to a public repository.
- Keep tracked example files limited to placeholders and blank templates.

Operational rollout checklist

1. Set `LANDCHECK_ENV=production` in live environments.
2. Set strong `WORK_USERNAME` and `WORK_PASSWORD` values. Do not leave defaults.
3. Set `FLW_SECRET_HASH` and keep `LANDCHECK_REQUIRE_FLW_WEBHOOK_SIGNATURE=true` in production.
4. Keep `LANDCHECK_ALLOW_LOG_RESET=false` in production.
5. Review `LANDCHECK_AUTH_SESSION_TTL_HOURS` and `LANDCHECK_AUTH_SESSION_IDLE_MINUTES` against policy.
6. Enable `LANDCHECK_ENV_ADMIN_MFA_ENABLED=true` only when the environment-admin login flow is paired with an MFA verification rollout plan.
7. Run `POST /green/admin/security/maintenance` on an operational schedule or from a secure cron job.
8. Review `GET /green/admin/security/posture` regularly.
9. Expect users with legacy local-only sessions to re-authenticate after deployment.

Remaining readiness work outside this patch

- Secret rotation procedure and evidence collection.
- Backup and restore validation records.
- Vulnerability management workflow and patch cadence.
- Formal access review cadence for partner and sponsor administrators.
- Incident response runbook and tabletop evidence.
- Centralized monitoring/alerting around authentication anomalies, payout operations, and webhook failures.

Repository assets for the operational layer are now in:

- [compliance/README.md](./compliance/README.md)
- [compliance/policies](./compliance/policies)
- [compliance/registers](./compliance/registers)
- [compliance/isms](./compliance/isms)
- [compliance/runbooks](./compliance/runbooks)
- [scripts/compliance](./scripts/compliance)
