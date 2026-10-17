FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WALLETWATCHER_DATA_DIR=/data \
    WALLETWATCHER_HEARTBEAT=/tmp/walletwatcher-heartbeat

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check \
    && groupadd --gid 10001 walletwatcher \
    && useradd --uid 10001 --gid 10001 --no-create-home walletwatcher \
    && mkdir /data \
    && chown walletwatcher:walletwatcher /data
COPY *.py ./
USER 10001:10001

FROM base AS test
COPY requirements-test.txt ./
USER root
RUN python -m pip install --no-cache-dir -r requirements-test.txt
USER 10001:10001
COPY tests/ ./tests/
CMD ["python", "-B", "tests/run_tests.py"]

FROM base AS runtime
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "healthcheck.py"]
CMD ["python", "supervisor.py"]
