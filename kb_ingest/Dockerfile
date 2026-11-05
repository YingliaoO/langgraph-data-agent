FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Use the official Debian sources over TLS.
# System dependencies for psycopg2
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev git \
    && rm -rf /var/lib/apt/lists/*

# 依赖清单，先做依赖安装
COPY requirements.txt .
RUN python -m pip install --upgrade pip --index-url https://pypi.org/simple \
 && pip install -r requirements.txt --index-url https://pypi.org/simple

# `.env` is supplied at runtime via docker-compose `env_file` / environment variables.
# Do not bake secrets into the image, and avoid build failures when the file is absent.

# 源码
COPY kb_service ./kb_service
COPY main.py ./
COPY ./entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && \
    sed -i 's/\r$//' /app/entrypoint.sh && \
    test -s /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
CMD ["uvicorn", "kb_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
