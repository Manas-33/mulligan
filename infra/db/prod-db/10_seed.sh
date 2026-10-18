#!/bin/sh
# Seed the demo company from demo/seed (mounted at /seed) before 20_enable.sql audits it.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /seed/prod.sql
