# One image for both processes (they share dependencies). Compose picks the command.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

# Dependencies first so this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY server.py downstream_api.py ./

# Non-root; /data holds FastMCP's OAuth proxy storage (mount a volume there). No secrets baked in.
RUN useradd --system --create-home app && mkdir /data && chown app /data
USER app
ENV FASTMCP_HOME=/data
EXPOSE 8000 8001
CMD ["python", "server.py"]
