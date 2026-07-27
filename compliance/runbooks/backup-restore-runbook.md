# Backup and Restore Runbook

## Backup

- run scheduled database backup
- store artifact securely
- record checksum and backup location

## Restore Test

1. choose non-production target
2. restore latest backup
3. validate schema and critical tables
4. record recovery time and issues
5. log outcome in backup-restore register

## Scripts

- `../../scripts/compliance/backup-postgres.sh`
- `../../scripts/compliance/restore-postgres.sh`

