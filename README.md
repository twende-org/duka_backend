# duka_backend

Django REST Framework backend for **Biashara Connect / Twende Duka** — a multi-tenant
POS, inventory and marketplace platform for East African SMEs. It serves the merchant
console, the customer storefront portal, and the B2B supply chain between wholesalers
and retailers.

This repository contains the API only. The React storefront and merchant console live
in the frontend repository and talk to this service over `/api/v1/`.

---

## Table of contents

- [Stack](#stack)
- [Architecture](#architecture)
- [Application domains](#application-domains)
- [API surface](#api-surface)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Background jobs](#background-jobs)
- [Testing](#testing)
- [Docker](#docker)
- [Production deployment](#production-deployment)
- [Operations](#operations)
- [Project layout](#project-layout)

---

## Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| Framework | Django 6.1.1 |
| API | Django REST Framework 3.18.1 |
| Auth | `djangorestframework-simplejwt` 5.5.1 (JWT access/refresh), `google-auth` 2.58.0 (Google Identity Services), `firebase-admin` 7.7.0 (legacy token bridge) |
| Database | SQLite for local development, PostgreSQL 16 in production — selected automatically by the presence of `POSTGRES_DB` |
| Async | Celery 5.6.3 with Redis 8.1.0 as broker |
| Serving | gunicorn 23.0.0 behind a reverse proxy; WhiteNoise 6.12.0 for static assets |
| Schema | `drf-spectacular` 0.30.0 (OpenAPI 3) |

Scale: 13 Django apps, 279 Python modules, 75 migrations, 36 test modules (714 tests).

## Architecture

```
React SPA (duka.twendedigital.tech)
        │  HTTPS / JSON + JWT
        ▼
   Traefik v3  ──►  gunicorn (config.wsgi)  ──►  PostgreSQL
        │                    │
        │                    ├── WhiteNoise  (/static/)
        │                    └── django.views.static  (/media/)
        │
        └──►  Celery worker + beat  ──►  Redis
```

Requests are stateless: every `/api/v1/` route requires a JWT except the explicitly
public ones (storefront catalogue, Facebook webhook, health probe). Tenancy is
enforced in the view layer — querysets are scoped to the authenticated user's shop
roles, so one merchant can never read another's rows.

The project is a Django app *plus* a thin routing layer:

- `config/` — settings, URL root, WSGI/ASGI entrypoints, Celery app.
- `apps/` — domain logic: models, business rules, management commands, Celery tasks.
- `api/v1/` — transport only: serializers and viewsets. No business logic lives here.
- `tests/` — the full suite, consolidated in one place rather than per-app.

## Application domains

| App | Responsibility | Principal models |
| --- | --- | --- |
| `core` | Shared primitives and the AI assistant used by several domains | `CoreModel`, `UUIDModel`, `TimeStampedModel`, `Conversation`, `Campaign` |
| `users` | Accounts, shop-scoped roles, subscriptions, customer profiles | `User`, `UserRole`, `Subscription`, `WishlistItem`, `CustomerAddress` |
| `shops` | Multi-tenant shop and branch registry, staff invitations | `Shop`, `Branch`, `Invitation`, `MerchantCategory` |
| `products` | Catalogue, stock levels, movements and internal transfers | `Product`, `Category`, `Inventory`, `InventoryMovement`, `StockTransfer` |
| `intake` | AI-assisted stock intake: receipts/photos → reviewable drafts → inventory | `IntakeBatch`, `ProductDraft` |
| `sales` | POS sales, shift reconciliation, marketplace orders | `Sale`, `SaleItem`, `Order`, `OrderItem`, `Shift`, `DailySalesSummary` |
| `purchases` | Supplier purchase orders, shipments and goods-received notes | `PurchaseOrder`, `PurchaseOrderItem`, `PurchaseShipment`, `GRN`, `GRNItem` |
| `expenses` | Operating expense ledger | `Expense` |
| `crm` | Customer and supplier relationships, invoicing, payments | `Customer`, `CustomerInvoice`, `CustomerPayment`, `Supplier`, `SupplierInvoice`, `SupplierPayment` |
| `corporate` | B2B wholesale to corporate buyers | `CorporateDepartment`, `CorporatePurchaseOrder`, `Customer` |
| `marketing` | Discount codes and campaigns | `DiscountCode`, `Campaign` |
| `social` | Facebook Login, page posting, messaging inbox, scheduled drips | `SocialIntegration`, `Conversation`, `Message`, `SocialLog`, `FacebookOAuthSession` |
| `telemetry` | Activity, product analytics and client error reporting | `ActivityLog`, `AnalyticsEvent`, `ErrorEvent`, `Announcement`, `SupportTicket` |

## API surface

Root routes (`config/urls.py`):

| Path | Purpose |
| --- | --- |
| `/healthz/` | Liveness probe. Exempt from SSL redirect so container healthchecks work. |
| `/admin/` | Django admin |
| `/api/auth/register/`, `/api/auth/login/` | Email/password registration and login |
| `/api/auth/google/` | Exchange a Google Identity Services credential for a JWT pair |
| `/api/auth/firebase/` | Legacy Firebase ID-token bridge |
| `/api/users/me/`, `/api/users/me/password/` | Current profile and password change |
| `/api/token/`, `/api/token/refresh/`, `/api/token/verify/` | Raw JWT endpoints |
| `/api/schema/` | OpenAPI 3 schema |
| `/api/docs/` | Swagger UI |
| `/api/v1/` | The versioned business API (50 registered router prefixes + 17 explicit paths) |
| `/media/<path>` | Uploaded media (served by the proxy/CDN in production) |

Representative `/api/v1/` groups: `shops/`, `branches/`, `products/`, `inventory/`,
`inventory/intake/`, `stock-transfers/`, `transfers/` (inter-shop B2B), `sales/`,
`orders/`, `shifts/`, `purchases/{orders,shipments,grns}/`, `customers/`, `suppliers/`,
`corporate/`, `discount-codes/`, `campaigns/`, `social/facebook/*`, `analytics/*`,
`telemetry/*`, `ai/assistant/`, `ai/extract-product/`, `ai/extract-products/`,
`identity/resolve/`, `portal/{orders,receipts}/`, `public/shops/`, `uploads/`,
`support/`, `announcements/`.

## Getting started

Prerequisites: Python 3.12. Redis is only needed for Celery; the API itself runs
without it.

```sh
git clone git@github.com:twende-org/duka_backend.git
cd duka_backend

python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit the values you need
set -a && . ./.env && set +a  # export into the current shell

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 8009
```

Open <http://127.0.0.1:8009/api/docs/> for Swagger UI and
<http://127.0.0.1:8009/healthz/> to confirm the process is up.

> Port 8009 is used above because 8000 is frequently taken on shared development
> machines; any free port works.

With no `POSTGRES_DB` in the environment the project uses `db.sqlite3`, so a fresh
clone is runnable in three commands. Set `POSTGRES_DB` to switch to PostgreSQL —
nothing else changes.

## Configuration

All configuration is environment-driven; `.env.example` is the tracked template and
documents every variable. `.env` is gitignored and must never be committed.

Loading it into the shell:

```sh
set -a && . ./.env && set +a
```

Variables that matter most:

| Variable | Default | Notes |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | — | **Required** when `DJANGO_DEBUG=False`; startup fails without it. |
| `DJANGO_DEBUG` | `True` | Set `False` in production to enable the security hardening block. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated. |
| `POSTGRES_DB` / `_USER` / `_PASSWORD` / `_HOST` / `_PORT` | unset | Presence of `POSTGRES_DB` selects PostgreSQL over SQLite. |
| `DB_CONN_MAX_AGE` / `DB_CONN_HEALTH_CHECKS` | `600` / `True` | Persistent connections; each gunicorn worker holds up to this many. |
| `CELERY_BROKER_URL` | `redis://127.0.0.1:6379/0` | |
| `CELERY_TASK_ALWAYS_EAGER` | `False` | Runs tasks inline — no broker needed. The test suite forces this. |
| `REDIS_CACHE_URL` | unset | Opt-in Redis cache; without it Django uses local-memory caching. |
| `CORS_ALLOWED_ORIGINS` / `CSRF_TRUSTED_ORIGINS` | localhost origins | Must list the frontend origin in production. |
| `GOOGLE_CLIENT_ID` | — | Required for Google sign-in token verification. |
| `OPENROUTER_API_KEY` | — | Required for AI intake and the assistant; those endpoints degrade gracefully without it. |
| `AI_INTAKE_MODEL` / `AI_INTAKE_MODEL_STRONG` / `AI_INTAKE_MIN_CONFIDENCE` | `google/gemini-2.5-flash` / `-pro` / `0.6` | Cheap model first, one escalation to the strong model. |
| `SOCIAL_TOKEN_ENCRYPTION_KEY` / `ENCRYPTION_KEY` | — | Encrypts stored Facebook tokens. |
| `FIREBASE_CREDENTIALS`, `FIREBASE_PROJECT_ID` | `./firebase-key.json` | Only for the legacy `import_firestore` importer and the Firebase auth bridge. |
| `DELIVERY_APP_*` | sync disabled | Firestore mirror for the Delivery App integration; `sync_delivery_app` is dry-run by default. |
| `WEB_CONCURRENCY` / `WEB_THREADS` | `3` / `2` | gunicorn sizing inside the container. |
| `RUN_MIGRATIONS` | `1` | Entrypoint switch: only the web container should migrate. |
| `CELERY_BEAT_SCHEDULE_FILENAME` | `/tmp/celerybeat-schedule` | See [Background jobs](#background-jobs). |

## Background jobs

Celery carries the scheduled work ported from the legacy Cloud Functions:

| Beat entry | Task | Schedule (local time) |
| --- | --- | --- |
| `daily-social-poster` | `apps.social.tasks.daily_social_poster` | 10:00, `Africa/Nairobi` |
| `peak-hours-marketing-drip` | `apps.social.tasks.peak_hours_marketing_drip` | 07:30 / 12:30 / 19:30, `Africa/Dar_es_Salaam` |

Run them locally:

```sh
celery -A config worker -l info
celery -A config beat   -l info
```

Beat persists its shelve schedule to `CELERY_BEAT_SCHEDULE_FILENAME`
(`/tmp/celerybeat-schedule` by default). The default is deliberately outside the
working tree: the container runs as an unprivileged user and `/app` is root-owned,
and the schedule is fully reconstructible from `CELERY_BEAT_SCHEDULE` on restart — so
no volume mount is required for beat.

## Testing

The suite lives in `tests/` and runs with Django's test runner. Tasks execute inline
(`CELERY_TASK_ALWAYS_EAGER`), so no broker is needed.

```sh
set -a && . ./.env && set +a
python manage.py test -v 1          # 714 tests
python manage.py test tests.test_products -v 2   # single module
```

Before committing:

```sh
python manage.py makemigrations --check --dry-run   # must report no changes
python manage.py test -v 1
```

## Docker

The image is a single artefact used three ways (web, worker, beat) — only the
`command` differs.

```sh
docker build -t duka_backend:latest .
```

It runs as uid/gid 1000 (`app`), exposes 8000, and its entrypoint waits for
Postgres, then — when `RUN_MIGRATIONS=1` — runs `migrate` and `collectstatic`
before handing off to gunicorn. Worker and beat containers set `RUN_MIGRATIONS=0`
so they never race the schema.

### Local stack

`docker-compose.yml` brings up PostgreSQL 16, Redis 7 and the API on your machine
without touching host ports 5432 or 6379 (they may belong to other projects):

```sh
cp .env.example .env      # set DJANGO_SECRET_KEY and POSTGRES_PASSWORD
docker compose up -d --build
curl -s http://127.0.0.1:8009/healthz/
```

The API is published on `127.0.0.1:${BACKEND_PORT:-8009}` only. Override the port
with `BACKEND_PORT=8010 docker compose up -d`.

## Production deployment

`docker-compose.prod.yml` targets the shared `*.twendedigital.tech` host: Traefik v3
terminates TLS with Let's Encrypt and routes `backend.twendedigital.tech` to
gunicorn. It attaches to the **existing external** `proxy` network and does not
define Traefik itself.

```
backend.twendedigital.tech  ->  api       (gunicorn, port 8000)
                                ├─ db      postgres:16-alpine  (volume duka_backend_db_data)
                                ├─ redis   redis:7-alpine      (volume duka_backend_redis_data)
                                ├─ worker  same image as api
                                └─ beat    same image as api
```

One-time server setup:

```sh
git clone git@github.com:twende-org/duka_backend.git ~/duka_backend
cd ~/duka_backend
cp .env.example .env && $EDITOR .env    # DJANGO_DEBUG=False, secrets, Postgres creds
```

`.env` must set, at minimum:

```ini
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<unique per environment>
DJANGO_ALLOWED_HOSTS=backend.twendedigital.tech
CORS_ALLOWED_ORIGINS=https://duka.twendedigital.tech
CSRF_TRUSTED_ORIGINS=https://duka.twendedigital.tech
POSTGRES_DB=duka_backend_db
POSTGRES_USER=duka_backend
POSTGRES_PASSWORD=<strong>
GOOGLE_CLIENT_ID=<oauth client>
```

Then deploy:

```sh
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d     # healthchecks gate start order
```

Verify:

```sh
curl -s  https://backend.twendedigital.tech/healthz/                       # {"status":"ok"}
curl -sI https://backend.twendedigital.tech/api/v1/ | head -1              # 401 (JWT required)
curl -s  https://backend.twendedigital.tech/api/docs/ -o /dev/null -w '%{http_code}\n'
docker compose -f docker-compose.prod.yml exec api python manage.py createsuperuser
```

CI (`.github/workflows/ci.yml`) runs the test suite on every push, then — on `main`
only — builds the image, pushes it to Docker Hub and redeploys over SSH. Required
repository secrets: `DOCKER_USERNAME`, `DOCKER_PASSWORD`, `SERVER_HOST`,
`SERVER_USER`, `SERVER_KEY`. Set `BACKEND_IMAGE` in the server environment to the
pushed tag (default `duka_backend:latest`) if you want deploys to pull instead of
build.

### Cutover note

If the previous monorepo stack is still running on the host, stop its backend
services first — both would otherwise claim the same Traefik host rule and the same
container names:

```sh
cd ~/multi-frontend && docker compose stop backend_api celery_worker celery_beat
```

The database starts **empty**; nothing migrates local SQLite content automatically.
Backfill with `python manage.py import_firestore` (needs `FIREBASE_CREDENTIALS`) or a
one-off `pg_dump`/`pg_restore` from another environment.

### Security posture (`DJANGO_DEBUG=False`)

HSTS (1 year), secure cookies, `SECURE_PROXY_SSL_HEADER` for Traefik-terminated TLS,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and no `SECRET_KEY`
fallback. `SECURE_SSL_REDIRECT` defaults to false because Traefik already redirects
port 80 → 443; `/healthz/` is exempt either way so container healthchecks never see a
301.

## Operations

- **Logs**: `docker compose -f docker-compose.prod.yml logs -f api` (also `worker`, `beat`).
- **Config change**: edit `.env`, then `docker compose -f docker-compose.prod.yml up -d api worker beat` — no rebuild needed.
- **Scale gunicorn**: `WEB_CONCURRENCY` / `WEB_THREADS`. Each worker holds up to `DB_CONN_MAX_AGE` persistent Postgres connections, so size the DB `max_connections` accordingly.
- **Migrations**: run automatically on `api` start (`RUN_MIGRATIONS=1`). For a manual run: `docker compose -f docker-compose.prod.yml exec api python manage.py migrate`.
- **Media**: `/app/media` is a named volume shared by `api` and `worker`.
- **Beat state**: `/tmp/celerybeat-schedule` inside the container, no volume; rebuilt from `CELERY_BEAT_SCHEDULE` on start.
- **Rollback**: retag the previous image and `up -d`; the Postgres volume is independent of the image.
- **Management commands**: `import_firestore` (legacy Firestore import), `sync_delivery_app` (Delivery App mirror — **dry-run unless `--apply`**; verify the target project before applying).

## Project layout

```
.
├── api/
│   └── v1/
│       ├── serializers/    # 21 modules — request/response contracts
│       ├── views/          # 24 modules — viewsets and API views
│       └── urls.py         # router registrations + explicit paths
├── apps/                   # 13 domain apps (models, tasks, commands)
├── config/
│   ├── settings.py         # env-driven; SQLite ↔ Postgres switch
│   ├── urls.py             # root URLconf
│   ├── celery.py           # Celery app bound to Django settings
│   ├── wsgi.py / asgi.py
├── docker/
│   └── entrypoint.sh       # wait-for-db → migrate → collectstatic → exec
├── tests/                  # consolidated suite (714 tests)
├── .env.example            # documented configuration template
├── Dockerfile
├── docker-compose.yml      # local development stack
├── docker-compose.prod.yml # Traefik / production stack
├── manage.py
└── requirements.txt        # fully pinned, including transitive deps
```
