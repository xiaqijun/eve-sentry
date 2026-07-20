FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    UV_DEFAULT_INDEX=https://mirrors.cloud.tencent.com/pypi/simple \
    UV_LINK_MODE=copy \
    UV_CONCURRENT_INSTALLS=1

RUN sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.11.6

WORKDIR /app
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps . \
    && rm -f /tmp/requirements.txt

RUN useradd --create-home --uid 10001 evebot \
    && mkdir -p /data \
    && chown evebot:evebot /data
USER evebot

CMD ["python", "-m", "eve_risk.bot"]
