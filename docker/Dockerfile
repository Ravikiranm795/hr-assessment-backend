# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

# ---- builder: resolve and install dependencies into a virtualenv ----
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# ---- runtime ----
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# WeasyPrint needs pango/harfbuzz at runtime; fonts so PDFs render text.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 libffi8 \
        shared-mime-info fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Optional: the Claude CLI for the CLI AI provider. Build with --build-arg INSTALL_CLAUDE_CLI=true
ARG INSTALL_CLAUDE_CLI=false
RUN if [ "$INSTALL_CLAUDE_CLI" = "true" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends nodejs npm \
        && npm install -g @anthropic-ai/claude-code \
        && rm -rf /var/lib/apt/lists/* /root/.npm; \
    fi

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    XDG_CACHE_HOME=/tmp/.cache
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app . /app
RUN chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /app/storage_data \
    && chown app:app /app/storage_data

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
