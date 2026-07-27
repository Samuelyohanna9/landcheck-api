# Security Logging and Monitoring Policy

## Purpose

This policy defines the minimum monitoring and review expectations for LandCheck.

## Policy Statements

1. Security-relevant events must be logged where technically possible.
2. Log review must occur on a scheduled basis.
3. Authentication failures, privileged actions, payout anomalies, and webhook failures must receive attention.
4. Log reset or destructive cleanup must be restricted in production.
5. Monitoring output must be retained as evidence.

## Minimum Review Scope

- admin logins
- failed login patterns
- role changes
- password resets
- payout approvals and overrides
- sponsor reconciliation anomalies
- webhook signature failures
- 5xx spikes on API routes

## Records

Use:

- `../registers/audit-log-review-log.csv`
- `../registers/monthly-evidence-log.csv`

