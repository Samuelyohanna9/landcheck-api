# Legal & compliance drafts

**These four documents are first drafts, written from the actual system architecture so a lawyer
has a real starting point rather than a blank page. None of them has been reviewed by counsel.
Do not present them to a customer, regulator or auditor as final until a Nigerian lawyer has
reviewed and signed off on them.**

| File | What it is |
|---|---|
| [`DATA_PROCESSING_AGREEMENT.md`](DATA_PROCESSING_AGREEMENT.md) | The contract each Estate company (Controller) and LandCheck (Processor) enter into. This is the document companies see and accept in-app - see "The in-app acceptance feature" below. |
| [`BREACH_RESPONSE_PLAN.md`](BREACH_RESPONSE_PLAN.md) | LandCheck's own internal procedure for handling a personal data breach - not shown to customers. |
| [`RECORDS_OF_PROCESSING_ACTIVITIES.md`](RECORDS_OF_PROCESSING_ACTIVITIES.md) | The processing-activity register the NDPA/GAID expects a controller and processor to keep. Internal record, kept up to date as features change. |
| [`DATA_MAP.md`](DATA_MAP.md) | What personal data LandCheck stores, in which system, where that system is hosted, and why. Internal reference, also useful for answering a customer's or auditor's questions. |

Every `[bracketed placeholder]` is a real gap - a fact only the business owner or the lawyer can
fill in (registered company name and RC number, registered address, a named Data Protection
Officer, the exact hosting region, notice email addresses, liability terms). Search for `[` to
find them all before this goes to counsel.

## The in-app acceptance feature

`DATA_PROCESSING_AGREEMENT.md`'s text (specifically its "What LandCheck stores and why" and
"Sub-processors" sections) is what renders on the **Legal & compliance** page each Estate company
sees at `/estates/legal` (owners only can click "I agree"). The version string shown there must
match `app/services/estates/dpa.py`'s `DPA_VERSION` constant - bump both together whenever the
document changes materially, the same way `billing_plans.py` and `EstatePricingCards.tsx` are kept
in step. Every acceptance (and who clicked it, and when) is stored in `estate_dpa_acceptances`
and is visible platform-wide in the LandCheck admin dashboard (`/green-work` -> Estate companies ->
expand a company -> "Data Processing Agreement").
