ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS python-wheels

# Official Python images already include the matching Python headers. Build on
# the target architecture so crcmod cannot silently fall back to Python-only CRC.
RUN apt-get update -o Acquire::Retries=3 \
    && apt-get install -y --no-install-recommends -o Acquire::Retries=3 gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /build/requirements.txt
RUN pip wheel --no-cache-dir --no-binary=crcmod --wheel-dir=/wheels -r /build/requirements.txt

FROM ${PYTHON_IMAGE}

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

RUN set -eux; \
    for attempt in 1 2 3 4 5; do \
      apt-get update -o Acquire::Retries=3 \
      && apt-get install -y --no-install-recommends -o Acquire::Retries=3 ca-certificates curl skopeo tzdata \
      && rm -rf /var/lib/apt/lists/* \
      && break; \
      if [ "$attempt" = "5" ]; then exit 1; fi; \
      rm -rf /var/lib/apt/lists/*; \
      sleep $((attempt * 5)); \
    done

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) supercronic_arch=amd64 ;; \
      arm64) supercronic_arch=arm64 ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    for attempt in 1 2 3 4 5; do \
      curl --retry 3 --retry-delay 2 --retry-all-errors -fsSL -o /usr/local/bin/supercronic "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${supercronic_arch}" \
      && break; \
      if [ "$attempt" = "5" ]; then exit 1; fi; \
      sleep $((attempt * 5)); \
    done; \
    chmod +x /usr/local/bin/supercronic

WORKDIR /app

COPY requirements.txt .
COPY --from=python-wheels /wheels /wheels
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY verify_native_crc.py ./
RUN python /app/verify_native_crc.py

COPY harbor2oss_sync.py entrypoint.sh ./
RUN chmod +x /app/harbor2oss_sync.py /app/entrypoint.sh \
    && mkdir -p /tmp/harbor2oss

ENTRYPOINT ["/app/entrypoint.sh"]
