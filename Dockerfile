# Small, non-root image for unattended running (REVIEW findings 17, 18).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DSB_DATABASE_PATH=/data/dexsignal.db \
    DSB_LOG_FORMAT=json

WORKDIR /app
COPY pyproject.toml README.md LICENSE.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 dexsignal \
    && mkdir -p /data && chown dexsignal /data

USER dexsignal
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=30s --start-period=10m \
    CMD ["dexsignal", "health", "--max-age-minutes", "15"]

ENTRYPOINT ["dexsignal"]
CMD ["run"]
