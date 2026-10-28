FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer caching: install dependencies first (without project)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source code
COPY main.py ./
COPY src/ src/

# Copy entrypoint (runs migrations before starting the app)
COPY docker/entrypoint.sh docker/entrypoint.sh
RUN chmod +x docker/entrypoint.sh

# Install the project itself
RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["/app/docker/entrypoint.sh"]
