# Records of Processing Activities (ROPA)

**DRAFT - prepared 27 September 2026 for review by a Nigerian lawyer. Not yet reviewed by
counsel. Internal document, kept up to date as features change - the NDPA/GAID expects a
controller and processor to maintain a record like this.**

This records what LandCheck Estates processes, as both a Processor (of each Estate company's
customer/agent data) and, for one row, as an independent Controller (of Estate companies' own
staff account data). See `DATA_PROCESSING_AGREEMENT.md` clause 2 for that distinction, and
`DATA_MAP.md` for which system each category of data lives in.

**Processor entity**: **[LandCheck's registered company name - fill in]**
**Controller**: each Estate company that holds a LandCheck Estates account (for rows 1-7 below);
LandCheck itself (for row 8)
**Data Protection Officer / contact**: **[name/contact - fill in]**

| # | Processing activity | Purpose | Categories of data subjects | Categories of personal data | Legal basis (NDPA) | Recipients / Sub-processors | Retention | Cross-border transfer | Security measures |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Customer & reservation management | Record who has reserved, is buying, or owns a plot, and manage that relationship | Land buyers / prospective customers | Full name, phone, email, address, company name (if any), free-text notes the Company's staff add | Performance of a contract between the Company and the customer; the Company's legitimate interest in managing its sales pipeline | Hosted on LandCheck's database; **[hosting provider]** | Until the Company deletes the record, or account closure + retention period in the DPA | Yes - database hosting (see `DATA_MAP.md`) | Access-controlled by the Company's own staff roles; audit-logged |
| 2 | Sales-agent commissions | Track which agent sold a plot and calculate/pay commission | The Company's sales agents | Name, contact details, sales/commission records | Performance of a contract (agent agreement); legitimate interest | Hosted on LandCheck's database | Until the Company deletes the record | Yes | Same as row 1 |
| 3 | Payments & billing (subscription) | Charge the Company for its LandCheck Estates subscription | The Company's billing contact | Name, email, phone, tokenised card reference, last 4 digits, card brand (never a full card number) | Performance of a contract (the subscription) | Flutterwave Technology Solutions Limited (payment processor) | For the subscription's life plus the retention period needed for tax/audit purposes - **[confirm exact period with counsel]** | Yes - Flutterwave | Card data never transits or is stored on LandCheck's servers; tokenised reference only |
| 4 | Public Estate marketing page | Let a Company publish a public, shareable page showing its plots, availability and prices | The general public who view the page; the customers who submit a reservation request through it | Name, phone and/or email submitted with a reservation request; browsing does not require any personal data | Legitimate interest (the Company's marketing); consent/contract once a reservation request is submitted | Hosted on LandCheck's database and, for map imagery, Mapbox (no personal data sent to Mapbox) | Until the Company deletes the request | Yes (database only; Mapbox receives no personal data) | Same as row 1 |
| 5 | Automatic social-media posting | Publish flyers/ads the Company approves to its own Facebook Page and Instagram account, on a schedule the Company sets | The public who see the post; not typically an identifiable data subject | The Company's own connected-account access tokens (stored encrypted); post images and captions the Company approves, generated from its own plot/price data | Legitimate interest; performed on the Company's explicit instruction (connecting the account, approving/scheduling the post) | Meta Platforms, Inc. | Access tokens: until the Company disconnects the account. Post records: until the Company deletes them | Yes - Meta Platforms | Tokens encrypted at rest; least-privilege permission scopes requested from Meta |
| 6 | WhatsApp updates to buyers | Send opted-in buyers a WhatsApp message about new plots, price changes, or an inspection invitation | Buyers who explicitly opted in on the public Estate page | Name (optional), phone number, opt-in consent record and timestamp, message delivery status | Consent (the buyer ticks an opt-in box and can reply STOP at any time) | Meta Platforms, Inc. (WhatsApp Business Cloud API) | Until the buyer opts out (STOP) or the Company removes the contact | Yes - Meta Platforms | Consent text and timestamp recorded; STOP honoured immediately across every Company that had messaged that number |
| 7 | Survey, staking, and land documents | Produce survey plans, staking instructions and store supporting documents (title documents, layout drawings) for an estate | Land buyers (named on documents); the Company's field/survey staff | Names appearing on uploaded documents; document files themselves; geolocation/coordinate data (not personal data on its own) | Performance of a contract; the Company's legal/regulatory recordkeeping needs | Cloudflare, Inc. (R2 object storage) | Until the Company deletes the document, or as required for the Company's own recordkeeping obligations | Yes - Cloudflare | Signed, time-limited links for third-party fetches; access-controlled by staff role |
| 8 | Company staff accounts (LandCheck as Controller) | Let the Company's staff log in and use the Platform; keep an audit trail of what they did | The Company's own staff who are given a LandCheck Estates login | Name, email, hashed password, login/session activity, actions taken (audit trail) | Performance of a contract (the Company's subscription agreement, which covers its staff's use); legitimate interest (security, audit) | Hosted on LandCheck's database | For as long as the account exists, plus **[retention period - fill in]** for the audit trail | Yes | Passwords hashed, never stored in plain text; session expiry; audit trail visible to the Company's own staff with audit-read permission |

## Notes for whoever maintains this record

- Add a new row whenever a feature starts processing a new category of personal data or sends it
  to a new third party - do this at the time the feature ships, not retroactively.
- If a row's legal basis is "consent" (rows 6), the actual consent text shown to the Data Subject
  is the authoritative record of what was agreed - keep it consistent with what is described here.
- This register, `DATA_MAP.md`, and the Sub-processor list in `DATA_PROCESSING_AGREEMENT.md`
  clause 7 should never disagree with each other. If one changes, check the other two.
