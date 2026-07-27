# LandCheck Compliance Pack

 toward audit readiness for:

- SOC 2 Type I readiness
- SOC 2 Type II evidence collection
- ISO/IEC 27001 ISMS readiness

This pack is designed for the LandCheck platform, including:

- LandCheck API
- LandCheck Green
- LandCheck Work
- Public sponsor operations
- CSR reporting operations
- Field-agent workflows

## What This Pack Covers

- Policies
- Registers
- ISMS templates
- Operational runbooks
- Security and backup scripts

## Directory Map

- `policies/`
  - formal security and operating policies
- `registers/`
  - template tracking sheets for risks, access, vendors, changes, incidents, and evidence
- `isms/`
  - ISO 27001 scope, SoA, risk method, internal audit, and management review templates
- `runbooks/`
  - practical procedures for incident handling, offboarding, backup/restore, monthly review, and deployment approval

## Expected Operating Cadence

- Daily
  - confirm backups completed
  - review critical failures
- Weekly
  - review admin access changes
  - review payout/payment anomalies
  - review deployment history
- Monthly
  - update risk register
  - complete access review
  - complete audit-log review
  - update vulnerability register
  - collect monthly evidence
- Quarterly
  - vendor review
  - management review
  - restore exercise
  - internal control walkthrough

## High-Priority First Actions

1. Complete `registers/asset-inventory.csv`
2. Complete `registers/vendor-register.csv`
3. Complete `registers/risk-register.csv`
4. Adopt the policies in `policies/`
5. Start monthly evidence logging in `registers/monthly-evidence-log.csv`
6. Run a restore test and record it in `registers/backup-restore-test-log.csv`
7. Run a privileged-user access review and record it in `registers/access-review-register.csv`