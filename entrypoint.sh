#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

CRON_FILE=/tmp/harbor2oss.cron
JOB_ARGS=${JOB_ARGS:-}

echo "[entrypoint] timezone: ${TZ:-unset}"
echo "[entrypoint] log level: ${LOG_LEVEL:-INFO}"
echo "[entrypoint] log language: ${LOG_LANGUAGE:-en}"
echo "[entrypoint] job args: ${JOB_ARGS:-<none>}"

if [ "${RUN_ON_STARTUP:-true}" = "true" ]; then
  echo "[entrypoint] running initial sync"
  python /app/harbor2oss_sync.py ${JOB_ARGS} || true
fi

echo "${CRON_SCHEDULE:-0 0 * * *} python /app/harbor2oss_sync.py ${JOB_ARGS}" > "${CRON_FILE}"
echo "[entrypoint] cron schedule: ${CRON_SCHEDULE:-0 0 * * *}"
exec /usr/local/bin/supercronic "${CRON_FILE}"
