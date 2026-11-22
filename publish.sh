#!/bin/sh
set -e
cd "$(dirname "$0")"

# 1. Build the image so the new code/.env layer is baked in.
docker compose -f compose.prod.yaml build

# 2. Bring up the db + app. On first boot the app runs migrations and starts
#    the scheduler; on a redeploy the app restarts with the fresh image.
docker compose -f compose.prod.yaml up -d --build

# 3. Wait for the db to be healthy (compose depends_on only affects startup
#    ordering, not the readiness of a redeploy).
until docker compose -f compose.prod.yaml exec -T db pg_isready -U techx -d techx >/dev/null 2>&1; do
  sleep 1
done

# 4. Apply any pending migrations (idempotent with the CMD's alembic call).
docker compose -f compose.prod.yaml exec -u 0 app alembic upgrade head

# 5. Seed the account + persona row on a fresh database.
docker compose -f compose.prod.yaml exec -u 0 app python -m scripts.bootstrap --handle ssh_bugs

# 6. Ensure the app is back up with the new image.
docker compose -f compose.prod.yaml up -d --force-recreate app

echo "deploy done"
