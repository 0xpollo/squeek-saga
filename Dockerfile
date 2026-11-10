FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md ./
COPY squeek_saga ./squeek_saga
RUN pip install .

# Run unprivileged; /data holds the SQLite database and the cached deployment record.
RUN useradd --system --uid 10001 --home-dir /app saga \
    && mkdir -p /data \
    && chown saga:saga /data
USER saga
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["python", "-m", "squeek_saga"]
