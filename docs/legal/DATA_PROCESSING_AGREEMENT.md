# Data Processing Agreement

**DRAFT - prepared 27 September 2026 for review by a Nigerian lawyer. Not yet reviewed by counsel.
Version shown to customers: `2026-09-27`. See `README.md` in this folder.**

This Data Processing Agreement ("**DPA**") is entered into between:

1. **[LandCheck's registered company name, e.g. "LandCheck Technologies Limited"]**, a company
   registered in Nigeria under RC number **[RC number]**, with its registered address at
   **[registered address]** ("**LandCheck**", "**Processor**", "**we**"); and
2. The company that created a LandCheck Estates account and clicked "I agree" below ("**the
   Company**", "**Controller**", "**you**"),

each a "**Party**" and together the "**Parties**".

This DPA forms part of, and is incorporated into, LandCheck's Terms of Service. Where this DPA
and the Terms of Service conflict on data protection matters, this DPA controls.

## 1. Definitions

- **"NDPA"** means the Nigeria Data Protection Act 2023.
- **"GAID"** means the NDPC's General Application and Implementation Directive 2025 (effective 19
  September 2025).
- **"NDPC"** means the Nigeria Data Protection Commission, the regulator for both instruments.
- **"Personal Data"**, **"Processing"**, **"Data Subject"**, **"Controller"** and **"Processor"**
  have the meanings given in the NDPA.
- **"Personal Data Breach"** means a breach of security leading to the accidental or unlawful
  destruction, loss, alteration, unauthorised disclosure of, or access to, Personal Data processed
  under this DPA.
- **"Platform"** means the LandCheck Estates software (web dashboard, public Estate pages, API and
  related services) that the Company uses under its LandCheck Estates subscription.
- **"Sub-processor"** means a third party LandCheck engages to process Personal Data on the
  Company's behalf in order to provide the Platform (see clause 7).

## 2. Roles of the parties

2.1 In relation to Personal Data the Company collects about its own customers, prospective buyers,
sales agents, and staff, and uploads to or generates on the Platform ("**Company Personal Data**"),
the Company is the Controller and LandCheck is the Processor. The Company alone decides why it
collects this data and what it is used for; LandCheck processes it only to provide the Platform
and only on the Company's documented instructions (which include the instructions built into the
Platform's ordinary functionality, such as generating a marketing flyer from the Company's own
plot and price data).

2.2 In relation to the Company's own account data (the names, emails and login activity of the
Company's staff who use the Platform, and the Company's billing details), LandCheck is an
independent Controller, since LandCheck decides why that data is kept - see LandCheck's Privacy
Policy.

## 3. Subject matter, nature and purpose of processing

3.1 **Subject matter**: LandCheck's provision of the Platform to the Company.

3.2 **Duration**: for as long as the Company's LandCheck Estates subscription is active, and for
the retention period in clause 10 after it ends.

3.3 **Nature of processing**: storage, organisation, retrieval, use, transmission and erasure of
Company Personal Data as needed to run the features the Company turns on.

3.4 **Purpose**: to let the Company manage its estate(s) - plots, blocks, customers, reservations,
allocations, payments, sales-agent commissions, survey and staking records, documents, and (if the
Company turns them on) a public marketing page, automatic social-media posts, and WhatsApp updates
to buyers who opted in.

3.5 **Categories of Data Subjects**: the Company's prospective and existing land buyers/customers;
the Company's sales agents and field staff; the Company's own Platform users (its staff accounts).

3.6 **Categories of Personal Data**: see `DATA_MAP.md` in this folder for the full inventory.
In summary: names, phone numbers, email addresses, postal/site addresses, and (where the Company
chooses to record them) notes about a customer or their reservation; payment references and
tokenised card identifiers (LandCheck never stores full card numbers - see clause 8.3); consent
records and phone numbers of buyers who opted in to WhatsApp updates; staff login credentials
(passwords are stored hashed, never in plain text) and activity logs. LandCheck does not require
the Company to submit any special category of personal data (health, biometric, religious,
political or similar) to use the Platform, and the Company should not submit any without first
confirming a lawful basis for doing so with its own counsel.

## 4. The Company's instructions

4.1 The Company instructs LandCheck to process Company Personal Data only: (a) to provide the
Platform's features as configured by the Company; (b) to comply with a legal obligation; and
(c) as otherwise agreed in writing between the Parties.

4.2 If LandCheck believes an instruction from the Company would breach the NDPA, GAID or another
applicable law, LandCheck will tell the Company before carrying it out.

## 5. LandCheck's obligations as Processor

LandCheck will:

(a) process Company Personal Data only on the Company's instructions (clause 4), except where
    required to do otherwise by law;

(b) ensure that everyone at LandCheck authorised to process Company Personal Data is bound by a
    confidentiality obligation;

(c) implement appropriate technical and organisational security measures (Annex A);

(d) not engage a Sub-processor without the authorisation in clause 7;

(e) taking into account the nature of the processing, reasonably assist the Company in responding
    to a Data Subject's request to exercise their rights under the NDPA (access, correction,
    erasure, restriction, objection, and portability where applicable) - LandCheck itself does not
    respond to a Data Subject on the Company's behalf unless the Parties agree otherwise;

(f) notify the Company of a Personal Data Breach in accordance with clause 9;

(g) reasonably assist the Company in meeting its own obligations under the NDPA/GAID (such as a
    data protection impact assessment) where the assistance depends on information only LandCheck
    holds;

(h) at the Company's choice, on termination of the subscription, delete or return all Company
    Personal Data and delete existing copies, subject to clause 10; and

(i) make available to the Company the information reasonably necessary to demonstrate compliance
    with this DPA, and allow for, and contribute to, audits under clause 11.

## 6. The Company's obligations as Controller

The Company will: (a) have a lawful basis (such as consent, contract, or legitimate interest under
the NDPA) for every category of Company Personal Data it submits to the Platform; (b) give any
notice, and obtain any consent, required from its Data Subjects - for example, the consent text a
buyer sees before opting in to WhatsApp updates is the Company's notice, not LandCheck's, even
though the Platform generates the tick-box for it; (c) not instruct LandCheck to process special
category data without first agreeing this with LandCheck in writing; and (d) keep its own account
credentials secure, since a compromised Company account is the most common route to a data breach.

## 7. Sub-processors

7.1 The Company authorises LandCheck to engage the Sub-processors listed below to provide the
Platform. Each is bound by a written agreement imposing data protection obligations no less
protective than this DPA.

| Sub-processor | What it does | Data it can see |
|---|---|---|
| Cloudflare, Inc. (Cloudflare R2) | Object storage for uploaded documents (survey plans, title documents, estate logos) | Files the Company or its customers upload |
| **[Postgres hosting provider - fill in]** | Hosts the primary database | All Company Personal Data |
| Flutterwave Technology Solutions Limited | Payment processing for the Company's LandCheck subscription | Billing contact details; LandCheck never receives or stores full card numbers (clause 8.3) |
| Meta Platforms, Inc. | Facebook Page and Instagram publishing, WhatsApp Business Cloud API, when the Company turns these on | Post content and images the Company chooses to publish; phone numbers of buyers who explicitly opted in to WhatsApp updates |
| Google LLC (Earth Engine) | Satellite imagery analysis for hazard screening and growth forecasts | Estate/plot boundary coordinates only - no Personal Data |
| Mapbox, Inc. | Map tiles and satellite basemaps shown in the dashboard and on public Estate pages | None (map display only) |
| **[Email/SMTP provider - fill in]** | Sends transactional emails (receipts, reservation notices, reminders) | Recipient name and email address, and the content of that email |

7.2 LandCheck will give the Company at least **[30 days - confirm with counsel]** notice before
adding or replacing a Sub-processor with access to Company Personal Data, by email to the
Company's billing contact, and the Company may object on reasonable data-protection grounds within
that period.

## 8. Security measures

8.1 LandCheck maintains the technical and organisational measures in Annex A, which include:
encryption in transit (HTTPS/TLS) for the Platform and its API; encryption at rest for third-party
access tokens (Facebook/Instagram/WhatsApp connections) using authenticated symmetric encryption;
password hashing (never plain text) for every account type; role-based access control within each
Company's own account, so a sales agent cannot see, for example, another agent's commission
records unless given that permission; and an audit trail of actions taken on a Company's estates.

8.2 The Company is responsible for the access-control choices it makes within its own account -
who it invites, and what role it gives them.

8.3 Card payment details are captured directly by Flutterwave's hosted payment page and never pass
through LandCheck's servers. LandCheck stores only a tokenised reference sufficient to charge a
renewal, plus the last four digits and card brand, for the Company's own reference.

## 9. Personal Data Breach notification

9.1 LandCheck will notify the Company without undue delay, and in any case within
**[24-48 hours - confirm with counsel]** of becoming aware, of a Personal Data Breach affecting
Company Personal Data, describing (to the extent then known): the nature of the breach; the
categories and approximate number of Data Subjects and records affected; the likely consequences;
and the measures taken or proposed to address it and mitigate its effects. See
`BREACH_RESPONSE_PLAN.md` for LandCheck's internal procedure.

9.2 The Company remains responsible for deciding whether and how to notify the NDPC and affected
Data Subjects, since the Company is the Controller; LandCheck will provide the information and
reasonable assistance the Company needs to do so.

## 10. Data retention and deletion

10.1 While the subscription is active, Company Personal Data is retained for as long as the
relevant record exists in the Company's account (for example, a customer record until the Company
deletes it).

10.2 On termination of the subscription, LandCheck will retain Company Personal Data for
**[90 days - confirm with counsel]** to allow the Company to export it or reactivate, after which
it is deleted, except where LandCheck must keep it longer to comply with a legal obligation (for
example, transaction records for tax or anti-money-laundering purposes) or to establish, exercise
or defend a legal claim.

## 11. Audits

On reasonable prior written notice and no more than once a year (unless investigating a suspected
Personal Data Breach), the Company may request evidence of LandCheck's compliance with this DPA.
LandCheck will provide relevant documentation (such as a summary of the measures in Annex A) and,
where documentation is not sufficient, reasonably cooperate with an audit conducted by the Company
or an independent auditor it appoints, at the Company's cost, subject to confidentiality and
without disrupting other customers' service.

## 12. International transfers

Some Sub-processors in clause 7 process data outside Nigeria. LandCheck relies on
**[the applicable NDPA/GAID cross-border transfer mechanism - to be confirmed by counsel, e.g.
standard contractual clauses, an adequacy finding, or Sub-processor certification]** for these
transfers, and will provide further information on request.

## 13. Liability

**[To be drafted by counsel - e.g., each Party's liability under this DPA is subject to the
limitations and exclusions in the Terms of Service, except that nothing limits liability for
matters that cannot be limited under the NDPA.]**

## 14. Term and termination

This DPA takes effect when the Company clicks "I agree" in the Platform and continues for as long
as the Company has a LandCheck Estates account, surviving termination of the subscription to the
extent needed to give effect to clauses 10 (retention/deletion) and 13 (liability).

## 15. Governing law

This DPA is governed by the laws of the Federal Republic of Nigeria.

---

## Annex A - Technical and organisational security measures

- **Encryption in transit**: all Platform traffic (dashboard, public Estate pages, API) is served
  over HTTPS/TLS.
- **Encryption at rest for sensitive tokens**: Facebook, Instagram and WhatsApp connection tokens
  are stored encrypted, not in plain text, using an authenticated encryption scheme with a key held
  only on the server.
- **Password storage**: every account type (Company staff, buyers, LandCheck staff) stores a salted
  hash of the password, never the password itself.
- **Access control**: each Company account is isolated from every other Company's data; within an
  account, staff are assigned a role (owner, manager, accounts, surveyor, field officer, sales,
  marketer, or viewer) that determines what they can see and change.
- **Audit trail**: material actions on an estate (creating or editing a plot, allocating a customer,
  recording a payment, publishing a public page, sending a marketing broadcast) are logged with who
  did it and when, visible to the Company's own staff with audit-read permission.
- **Signed, time-limited links**: images and documents fetched by third parties (for example, an
  image Facebook's servers fetch to publish a post) use a signed link that expires, rather than a
  permanently public address.
- **Least-privilege third-party access**: LandCheck requests only the Facebook/Instagram/WhatsApp
  permissions needed to publish the specific content the Company asks it to publish.
- **Backups**: **[describe actual backup frequency, encryption and retention - fill in]**.
- **Vulnerability and dependency management**: **[describe patching cadence / any scanning tooling
  in use - fill in]**.
- **Staff access**: **[describe who at LandCheck can access production data and under what
  conditions - fill in]**.
