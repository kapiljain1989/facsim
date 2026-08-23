# Runtime image for the simulator.
#
# Includes the Postgres and ClickHouse extras so the same image can run the
# zero-setup default or the polyglot production shape, selected by configuration
# or by the PHARMA_* environment overrides.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a source change does not invalidate the layer.
COPY pyproject.toml README.md ./
COPY src/pharma_sim/__init__.py src/pharma_sim/__init__.py
RUN pip install --upgrade pip && pip install ".[postgres,clickhouse]"

COPY src/ src/
COPY config/ config/
COPY scripts/ scripts/
RUN pip install --no-deps -e .

# Data is written here; mount a volume to keep it.
RUN mkdir -p /app/data && \
    useradd --create-home --uid 10001 pharma && \
    chown -R pharma:pharma /app
USER pharma

# Fail fast on a bad configuration rather than part-way through a long run.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -m pharma_sim validate > /dev/null || exit 1

ENTRYPOINT ["python", "-m", "pharma_sim"]
CMD ["run", "--days", "30"]
