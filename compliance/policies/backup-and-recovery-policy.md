# Backup and Recovery Policy

## Purpose

This policy defines how LandCheck protects system and database recoverability.

## Policy Statements

1. Production data must be backed up on a scheduled basis.
2. Restore procedures must be tested at least quarterly.
3. Backup failures must be reviewed and resolved promptly.
4. Backup artifacts must be protected against unauthorized access.
5. Recovery procedures must be documented and repeatable.

## Minimum Baseline

- Database backup: daily
- Backup retention: according to business and legal requirements
- Restore test: quarterly minimum
- Recovery evidence: retained in the compliance register

## Records

Use:

- `../registers/backup-restore-test-log.csv`
- `../runbooks/backup-restore-runbook.md`

