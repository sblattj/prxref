# syntax=docker/dockerfile:1

# Stage 1: Build wheel using uv
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Copy project definition and source tree
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Build wheel package using uv
RUN uv build --wheel --out-dir /dist

# Stage 2: Final runtime container
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Create non-root system user and group
RUN groupadd -r prxref && useradd -r -g prxref -d /app -s /sbin/nologin prxref

WORKDIR /app

# Copy wheel from builder stage and install into system Python
COPY --from=builder /dist/*.whl /tmp/
RUN uv pip install --system /tmp/*.whl && rm -rf /tmp/*.whl

USER prxref

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).getcode() == 200 else 1)"

CMD ["prxref", "serve"]
