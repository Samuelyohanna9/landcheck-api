# Password and MFA Policy

## Purpose

This policy defines the authentication baseline for LandCheck administrative and operational users.

## Policy Statements

1. Passwords must be unique to LandCheck and must not be reused from personal or unrelated business services.
2. Default passwords must never remain active in production.
3. Privileged users must use MFA where supported by the platform.
4. Password reset events must be logged or otherwise traceable.
5. Emergency-use environment-admin credentials must be tightly controlled and rotated when exposed or changed.

## Minimum Password Expectations

- minimum length: 12 characters
- must not be common or easily guessed
- should contain a mix of character types
- must not contain project name, company name, or user name as the full password

## MFA Requirements

The following roles must use MFA once rollout is operationally complete:

- Super Admin
- Partner Organization Admin
- Payout Reviewer
- Sponsor Operations Reviewer
- Any user with access to security posture or audit-log administration

## Credential Storage and Handling

- Passwords must be stored only as salted secure hashes
- Secrets must not be hardcoded in frontend code
- Production credentials must be stored in secure server-side environment configuration

## Exceptions

Any temporary exception must:

- be documented
- include owner and expiry date
- be approved by management

