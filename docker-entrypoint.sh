#!/bin/sh
# Applies pending Alembic migrations, seeds synthetic demo data (a no-op if it's
# already present — see seed/seed_data.py), then starts the API. A container has no
# equivalent of the README's "run this once after cloning" manual steps, so all of
# this happens on every start instead. Re-seeding on every start matters on free-tier
# PaaS hosts with no persistent disk (e.g. Render's free plan), where the SQLite file
# is wiped on every cold start — this keeps the demo logins working regardless.
#
# $PORT is honored because PaaS hosts (Render, Railway) assign it dynamically; it
# falls back to 8000 for local `docker compose up`, which hardcodes that port mapping.
set -e

alembic upgrade head
python -m seed.seed_data --quiet
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
