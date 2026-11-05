FROM python:3.10-slim AS server

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

# Keep the official Debian sources, using TLS for downloads.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY multisource_data_agent ./multisource_data_agent
RUN python -m pip install --upgrade pip --index-url https://pypi.org/simple && \
    pip install ".[deploy]" --index-url https://pypi.org/simple && \
    pip check

COPY scripts/backend_entrypoint.sh ./backend_entrypoint.sh
RUN chmod +x /app/backend_entrypoint.sh && \
    sed -i 's/\r$//' /app/backend_entrypoint.sh

# Synthetic Retail data is imported explicitly by the demo bootstrap.

EXPOSE 8000
ENTRYPOINT ["./backend_entrypoint.sh"]
CMD ["uvicorn", "multisource_data_agent.main:application", "--host", "0.0.0.0", "--port", "8000"]
