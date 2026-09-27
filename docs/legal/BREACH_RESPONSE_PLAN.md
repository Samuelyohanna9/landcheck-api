# Personal Data Breach Response Plan

**DRAFT - prepared 27 September 2026 for review by a Nigerian lawyer. Not yet reviewed by
counsel. Internal document - not shown to customers.**

This is LandCheck's own procedure for responding to a Personal Data Breach affecting the LandCheck
Estates platform (or any other LandCheck product handling personal data). It exists so that when
an incident happens, nobody is deciding the process for the first time under pressure.

## 1. What counts as a Personal Data Breach

A breach of security leading to the accidental or unlawful destruction, loss, alteration,
unauthorised disclosure of, or unauthorised access to, personal data. In this system, examples
include (this list is illustrative, not exhaustive):

- a database credential, API key, or the `SECRET_KEY-`/`SOCIAL_SECRET_KEY`-family encryption key
  is exposed or suspected compromised;
- a Cloudflare R2 bucket or a signed document/image link is found to be readable by someone who
  should not have access;
- a Facebook, Instagram or WhatsApp access token is exposed, or a connected account is used to
  post or message without the Company's authorisation;
- a bug exposes one Company's customers, plots or payment records to another Company;
- a staff account (LandCheck's own, or a Company's) is compromised and used to export or alter
  data it should not have accessed;
- a lost or stolen device that had access to production systems or customer data;
- a vendor (Sub-processor) notifies LandCheck of an incident affecting LandCheck's data with them.

A bug that causes an error but exposes no personal data (for example, a page failing to load) is
**not** a Personal Data Breach for this plan, even though it should still be fixed.

## 2. Roles

| Role | Who | Responsibility |
|---|---|---|
| Incident Lead | **[name/role - fill in, e.g. the founder/CTO]** | Owns the response end to end; decides severity and whether to notify the NDPC |
| Technical Responder | **[name/role - fill in]** | Contains the issue, investigates scope, fixes the root cause |
| Communications | **[name/role - fill in]** | Drafts and sends notifications to affected Companies, Data Subjects, and the NDPC |
| Data Protection Officer | **[name/contact - fill in, if appointed]** | Advises on legal notification obligations; may be the same person as Incident Lead at LandCheck's current size |

If LandCheck is a small team, one person may hold more than one role - what matters is that every
role above is covered by someone during an incident, not that four different people exist.

## 3. How a breach is likely to be found

- An error-monitoring alert or an unusual pattern in server logs.
- A report from a Company, a buyer, or a member of the public.
- A notification from a Sub-processor (Cloudflare, Flutterwave, Meta, Google, Mapbox, the email
  provider, or the database/hosting provider).
- Routine review of access logs or the audit trail.
- Automated dependency/vulnerability alerts (e.g. GitHub security advisories) indicating a library
  LandCheck depends on has an actively exploited flaw.

Anyone at LandCheck who suspects a breach should notify the Incident Lead immediately, without
waiting to confirm it first - a false alarm costs little; a delayed real one costs a great deal.

## 4. Severity classification

| Severity | Examples | Target response |
|---|---|---|
| **Critical** | Payment-token compromise; exposure of many Companies' data at once; ransomware/destructive access to the production database | Immediate, all-hands; regulator notification track begins immediately |
| **High** | One Company's customer list exposed; a leaked Facebook/Instagram/WhatsApp token used to post without authorisation; a misconfigured storage bucket exposing documents | Contain within hours; assess regulator/Company notification same day |
| **Medium** | A single account compromised with limited data exposure; a bug briefly exposing non-sensitive fields (e.g. a plot's price) across accounts | Contain within 24 hours; assess notification need |
| **Low** | Near-miss caught before any data was actually accessed; an internal control gap found during testing, with no evidence of exploitation | Fix and log; no notification expected, but recorded for the register in section 8 |

## 5. Response procedure

### Step 1 - Identify and contain (target: as soon as detected)
Confirm what is actually happening. Revoke or rotate the specific credential/token involved;
disable the affected account, feature flag, or endpoint if needed to stop ongoing exposure. Do not
destroy evidence (logs, the exposed record itself) needed for the investigation in Step 2.

### Step 2 - Assess (target: within 24 hours of detection)
Determine: what personal data was involved; which Company/Companies and how many Data Subjects;
whether the data was actually accessed/exfiltrated or only exposed; the likely cause (see Step 5);
and the severity (section 4). Document this in the incident log (section 8) as you go, not after
the fact.

### Step 3 - Notify (target: within 72 hours of LandCheck becoming aware, per NDPA)
- **NDPC**: the Incident Lead decides, with legal advice where available, whether the incident
  meets the NDPA's threshold for notifying the Nigeria Data Protection Commission. Nigeria Data
  Protection Act 2023 and the GAID set the applicable notification obligations and timelines -
  **confirm the current exact threshold and deadline with counsel before relying on the "72 hours"
  figure used here.**
- **Affected Estate companies (Controllers)**: notify without undue delay under clause 9 of the
  Data Processing Agreement - see `DATA_PROCESSING_AGREEMENT.md`. LandCheck is the Processor for
  Company Personal Data, so the Company decides whether and how to notify its own customers, but
  LandCheck gives it the facts needed to do so promptly.
- **Data Subjects directly**: only where LandCheck itself is the Controller (its own staff/billing
  data - clause 2.2 of the DPA) or where a Company asks LandCheck to send the notice on its behalf.

### Step 4 - Remediate (target: as soon as safely possible)
Fix the root cause (not just the symptom); rotate every credential that could plausibly have been
exposed, even if only one was confirmed; verify the fix in a way that would have caught the
original issue.

### Step 5 - Post-incident review (target: within 2 weeks of resolution)
Write a short internal report: what happened, timeline, root cause, what was notified to whom and
when, and at least one concrete change (a new check, a monitoring alert, a process change) to
reduce the chance of the same class of incident recurring. Update this plan if the incident
revealed a gap in it.

## 6. Notification content

A notification to the NDPC or an affected party should cover, to the extent known at the time:
what happened; when it happened and when it was discovered; what categories of personal data and
approximately how many people are affected; the likely consequences for those affected; what
LandCheck has done or will do about it; and a contact point for follow-up questions. It is
acceptable, and expected, to notify with incomplete information within the deadline and follow up
with details as the investigation continues - waiting for a complete picture before notifying at
all is not acceptable.

## 7. Contacts

| Purpose | Contact |
|---|---|
| Internal incident reporting | **[internal contact - fill in]** |
| NDPC notification | **[NDPC's current notification channel - confirm with counsel]** |
| External legal counsel | **[fill in once engaged]** |
| Key Sub-processors' security contacts | Cloudflare: **[fill in]** · Flutterwave: **[fill in]** · Meta: **[fill in]** · **[database/hosting provider]**: **[fill in]** |

## 8. Incident log

Keep a running record of every suspected or confirmed incident, however small, in a single place
(a private document or ticket, not this file). At minimum record: date detected, what happened,
severity, who was notified and when, and the post-incident action taken. This register is itself
something the NDPC or a Company's auditor may reasonably ask to see.

## 9. Testing this plan

Review this plan, and the contact details in section 7, at least once a year, and after any real
incident. Where practical, run a short tabletop exercise (a fictional scenario walked through by
the people in section 2) so the first real incident is not the first time the plan is used.
