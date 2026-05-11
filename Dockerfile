FROM python:3.12-slim

ARG SUPERCRONIC_VERSION=v0.2.33
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TMP_DIR=/tmp/harbor2oss \
    CRON_SCHEDULE="0 0 * * *" \
    RUN_ON_STARTUP=true \
    TZ=Asia/Shanghai \
    LOG_LEVEL=INFO \
    LOG_LANGUAGE=en

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl skopeo tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) supercronic_arch=amd64 ;; \
      arm64) supercronic_arch=arm64 ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/supercronic "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${supercronic_arch}"; \
    chmod +x /usr/local/bin/supercronic

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY harbor2oss_sync.py entrypoint.sh ./
RUN chmod +x /app/harbor2oss_sync.py /app/entrypoint.sh \
    && mkdir -p /tmp/harbor2oss

ENTRYPOINT ["/app/entrypoint.sh"]
