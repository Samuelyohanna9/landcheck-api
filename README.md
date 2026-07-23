LandCheck API

Backend service for LandCheck.

For the full local stack setup, see [LOCAL_DEVELOPMENT.md](../LOCAL_DEVELOPMENT.md).

## Local

Use `.env.example` as the local starting point.

Local API target:

- `http://localhost:8000`
- Swagger docs: `http://localhost:8000/docs`

For the local scripts in `../scripts/local-dev`, the API connects to Postgres on `localhost`.

## Production Docker stack

Use `docker-compose.yml` together with a real `.env` derived from `.env.production.example`.

Important production notes:

- `DATABASE_URL` must point to `@db:5432` because the production compose file runs Postgres as the `db` service.
- The production Postgres volume is `pgdata`, which becomes `landcheck-api_pgdata` under the default Compose project name.
- Keep only one production compose file in the server directory. If both `docker-compose.yml` and `docker-compose.yaml` exist, Docker Compose will warn and may use the wrong one.
- Do not run `docker compose down -v` on production unless you intentionally want to remove the database volume.

Recommended production commands:

```bash
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs -f db api
```

If you deploy on an existing server, make sure the Postgres image major version matches the existing database volume. The current production stack expects PostgreSQL 15 / PostGIS 3.4:

- `postgis/postgis:15-3.4`
