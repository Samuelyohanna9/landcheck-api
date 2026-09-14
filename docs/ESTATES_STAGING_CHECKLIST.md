# LandCheck Estates staging checklist

Run the backend migration before the application is deployed:

```powershell
$env:DATABASE_URL = $env:LANDCHECK_STAGING_DATABASE_URL
alembic upgrade head
```

Run the non-destructive infrastructure check from the repository root:

```powershell
python scripts/verify_estates_staging.py
```

Required checks are API health, migration `20260914_0011`, PostGIS availability, private S3/MinIO
bucket access, and (when `LANDCHECK_STAGING_AUTH_TOKEN` is supplied) the authenticated Estates
access route. Complete the authenticated role, tenant-isolation, payment, document, Survey and
Green smoke cases with staging accounts before production promotion.
