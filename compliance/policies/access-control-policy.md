# Access Control Policy

## Purpose

This policy defines how LandCheck provisions, approves, reviews, and revokes access to systems, data, and administrative functions.

## Scope

This policy applies to:

- LandCheck API
- LandCheck Work
- LandCheck Green
- Sponsor operations
- CSR workflows
- Databases
- Object storage
- Deployment and infrastructure consoles

## Policy Statements

1. Access must be granted on least-privilege principles.
2. Privileged roles must be limited to approved personnel with documented business need.
3. Shared accounts are prohibited except for explicitly approved emergency-use administrative credentials.
4. All privileged users must use MFA where technically available.
5. Access must be reviewed at least monthly for privileged users and quarterly for standard operational users.
6. User access must be removed or reduced immediately when employment, contract, or role status changes.
7. Administrative actions must be traceable through application logs, infrastructure logs, or approval records.

## Role Categories

- Super Admin
- Partner Organization Admin
- CSR Client User
- Sponsor Operations Reviewer
- Payout Reviewer
- Field Supervisor
- Field Agent
- Read-only Reporting User

## Provisioning Requirements

- Access request must identify:
  - user name
  - role requested
  - business justification
  - approving authority
  - effective date
- Privileged access must be approved by leadership or a designated system owner.

## Review Requirements

- Monthly review:
  - Super Admin
  - payout-related users
  - sponsor operations reviewers
  - organization administrators
- Quarterly review:
  - standard platform users

## Revocation Requirements

- Departed personnel: same day
- Contractor end date: same day
- Role change: before new duties begin where possible, otherwise within one business day

## Records

Use:

- `../registers/access-review-register.csv`
- `../registers/joiner-mover-leaver-register.csv`

