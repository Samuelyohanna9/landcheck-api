LandCheck API

Backend service for land verification and hazard analysis platform.

R2 Photo Storage (Green)

Set these environment variables in your API runtime:

- `R2_ENDPOINT_URL`
- `R2_PUBLIC_BASE_URL`
- `R2_BUCKET`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_REGION` (optional, default: `auto`)

Example with your bucket URL:

- `R2_PUBLIC_BASE_URL=https://751ea1abdb3fb6ff7f276b3753e4c6a1.r2.cloudflarestorage.com/photosgreen`
- `R2_ENDPOINT_URL=https://751ea1abdb3fb6ff7f276b3753e4c6a1.r2.cloudflarestorage.com`
- `R2_BUCKET=photosgreen`

R2 PDF Export Storage (Survey/Green/Work/Hazard)

PDF exports now upload to R2 on every export request (best effort) and still return normal download responses.

Optional environment variables:

- `R2_EXPORTS_ENABLED` (default: `true`)
- `R2_EXPORTS_PREFIX` (default: `exports/pdf`)
- `R2_EXPORTS_BUCKET` (optional, falls back to `R2_BUCKET`)
- `R2_EXPORTS_PUBLIC_BASE_URL` (optional, falls back to `R2_PUBLIC_BASE_URL`)
- `R2_EXPORTS_ENDPOINT_URL` (optional, falls back to `R2_ENDPOINT_URL`)
- `R2_EXPORTS_ACCESS_KEY_ID` (optional, falls back to `R2_ACCESS_KEY_ID`)
- `R2_EXPORTS_SECRET_ACCESS_KEY` (optional, falls back to `R2_SECRET_ACCESS_KEY`)
- `R2_EXPORTS_REGION` (optional, falls back to `R2_REGION`, default `auto`)

If you want a separate bucket for PDFs, set `R2_EXPORTS_BUCKET` and related `R2_EXPORTS_*` values.
