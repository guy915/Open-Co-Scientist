#!/bin/sh
set -eu

if [ "${1:-}" = "--serve" ]; then
    exec python -m uvicorn co_scientist.serving:create_app --factory --no-proxy-headers --host 0.0.0.0 \
        --port "${PORT:-8008}" --timeout-graceful-shutdown 20 --timeout-keep-alive 15
fi
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

unset COSCIENTIST_LITESTREAM_ACTIVE
if [ -z "${LITESTREAM_R2_BUCKET:-}" ] || [ -z "${LITESTREAM_R2_ENDPOINT:-}" ] || \
   [ -z "${LITESTREAM_R2_ACCESS_KEY_ID:-}" ] || [ -z "${LITESTREAM_R2_SECRET_ACCESS_KEY:-}" ]; then
    exec "$0" --serve
fi

export COSCIENTIST_LITESTREAM_ACTIVE=1
export LITESTREAM_R2_PATH="${LITESTREAM_R2_PATH:-api/coscientist}"
export COSCIENTIST_DB_PATH="${COSCIENTIST_DB_PATH:-/app/data/coscientist.db}"
config="${LITESTREAM_CONFIG:-/etc/litestream.yml}"

had_database=0
if [ -e "$COSCIENTIST_DB_PATH" ]; then had_database=1; fi
# Missing replicas are a fresh install; network or corrupt-backup errors must fail closed.
litestream restore -config "$config" -if-db-not-exists -if-replica-exists "$COSCIENTIST_DB_PATH"
if [ "$had_database" = 0 ] && [ -e "$COSCIENTIST_DB_PATH" ]; then
    # A replica can trail the Azure ledger; hold paid spend until re-recorded.
    python -m co_scientist.platform.db.spend "$COSCIENTIST_DB_PATH"
fi
exec python -m co_scientist.platform.db.backup_service --config "$config" -- "$0" --serve
