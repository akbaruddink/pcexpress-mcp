# Pinned base image tag, not `python:3.12-slim` (which floats to whatever
# the latest 3.12.x/Debian point release is) -- consistent with this
# project's "no auto package updates" policy (see README "Dependency
# policy"). Bumping this is a deliberate, tested action like any other
# dependency bump here.
FROM python:3.12.9-slim-bookworm

# uv itself pinned too, for the same reason -- it's what actually installs
# every other pinned version below.
RUN pip install --no-cache-dir uv==0.12.3

WORKDIR /app

# Copy dependency manifests first so `uv sync` is cached by Docker unless
# they change, independent of application code edits.
COPY pyproject.toml uv.lock ./

# --locked: fail the build rather than silently re-resolving if uv.lock and
# pyproject.toml have drifted apart -- same guarantee CI enforces (see
# .github/workflows/tests.yml). No `--extra dev`: pytest/coverage have no
# place in a production image.
RUN uv sync --locked --extra http --no-install-project

COPY pc_express_mcp/ ./pc_express_mcp/
COPY scripts/ ./scripts/
# server.py reads this at import time and refuses to start without it.
COPY web/product-search-widget/dist/widget.html ./web/product-search-widget/dist/widget.html
COPY README.md ./
RUN uv sync --locked --extra http

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runs as an unprivileged user -- nothing here needs root, and stdio mode's
# optional state dir (if you mount one -- see README) should never be
# writable by more than this user.
RUN useradd --create-home --uid 1000 pcexpress && chown -R pcexpress:pcexpress /app
USER pcexpress

# EXPOSE is informational only (relevant for --http mode; see below) --
# stdio mode (the default CMD) doesn't listen on any port at all.
EXPOSE 8090

# Defaults to stdio mode -- the same safe-by-default posture as running
# this package directly with no arguments (see main() in server.py): no
# network exposure, no OAuth server, single local session. This matters
# specifically for Docker/self-hosting: HTTP mode is open self-service
# multi-tenant (see oauth_server.py's module docstring and README "Remote/
# mobile access") -- a real scope decision that should never be something a
# container silently defaults into. Opt into it explicitly with
# `-e PCEXPRESS_HTTP=1` (or by appending `--http`) plus the required
# PCEXPRESS_PUBLIC_URL / PCEXPRESS_TOKEN_SECRET -- see docker-compose.yml
# for the HTTP-mode-oriented setup.
ENTRYPOINT ["python", "-m", "pc_express_mcp.server"]
