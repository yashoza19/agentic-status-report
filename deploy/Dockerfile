FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
COPY skills/ skills/
COPY alembic/ alembic/
COPY alembic.ini .

RUN pip install ".[slack]"

USER 1001

CMD ["status", "slack", "run"]
