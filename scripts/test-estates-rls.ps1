param(
  [Parameter(Mandatory = $true)]
  [string]$DatabaseUrl
)

if ($DatabaseUrl -notmatch '^postgresql' -or $DatabaseUrl -notmatch 'test') {
  throw 'DatabaseUrl must be a dedicated PostgreSQL test database containing "test".'
}

$env:DATABASE_URL = $DatabaseUrl
$env:ESTATE_RLS_TEST_DATABASE_URL = $DatabaseUrl
py -3 -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
py -3 -m pytest -q -m estate_rls
