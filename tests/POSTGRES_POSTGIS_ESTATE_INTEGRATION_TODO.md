# PostgreSQL/PostGIS Estate Integration Verification

Run this suite only against a disposable PostgreSQL database with PostGIS enabled after applying
Alembic revisions `20260913_0001` through `20260913_0003`.

- Verify PostGIS estate, block and plot geometry tables and spatial indexes.
- Verify the active allocation partial unique index under concurrent reservations/allocations.
- Verify payment creation, confirmation, voiding and allocation/customer relationship integrity.
- Verify Organization B cannot list, read, confirm or void Organization A payments.
- Verify Organization B cannot list, upload, download or link Organization A documents.
- Verify private R2 receipt/document proxy access and audit events against actual storage.
- Verify migration rollback in a disposable environment.
