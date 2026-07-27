# Incident Response Policy

## Purpose

This policy defines how LandCheck identifies, classifies, responds to, and learns from security and service incidents.

## Incident Examples

- unauthorized access attempt
- account takeover
- payout fraud or suspected manipulation
- sponsor payment verification failure affecting records
- storage exposure
- data corruption
- service outage affecting field operations
- webhook failure causing financial state mismatch

## Policy Statements

1. All suspected incidents must be logged.
2. Incidents must be triaged by severity.
3. Containment must occur as quickly as practical.
4. Evidence must be preserved for root-cause analysis.
5. Customer or partner communication must be coordinated and recorded.
6. A post-incident review must be completed for material incidents.

## Severity Levels

- `Sev 1`: major outage, security breach, payment fraud, or sensitive data exposure
- `Sev 2`: material functionality outage or high-risk attempted compromise
- `Sev 3`: limited-scope issue with workaround
- `Sev 4`: low-risk issue or false positive

## Records

Use:

- `../registers/incident-log.csv`
- `../runbooks/incident-response-runbook.md`

