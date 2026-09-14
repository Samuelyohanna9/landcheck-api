# Database migrations

Estate schema changes are managed with Alembic. Existing Survey and Green bootstrap behavior is
unchanged; do not add Estate tables to runtime `create_all` or `CREATE TABLE IF NOT EXISTS` paths.

Run from `landcheck-api` with the target environment's `DATABASE_URL` configured:

```powershell
py -3 -m alembic upgrade head
```

Generate a reviewed revision after changing migration-managed models:

```powershell
py -3 -m alembic revision -m "describe change"
```

Every revision needs an explicit, reviewed downgrade. Validate generated SQL without connecting to
a database when needed:

```powershell
py -3 -m alembic upgrade head --sql
py -3 -m alembic downgrade 20260913_0001:base --sql
```

Run tests with `LANDCHECK_TEST_DATABASE_URL` pointing to a dedicated test database. The default is
an isolated in-memory SQLite database for unit tests; PostgreSQL integration tests should use a
separate database with `test` in its name.
