# VERSIONS
ARG PYTHON_VERSION=3.14
ARG PYTHON_IMAGE_TAG=${PYTHON_VERSION}-slim
ARG UV_VERSION=latest

# uv
FROM ghcr.io/astral-sh/uv:$UV_VERSION AS uv

FROM python:${PYTHON_IMAGE_TAG} AS run
COPY --from=uv /uv /uvx /bin/

# System deps (curl do healthchecków, libxml/libxslt często potrzebne przy parsowaniu)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
 && rm -rf /var/lib/apt/lists/*


# Add a new user and group
RUN groupadd -g 1001 appgroup && \
    useradd -m -u 1001 -g appgroup appuser && \
    mkdir /app && \
    chown -R appuser:appgroup /app
# Set working directory in container
WORKDIR /app

USER appuser

# Install dependencies
COPY pyproject.toml .
COPY uv.lock .

RUN uv sync --locked

# Copy the rest of the application files
ADD main.py ./


ENV LTE_DATA_DIR=/tmp
ENV LTE_BASE_URL=https://twoja-domena.pl

ENV LTE_SERVER_PORT=8000
EXPOSE ${LTE_SERVER_PORT}
ENV LTE_SERVER_ADDRESS="0.0.0.0"

CMD ["uv", "run", "uvicorn", "main:app", "--host", "${LTE_SERVER_ADDRESS}", "--port", "${LTE_SERVER_PORT}"]